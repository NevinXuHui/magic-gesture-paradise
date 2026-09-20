import asyncio
import gc
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import tempfile
import unittest
import warnings
from dataclasses import replace
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from smartapp_runtime.config import RuntimeConfig, PathsConfig, TimeoutConfig, LimitConfig, ProcessConfig
from smartapp_runtime.domain.commands import StopReason
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest
from smartapp_runtime.domain.models import Session
from smartapp_runtime.infrastructure.packages.installer import InstalledApp
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.infrastructure.persistence.state_repository import FileStateRepository
from smartapp_runtime.infrastructure.persistence.recovery import ObservedProcessIdentity
from smartapp_runtime.infrastructure.processes.supervisor import ProcessSupervisor
from smartapp_runtime.ports.process import BackendEvent, BackendHandle


FIXTURES = Path(__file__).parents[1] / "fixtures" / "backends"


class SignalFailureProcess:
    """Virtual process for denied-signal tests; its PID never reaches the OS."""
    pid = 987654321

    def __init__(self):
        self.returncode = None
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.stdin = self
        self.closed = False
        self.exited = asyncio.Event()
        self.allow_signals = False
        self.signals = []

    def write(self, payload):
        if self.closed:
            raise BrokenPipeError()
        if json.loads(payload)["event"] == "runtime_init":
            self.stdout.feed_data(b'{"event":"app_ready"}\n')

    async def drain(self):
        pass

    def close(self):
        self.closed = True

    async def wait(self):
        await self.exited.wait()
        return self.returncode

    def finish(self):
        if self.returncode is None:
            self.returncode = -signal.SIGKILL
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    def send_group(self, pid, signum):
        if pid != self.pid:
            raise AssertionError("signal outside virtual process group")
        if self.returncode is not None:
            raise ProcessLookupError()
        if signum:
            self.signals.append((pid, signum))
            if not self.allow_signals:
                raise PermissionError("private signal failure details")
            if signum == signal.SIGKILL:
                self.finish()


class IdentityRetryProcess(SignalFailureProcess):
    def __init__(self, entry):
        super().__init__()
        self.original = ObservedProcessIdentity("12345", ("python", "-u", str(entry)))
        self.observation = self.original
        self.leader_exists = True
        self.group_exists = True
        self.replacement = False
        self.replace_on_term = False
        self.replacement_signals = []

    def read(self, pid):
        if pid != self.pid:
            raise AssertionError("identity lookup outside virtual process")
        return self.observation if self.leader_exists else None

    def getpgid(self, pid):
        if pid != self.pid:
            raise AssertionError("PID lookup outside virtual process")
        if not self.leader_exists:
            raise ProcessLookupError()
        return self.pid

    def leader_exit(self):
        self.returncode = 0
        self.leader_exists = False
        self.exited.set()

    def reuse(self, observation):
        self.leader_exit()
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self.leader_exists = True
        self.observation = observation
        self.replacement = True

    def finish(self):
        self.leader_exit()
        if not self.stdout.at_eof():
            self.stdout.feed_eof()
        if not self.stderr.at_eof():
            self.stderr.feed_eof()
        self.group_exists = False

    def send_group(self, pid, signum):
        if pid != self.pid:
            raise AssertionError("signal outside virtual process group")
        if not self.group_exists:
            raise ProcessLookupError()
        if not signum:
            return
        self.signals.append((pid, signum))
        if self.replacement:
            self.replacement_signals.append(signum)
            self.group_exists = False
            return
        if not self.allow_signals:
            raise PermissionError("private denied signal")
        if signum == signal.SIGTERM and self.replace_on_term:
            self.reuse(ObservedProcessIdentity("99999", self.original.cmdline))
        elif signum == signal.SIGKILL:
            self.finish()


@unittest.skipUnless(os.name == "posix", "process groups require POSIX")
class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths.from_root(Path(self.temp.name) / "runtime")
        self.paths.ensure_layout()
        self.root = self.paths.apps_root / "demo" / "v1"
        (self.root / "backend").mkdir(parents=True)
        self.repo = FileStateRepository(self.paths.state_file)
        self.config = RuntimeConfig(
            paths=PathsConfig(root=self.paths.root, python_executable=Path(sys.executable)),
            timeouts=TimeoutConfig(startup=0.35, graceful_stop=0.08, sigterm=0.08),
            limits=LimitConfig(max_message_bytes=4096),
            process=ProcessConfig(env_passthrough=("LANG", "ALLOWED", "SMARTAPP_SESSION_ID")))
        self.session = Session("session-1", "demo", "v1", 1)
        self.supervisor = ProcessSupervisor(self.config, self.repo, environment={
            "LANG": "C.UTF-8", "ALLOWED": "yes", "SECRET": "hidden", "SMARTAPP_SESSION_ID": "spoof"})
        self.events, self.exits, self.logs = [], [], []
        self.handles, self.pids = [], set()
        self.task_errors = []
        self.loop = asyncio.get_running_loop()
        self.old_handler = self.loop.get_exception_handler()
        self.loop.set_exception_handler(lambda loop, context: self.task_errors.append(context))
        self.initial_tasks = asyncio.all_tasks()

    async def asyncTearDown(self):
        for handle in self.handles:
            await self.supervisor.stop(handle, StopReason.OPERATOR)
        for path in self.root.glob("*.pid"):
            self.pids.add(int(path.read_text()))
        for pid in self.pids:
            await self.wait_until(lambda pid=pid: not self.pid_exists(pid))
        await asyncio.sleep(0)
        leaked = [task for task in asyncio.all_tasks() - self.initial_tasks
                  if task is not asyncio.current_task() and not task.done()]
        self.loop.set_exception_handler(self.old_handler)
        self.assertEqual(leaked, [], "supervisor left live tasks")
        self.assertEqual(self.task_errors, [], "unretrieved task exceptions")
        self.assertIsNone(self.repo.load().backend_process)
        self.temp.cleanup()

    @staticmethod
    def pid_exists(pid):
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False

    async def wait_until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(poll(), 3)

    def installed(self, fixture="ready_backend.py"):
        shutil.copyfile(FIXTURES / fixture, self.root / "backend" / "main.py")
        manifest = Manifest.from_dict({"schemaVersion": 1, "appId": "demo", "version": "v1",
            "web": {"enabled": False, "entry": "index.html"},
            "backend": {"enabled": True, "entry": "main.py", "dynamicService": False},
            "routing": {"defaultTarget": "python"}})
        return InstalledApp(self.root, manifest, "a" * 64, 1, False)

    async def start(self, data=None, fixture="ready_backend.py", on_event=None, on_exit=None, on_stderr=None):
        handle = await self.supervisor.start(self.installed(fixture), self.session, data or {},
            on_event or self.events.append, on_exit or (lambda handle, error: self.exits.append((handle, error))),
            on_stderr or (lambda text, truncated: self.logs.append((text, truncated))))
        self.handles.append(handle)
        self.pids.add(handle.pid)
        return handle

    async def send(self, handle, data, seq=0):
        await self.supervisor.send(handle, {"event": "cloud_data", "seq": seq, "data": data})

    @asynccontextmanager
    async def retry_backend(self, process):
        async def spawn(*args, **kwargs):
            return process
        with patch("smartapp_runtime.infrastructure.processes.supervisor.asyncio.create_subprocess_exec", new=spawn), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.os.killpg", new=process.send_group), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.os.getpgid", new=process.getpgid), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.sys", SimpleNamespace(platform="linux")), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.LinuxProcessIdentityReader", return_value=process):
            handle = await self.supervisor.start(self.installed(), self.session, {},
                self.events.append, lambda handle, error: self.exits.append((handle, error)))
            try:
                with self.assertRaises(SmartAppError):
                    await asyncio.wait_for(self.supervisor.stop(handle, StopReason.OPERATOR), 0.7)
                yield handle
            finally:
                process.finish()
                await asyncio.gather(self.supervisor.stop(handle, StopReason.OPERATOR), return_exceptions=True)

    async def test_environment_identity_runtime_init_and_echo(self):
        handle = await self.start({"mode": "report", "nested": {"value": "中文"}})
        self.assertIsInstance(handle, BackendHandle)
        await self.wait_until(lambda: self.events)
        report = self.events[0].data
        self.assertEqual(report["init"], {"event": "runtime_init", "sessionId": "session-1",
            "data": {"mode": "report", "nested": {"value": "中文"}}})
        self.assertEqual(report["argv"], [str(self.root / "backend" / "main.py")])
        self.assertEqual(report["cwd"], str(self.root))
        env = report["environment"]
        self.assertEqual(env["ALLOWED"], "yes")
        self.assertNotIn("SECRET", env)

        self.assertNotIn("HOME", env)
        self.assertEqual(env["SMARTAPP_SESSION_ID"], "session-1")
        self.assertEqual(env["SMARTAPP_APP_ID"], "demo")
        self.assertEqual(env["SMARTAPP_VERSION"], "v1")
        self.assertEqual(env["SMARTAPP_DYNAMIC_HOST"], "127.0.0.1")
        self.assertEqual(env["SMARTAPP_DYNAMIC_PORT"], "18081")
        identity = self.repo.load().backend_process
        self.assertEqual(identity.pid, handle.pid)
        self.assertEqual(identity.command_marker, str(self.root / "backend" / "main.py"))
        self.assertTrue(identity.start_time.isdecimal() if sys.platform.startswith("linux")
                        else identity.start_time == "non-linux")
        await asyncio.gather(*(self.send(handle, {"index": n, "text": "中文"}, n) for n in range(20)))
        await self.wait_until(lambda: len(self.events) == 21)
        self.assertEqual([event.data["index"] for event in self.events[1:]], list(range(20)))
        self.assertTrue(all(isinstance(event, BackendEvent) for event in self.events))
        await self.supervisor.stop(handle, StopReason.CLOUD_STOP)
        self.assertEqual(json.loads((self.root / "stop.json").read_text()),
                         {"event": "app_stop", "reason": "cloud_stop"})
        self.assertEqual(self.exits, [])

    async def test_owned_callback_observes_handle_before_identity_persistence(self):
        observed = []

        def on_owned(handle):
            observed.append((handle, self.repo.load().backend_process))

        handle = await self.supervisor.start(
            self.installed(), self.session, {}, self.events.append,
            lambda handle, error: self.exits.append((handle, error)),
            on_owned=on_owned,
        )
        self.handles.append(handle)
        self.pids.add(handle.pid)
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], handle)
        self.assertIsNone(observed[0][1])

    async def test_persist_failure_and_denied_signals_retains_notified_ownership(self):
        process = SignalFailureProcess()
        owned = []

        async def spawn(*args, **kwargs):
            return process

        with patch("smartapp_runtime.infrastructure.processes.supervisor.asyncio.create_subprocess_exec", new=spawn), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.os.killpg", new=process.send_group), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.sys", SimpleNamespace(platform="darwin")), \
                patch.object(self.repo, "set_backend_process", side_effect=OSError("secret")):
            try:
                with self.assertRaises(SmartAppError):
                    await self.supervisor.start(
                        self.installed(), self.session, {}, self.events.append,
                        lambda *args: None, on_owned=owned.append,
                    )
                self.assertEqual(len(owned), 1)
                self.assertEqual(owned[0].pid, process.pid)
                self.assertIsNotNone(self.supervisor._active)
                self.assertIsNone(process.returncode)
                process.allow_signals = True
                await self.supervisor.stop(owned[0], StopReason.OPERATOR)
            finally:
                process.finish()
                await asyncio.gather(
                    self.supervisor.stop(owned[0], StopReason.OPERATOR)
                    if owned else asyncio.sleep(0),
                    return_exceptions=True,
                )

    async def test_owned_callback_failure_uses_startup_cleanup(self):
        observed = []

        def fail(handle):
            observed.append(handle)
            self.pids.add(handle.pid)
            raise RuntimeError("private callback failure")

        with self.assertRaises(SmartAppError) as caught:
            await self.supervisor.start(
                self.installed(), self.session, {}, self.events.append,
                lambda *args: None, on_owned=fail,
            )
        self.assertEqual(caught.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("private", str(caught.exception))
        self.assertEqual(len(observed), 1)
        self.assertTrue(all(not self.pid_exists(pid) for pid in self.pids))
        self.assertIsNone(self.supervisor._active)

    async def test_ready_timeout_and_premature_exit_are_start_errors(self):
        for mode, code in (("timeout", ErrorCode.START_TIMEOUT), ("early_exit", ErrorCode.BACKEND_EXITED)):
            with self.subTest(mode=mode):
                with self.assertRaises(SmartAppError) as caught:
                    await self.start({"mode": mode})
                self.assertEqual(caught.exception.code, code)
                pid = int((self.root / "leader.pid").read_text())
                self.pids.add(pid)
                self.assertFalse(self.pid_exists(pid))
                self.assertIsNone(self.repo.load().backend_process)
        self.assertEqual(self.exits, [])

    async def test_conflicting_start_and_stale_handle_do_not_stop_active_process(self):
        handle = await self.start()
        with self.assertRaises(SmartAppError) as caught:
            await self.start()
        self.assertEqual(caught.exception.code, ErrorCode.SESSION_CONFLICT)
        stale = replace(handle, session=replace(self.session, generation=2))
        await self.supervisor.stop(stale, StopReason.OPERATOR)
        with self.assertRaises(SmartAppError):
            await self.send(stale, {})
        await self.send(handle, {"alive": True})
        await self.wait_until(lambda: self.events)
        self.assertEqual(self.events[0].data, {"alive": True})

    async def test_send_rejects_invalid_and_oversized_logical_messages(self):
        handle = await self.start()
        valid = {"event": "cloud_data", "seq": 0, "data": {}}
        for message in ([], {"event": "app_stop"}, dict(valid, seq=True), dict(valid, seq=-1),
                        dict(valid, seq=2 ** 63), dict(valid, data=[]), dict(valid, dataType=""),
                        dict(valid, extra=1), dict(valid, data={"x": float("nan")}),
                        dict(valid, data={"x": "中" * 4096})):
            with self.subTest(message_type=type(message).__name__):
                with self.assertRaises(SmartAppError):
                    await self.supervisor.send(handle, message)
        await self.send(handle, {"ok": True})
        await self.wait_until(lambda: self.events)
        self.assertEqual(len(self.events), 1)

    async def test_stderr_truncates_and_replaces_invalid_utf8(self):
        await self.start({"mode": "stderr"})
        await self.wait_until(lambda: len(self.logs) == 3)
        self.assertEqual(self.logs[0], ("log�", False))
        self.assertEqual(self.logs[1], ("x" * 4096, True))
        self.assertEqual(self.logs[2], ("after", False))

    async def test_unexpected_exit_notifies_once_and_clears_identity(self):
        handle = await self.start({"mode": "exit"})
        await self.wait_until(lambda: self.exits)
        self.assertEqual(self.exits[0][0], handle)
        self.assertEqual(self.exits[0][1].code, ErrorCode.BACKEND_EXITED)
        self.assertIsNone(self.repo.load().backend_process)
        await self.supervisor.stop(handle, StopReason.OPERATOR)
        self.assertEqual(len(self.exits), 1)

    async def test_protocol_rejects_each_invalid_line_kind_at_threshold(self):
        self.supervisor.config = replace(self.config, process=replace(self.config.process, protocol_violation_limit=1))
        invalid = ["{", "[]", "utf8", "oversized", '{"event":"other"}',
            '{"event":"app_ready"}', '{"event":"app_ready","extra":1}',
            '{"event":"app_data","dataType":"","data":{}}',
            '{"event":"app_data","dataType":"x","data":[]}',
            '{"event":"app_data","dataType":"x","data":{"a":1,"a":2}}',
            '{"event":"app_data","dataType":"x","data":{"n":NaN}}']
        for line in invalid:
            with self.subTest(line=line):
                self.exits.clear()
                handle = await self.start({"lines": [line]}, "invalid_backend.py")
                await self.wait_until(lambda: self.exits)
                self.assertEqual(self.exits[0][1].code, ErrorCode.BACKEND_PROTOCOL_ERROR)
                self.assertFalse(self.pid_exists(handle.pid))

    async def test_consecutive_violations_reset_and_default_fifth_terminates(self):
        valid = '{"event":"app_data","dataType":"x","data":{"ok":true}}'
        lines = ["{"] * 4 + [valid, "", "{"] + ["{"] * 4
        await self.start({"lines": lines}, "invalid_backend.py")
        await self.wait_until(lambda: self.exits)
        self.assertEqual([event.data for event in self.events], [{"ok": True}])
        self.assertEqual(self.exits[0][1].code, ErrorCode.BACKEND_PROTOCOL_ERROR)

    async def test_protocol_failure_before_ready_raises_without_exit_callback(self):
        with self.assertRaises(SmartAppError) as caught:
            await self.start({"lines": ["{"] * 5, "before_ready": True}, "invalid_backend.py")
        self.assertEqual(caught.exception.code, ErrorCode.BACKEND_PROTOCOL_ERROR)
        self.assertEqual(self.exits, [])

    async def test_async_callbacks_are_serialized_and_event_failure_cleans_up(self):
        active = []
        async def event_callback(event):
            self.assertFalse(active)
            active.append("event")
            await asyncio.sleep(0.02)
            active.clear()
            raise ValueError("secret event payload")
        async def exit_callback(handle, error):
            self.assertFalse(active)
            self.exits.append((handle, error))
            raise ValueError("secret callback")
        handle = await self.start(on_event=event_callback, on_exit=exit_callback)
        await self.send(handle, {"go": True})
        await self.wait_until(lambda: self.exits)
        self.assertEqual(self.exits[0][1].code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("secret", str(self.exits[0][1]))

    async def test_stderr_callback_failure_does_not_break_cleanup(self):
        def fail(*args):
            raise RuntimeError("secret stderr")
        handle = await self.start({"mode": "stderr"}, on_stderr=fail)
        await self.supervisor.stop(handle, StopReason.OPERATOR)
        self.assertFalse(self.pid_exists(handle.pid))

    async def test_stderr_callback_self_cancel_keeps_reading_later_logs(self):
        count = 0
        async def callback(text, truncated):
            nonlocal count
            count += 1
            if count == 1:
                raise asyncio.CancelledError()
            self.logs.append((text, truncated))
        handle = await self.start({"mode": "stderr"}, on_stderr=callback)
        await self.wait_until(lambda: len(self.logs) == 2)
        self.assertEqual(self.logs, [("x" * 4096, True), ("after", False)])
        await self.send(handle, {"still_running": True})
        await self.wait_until(lambda: self.events)
        self.assertEqual(self.events[0].data, {"still_running": True})
        self.assertEqual(self.exits, [])

    async def test_external_stderr_reader_cancellation_is_not_swallowed(self):
        entered = asyncio.Event()
        async def callback(text, truncated):
            entered.set()
            await asyncio.Event().wait()
        handle = await self.start({"mode": "stderr"}, on_stderr=callback)
        await asyncio.wait_for(entered.wait(), 1)
        reader = self.supervisor._active.tasks[1]
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await reader
        self.assertEqual(self.exits, [])
        await self.supervisor.stop(handle, StopReason.OPERATOR)

    async def test_all_signals_denied_returns_error_retains_ownership_and_retries(self):
        process = SignalFailureProcess()
        async def spawn(*args, **kwargs):
            return process
        with patch("smartapp_runtime.infrastructure.processes.supervisor.asyncio.create_subprocess_exec", new=spawn), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.os.killpg", new=process.send_group), \
                patch("smartapp_runtime.infrastructure.processes.supervisor.sys", SimpleNamespace(platform="darwin")):
            handle = await self.supervisor.start(self.installed(), self.session, {},
                self.events.append, lambda handle, error: self.exits.append((handle, error)))
            identity = self.repo.load().backend_process
            try:
                for attempt in range(2):
                    with self.assertRaises(SmartAppError) as caught:
                        await asyncio.wait_for(self.supervisor.stop(handle, StopReason.OPERATOR), 0.7)
                    self.assertEqual(caught.exception.code, ErrorCode.INTERNAL_ERROR)
                    self.assertNotIn("private", str(caught.exception))
                    self.assertIsNone(process.returncode)
                    self.assertEqual(self.repo.load().backend_process, identity)
                    owned = self.supervisor._active.tasks
                    self.assertEqual(len(owned), 3)
                    self.assertTrue(all(task in asyncio.all_tasks() and not task.done() for task in owned))
                    with self.assertRaises(SmartAppError) as conflict:
                        await self.supervisor.start(self.installed(), self.session, {},
                            self.events.append, lambda *args: None)
                    self.assertEqual(conflict.exception.code, ErrorCode.SESSION_CONFLICT)
                self.assertEqual(process.signals, [(process.pid, signal.SIGTERM),
                    (process.pid, signal.SIGKILL)] * 2)
                process.allow_signals = True
                await asyncio.wait_for(self.supervisor.stop(handle, StopReason.OPERATOR), 0.7)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                self.assertIsNone(self.repo.load().backend_process)
                self.assertIsNone(self.supervisor._active)
                self.assertEqual(self.exits, [])
            finally:
                # Also release a broken implementation after the outer timeout.
                process.finish()
                await asyncio.gather(self.supervisor.stop(handle, StopReason.OPERATOR), return_exceptions=True)

    async def test_stop_retry_rejects_reused_leader_start_time_or_argv(self):
        for mismatch in ("start_time", "argv"):
            with self.subTest(mismatch=mismatch):
                process = IdentityRetryProcess(self.root / "backend" / "main.py")
                async with self.retry_backend(process) as handle:
                    observation = (ObservedProcessIdentity("99999", process.original.cmdline)
                        if mismatch == "start_time" else ObservedProcessIdentity("12345", ("python", "unrelated.py")))
                    process.reuse(observation)
                    results = await asyncio.wait_for(asyncio.gather(
                        self.supervisor.stop(handle, StopReason.OPERATOR), return_exceptions=True), 0.7)
                    self.assertEqual(process.replacement_signals, [])
                    self.assertIsInstance(results[0], SmartAppError)
                    self.assertEqual(results[0].code, ErrorCode.INTERNAL_ERROR)
                    self.assertTrue(process.group_exists)
                    self.assertIsNone(self.repo.load().backend_process)
                    self.assertIsNone(self.supervisor._active)
                    self.assertEqual(self.exits, [])

    async def test_stop_retry_rechecks_identity_between_term_and_kill(self):
        process = IdentityRetryProcess(self.root / "backend" / "main.py")
        async with self.retry_backend(process) as handle:
            process.allow_signals = True
            process.replace_on_term = True
            results = await asyncio.wait_for(asyncio.gather(
                self.supervisor.stop(handle, StopReason.OPERATOR), return_exceptions=True), 0.7)
            self.assertEqual(process.replacement_signals, [])
            self.assertIsInstance(results[0], SmartAppError)
            self.assertEqual(process.signals[-1], (process.pid, signal.SIGTERM))
            self.assertTrue(process.group_exists)
            self.assertIsNone(self.repo.load().backend_process)
            self.assertIsNone(self.supervisor._active)

    async def test_stop_retry_cleans_original_descendants_after_leader_exit(self):
        process = IdentityRetryProcess(self.root / "backend" / "main.py")
        async with self.retry_backend(process) as handle:
            process.leader_exit()
            process.allow_signals = True
            await asyncio.wait_for(self.supervisor.stop(handle, StopReason.OPERATOR), 0.7)
            self.assertEqual(process.signals[-2:], [(process.pid, signal.SIGTERM), (process.pid, signal.SIGKILL)])
            self.assertEqual(process.replacement_signals, [])
            self.assertFalse(process.group_exists)
            self.assertIsNone(self.repo.load().backend_process)
            self.assertIsNone(self.supervisor._active)

    async def test_term_and_kill_fallback_and_concurrent_idempotent_stop(self):
        for mode in ("term", "kill"):
            with self.subTest(mode=mode):
                handle = await self.start({"mode": mode})
                await asyncio.gather(*(self.supervisor.stop(handle, StopReason.OPERATOR) for _ in range(8)))
                self.assertFalse(self.pid_exists(handle.pid))
                if mode == "term":
                    self.assertEqual((self.root / "terminated").read_text(), "term")
                self.assertIsNone(self.repo.load().backend_process)
                with self.assertRaises(SmartAppError):
                    await self.send(handle, {})

    async def test_process_group_cleanup_kills_recorded_child(self):
        handle = await self.start(fixture="tree_backend.py")
        child_pid = int((self.root / "child.pid").read_text())
        self.pids.add(child_pid)
        self.assertTrue(self.pid_exists(child_pid))
        await self.supervisor.stop(handle, StopReason.OPERATOR)
        await self.wait_until(lambda: not self.pid_exists(child_pid))
        self.assertFalse(self.pid_exists(handle.pid))

    async def test_persist_failure_reaps_new_process_before_raising(self):
        def fail(identity):
            self.pids.add(identity.pid)
            raise OSError("secret")
        with patch.object(self.repo, "set_backend_process", side_effect=fail):
            with self.assertRaises(SmartAppError) as caught:
                await self.start()
        self.assertEqual(caught.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(self.pids)
        self.assertTrue(all(not self.pid_exists(pid) for pid in self.pids))
        self.assertFalse((self.root / "init.json").exists(), "no protocol data before ownership persists")

    async def test_start_timeout_includes_blocked_runtime_init_drain(self):
        self.supervisor.config = replace(self.config,
            limits=replace(self.config.limits, max_message_bytes=2000000),
            process=replace(self.config.process, env_passthrough=("FIXTURE_NO_READ",)))
        self.supervisor.environment = {"FIXTURE_NO_READ": "yes"}
        with self.assertRaises(SmartAppError) as caught:
            await asyncio.wait_for(self.start({"blob": "x" * 1000000}), 1.5)
        self.assertEqual(caught.exception.code, ErrorCode.START_TIMEOUT)

    async def test_cancelled_start_and_cancelled_stop_still_reap(self):
        starter = asyncio.create_task(self.start({"mode": "timeout"}))
        await self.wait_until(lambda: (self.root / "leader.pid").exists())
        self.pids.add(int((self.root / "leader.pid").read_text()))
        starter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await starter
        self.assertTrue(all(not self.pid_exists(pid) for pid in self.pids))
        handle = await self.start({"mode": "kill"})
        stopper = asyncio.create_task(self.supervisor.stop(handle, StopReason.OPERATOR))
        await asyncio.sleep(0)
        stopper.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopper
        await self.supervisor.stop(handle, StopReason.OPERATOR)
        self.assertFalse(self.pid_exists(handle.pid))

    async def test_stop_waits_for_in_progress_unexpected_exit_callback(self):
        entered, finish = asyncio.Event(), asyncio.Event()
        async def on_exit(handle, error):
            entered.set()
            await finish.wait()
            self.exits.append((handle, error))
        handle = await self.start({"mode": "exit"}, on_exit=on_exit)
        await asyncio.wait_for(entered.wait(), 2)
        stopper = asyncio.create_task(self.supervisor.stop(handle, StopReason.OPERATOR))
        try:
            await asyncio.sleep(0.02)
            self.assertFalse(stopper.done(), "stop returned before shared cleanup finished")
        finally:
            finish.set()
            await stopper
            await self.wait_until(lambda: self.exits)

    async def test_exit_callback_can_stop_its_own_handle_without_deadlock(self):
        async def on_exit(handle, error):
            timer = self.loop.call_later(0.2, asyncio.current_task().cancel)
            try:
                await self.supervisor.stop(handle, StopReason.OPERATOR)
                self.exits.append("stopped")
            except asyncio.CancelledError:
                self.exits.append("deadlock")
            finally:
                timer.cancel()
        await self.start({"mode": "exit"}, on_exit=on_exit)
        await self.wait_until(lambda: self.exits)
        self.assertEqual(self.exits, ["stopped"])

    async def test_event_is_delivered_after_start_returns_and_init_is_copied(self):
        returned = False
        def event_callback(event):
            self.assertTrue(returned)
            self.events.append(event)
        initial = {"mode": "report", "nested": {"value": "original"}}
        async def start_and_mark():
            nonlocal returned
            handle = await self.start(initial, on_event=event_callback)
            returned = True
            return handle
        starter = asyncio.create_task(start_and_mark())
        await self.wait_until(lambda: (self.root / "leader.pid").exists())
        initial["nested"]["value"] = "changed"
        await starter
        await self.wait_until(lambda: self.events or self.exits)
        self.assertEqual(self.exits, [])
        self.assertEqual(self.events[0].data["init"]["data"]["nested"], {"value": "original"})

    async def test_broken_stdin_still_cleans_up(self):
        handle = await self.start({"mode": "closed_stdin"})
        await self.wait_until(lambda: (self.root / "stdin.closed").exists())
        try:
            await self.send(handle, {"x": 1})
        except SmartAppError as error:
            self.assertEqual(error.code, ErrorCode.BACKEND_EXITED)
        await self.supervisor.stop(handle, StopReason.OPERATOR)
        self.assertFalse(self.pid_exists(handle.pid))

    async def test_failed_term_does_not_skip_kill_reap_and_identity_clear(self):
        handle = await self.start({"mode": "kill"})
        original = os.killpg
        def signal_group(pid, signum):
            self.assertEqual(pid, handle.pid)
            if signum == signal.SIGTERM:
                raise PermissionError("secret")
            return original(pid, signum)
        with patch("smartapp_runtime.infrastructure.processes.supervisor.os.killpg", side_effect=signal_group):
            with self.assertRaises(SmartAppError):
                await self.supervisor.stop(handle, StopReason.OPERATOR)
        self.assertFalse(self.pid_exists(handle.pid))
        self.assertIsNone(self.repo.load().backend_process)

    async def test_linux_identity_requires_exact_entry_and_nonempty_start_time(self):
        for observation in (None, ObservedProcessIdentity("", (str(self.root / "backend/main.py"),)),
                            ObservedProcessIdentity("123", (str(self.root / "backend/main.py") + "-other",))):
            with patch("smartapp_runtime.infrastructure.processes.supervisor.sys", SimpleNamespace(platform="linux")):
                with patch("smartapp_runtime.infrastructure.processes.supervisor.LinuxProcessIdentityReader") as reader:
                    def observe(pid):
                        self.pids.add(pid)
                        return observation
                    reader.return_value.read.side_effect = observe
                    with self.assertRaises(SmartAppError) as caught:
                        await self.start()
            self.assertEqual(caught.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertTrue(all(not self.pid_exists(pid) for pid in self.pids))

    async def test_stop_cancels_blocked_callback_and_drains_full_stdout(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def callback(event):
            entered.set()
            await release.wait()
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always", ResourceWarning)
            handle = await self.start({"mode": "flood"}, on_event=callback)
            await asyncio.wait_for(entered.wait(), 1)
            # Give the bounded subprocess transport time to fill behind the callback.
            await asyncio.sleep(0.03)
            try:
                await asyncio.wait_for(self.supervisor.stop(handle, StopReason.OPERATOR), 1)
            finally:
                release.set()
                await self.supervisor.stop(handle, StopReason.OPERATOR)
            await asyncio.sleep(0)
            gc.collect()
        self.assertEqual([str(item.message) for item in captured if item.category is ResourceWarning], [])
        self.assertFalse(self.pid_exists(handle.pid))

    async def test_leader_exit_still_cleans_descendants_holding_pipes(self):
        handle = await self.start({"mode": "leader_exit"}, "tree_backend.py")
        child = int((self.root / "child.pid").read_text())
        self.pids.add(child)
        await self.wait_until(lambda: self.exits)
        await self.wait_until(lambda: not self.pid_exists(child))
        self.assertEqual(self.exits[0][1].code, ErrorCode.BACKEND_EXITED)
        self.assertFalse(self.pid_exists(handle.pid))

    async def test_callback_cancelled_error_is_session_failure(self):
        async def callback(event):
            raise asyncio.CancelledError()
        handle = await self.start(on_event=callback)
        await self.send(handle, {})
        await self.wait_until(lambda: self.exits)
        self.assertEqual(self.exits[0][1].code, ErrorCode.INTERNAL_ERROR)

    async def test_disabled_symlink_and_external_entry_are_rejected(self):
        installed = self.installed()
        bad = replace(installed, manifest=replace(installed.manifest,
            backend=replace(installed.manifest.backend, enabled=False)))
        with self.assertRaises(SmartAppError):
            await self.supervisor.start(bad, self.session, {}, self.events.append, lambda *args: None)
        entry = self.root / "backend" / "main.py"
        entry.unlink()
        entry.symlink_to(FIXTURES / "ready_backend.py")
        with self.assertRaises(SmartAppError):
            await self.supervisor.start(installed, self.session, {}, self.events.append, lambda *args: None)
        self.assertFalse((self.root / "leader.pid").exists())
