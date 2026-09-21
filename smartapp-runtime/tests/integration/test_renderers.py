import asyncio
import json
import math
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path

from smartapp_runtime.adapters.command_renderer import CommandRenderer
from smartapp_runtime.adapters.fake_renderer import FakeRenderer
from smartapp_runtime.adapters.process_renderer import ProcessRenderer
from smartapp_runtime.config import RendererConfig
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session


class FakeRendererTests(unittest.IsolatedAsyncioTestCase):
    async def test_optional_auto_ready_mode_supports_hardware_free_runtime(self):
        renderer = FakeRenderer(auto_ready=True)
        await renderer.load(
            "http://127.0.0.1/app", Session("session-1", "demo_app", "1.0.0", 1)
        )

        await renderer.wait_ready(0.01)

    async def test_records_defensive_copies_and_emits_sync_or_async_messages(self):
        renderer = FakeRenderer()
        session = Session("session-1", "demo_app", "1.0.0", 1)
        received = []

        async def handler(message):
            received.append(message)

        renderer.set_message_handler(handler)
        await renderer.load("http://127.0.0.1/app", session)
        renderer.mark_ready()
        await renderer.wait_ready(0.1)
        message = {"event": "cloud_data", "seq": 1, "data": {"value": [1]}}
        await renderer.send(message)
        message["data"]["value"].append(2)
        emitted = {"event": "app_data", "dataType": "x", "data": {}}
        await renderer.emit_message(emitted)

        self.assertEqual(renderer.load_calls, [("http://127.0.0.1/app", session)])
        self.assertEqual(renderer.sent_messages[0]["data"], {"value": [1]})
        self.assertEqual(received, [emitted])

    async def test_explicit_failure_controls_and_idempotent_lifecycle(self):
        renderer = FakeRenderer()
        session = Session("session-1", "demo_app", "1.0.0", 1)
        await renderer.load("http://127.0.0.1/app", session)
        renderer.fail_ready()
        with self.assertRaises(SmartAppError) as raised:
            await renderer.wait_ready(0.1)
        self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)

        await renderer.stop()
        await renderer.stop()
        await renderer.restore_default()
        await renderer.restore_default()
        self.assertEqual(renderer.stop_calls, 1)
        self.assertEqual(renderer.restore_calls, 1)

    async def test_wait_ready_timeout_and_callback_failure_are_observed(self):
        renderer = FakeRenderer()
        await renderer.load(
            "http://127.0.0.1/app", Session("session-1", "demo_app", "1.0.0", 1)
        )
        with self.assertRaises(SmartAppError) as timeout:
            await renderer.wait_ready(0.01)
        self.assertEqual(timeout.exception.code, ErrorCode.RENDERER_FAILED)

        def handler(_message):
            raise RuntimeError("callback failed")

        renderer.set_message_handler(handler)
        with self.assertRaises(SmartAppError) as callback:
            await renderer.emit_message({"event": "app_data", "dataType": "x", "data": {}})
        self.assertEqual(callback.exception.code, ErrorCode.RENDERER_FAILED)


class ProcessRendererTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = Session("session-1", "demo_app", "1.0.0", 1)
        self.script_number = 0

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, script, restore=None):
        self.script_number += 1
        script_path = self.root / "renderer-{0}.py".format(self.script_number)
        script_path.write_text(script)
        return RendererConfig(
            kind="process",
            process_argv=(sys.executable, "-u", str(script_path), "{url}", "{session_id}"),
            restore_argv=tuple(restore or (sys.executable, "-c", "pass")),
        )

    async def test_persistent_jsonl_bridge_is_bidirectional_and_stops_cleanly(self):
        script = """import json
import sys

print(json.dumps({"event": "renderer_ready"}), flush=True)
for raw in sys.stdin:
    message = json.loads(raw)
    if message.get("event") == "renderer_stop":
        break
    print(json.dumps({
        "event": "app_data", "dataType": "echo", "data": message,
    }), flush=True)
"""
        renderer = ProcessRenderer(self.config(script), command_timeout=1.0)
        received = []
        delivered = asyncio.Event()

        async def handler(message):
            received.append(message)
            delivered.set()

        renderer.set_message_handler(handler)
        await renderer.load("http://127.0.0.1/app", self.session)
        await renderer.wait_ready(1.0)
        await renderer.send({"event": "cloud_data", "seq": 1, "data": {"word": "苹果"}})
        await asyncio.wait_for(delivered.wait(), 1.0)
        self.assertEqual(received[0]["data"]["data"], {"word": "苹果"})
        await renderer.stop()
        await renderer.stop()
        await renderer.restore_default()
        await renderer.restore_default()

    async def test_rejects_invalid_first_frame_and_embedded_placeholders(self):
        bad = "import time\nprint('not-json', flush=True)\ntime.sleep(60)\n"
        renderer = ProcessRenderer(self.config(bad), command_timeout=0.05)
        await renderer.load("http://127.0.0.1/app", self.session)
        with self.assertRaises(SmartAppError) as raised:
            await renderer.wait_ready(1.0)
        self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)
        await renderer.stop()

        config = self.config("pass")
        config = RendererConfig(
            kind="process",
            process_argv=(sys.executable, "prefix-{url}"),
            restore_argv=config.restore_argv,
        )
        with self.assertRaises(SmartAppError) as placeholder:
            ProcessRenderer(config)
        self.assertEqual(placeholder.exception.code, ErrorCode.VALIDATION_ERROR)

    async def test_timeout_terminates_owned_process(self):
        script = """import json
import signal
import time

signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({"event": "renderer_ready"}), flush=True)
time.sleep(60)
"""
        renderer = ProcessRenderer(self.config(script), command_timeout=0.03)
        await renderer.load("http://127.0.0.1/app", self.session)
        await renderer.wait_ready(1.0)
        pid = renderer._process.pid
        await renderer.stop()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_restore_failure_is_sanitized(self):
        renderer = ProcessRenderer(self.config(
            "import json\nprint(json.dumps({'event': 'renderer_ready'}), flush=True)\n",
            restore=(sys.executable, "-c", "raise SystemExit(7)", "secret"),
        ))
        with self.assertRaises(SmartAppError) as raised:
            await renderer.restore_default()
        self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)
        self.assertNotIn("secret", raised.exception.message)


class CommandRendererTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.session = Session("session-1", "demo_app", "1.0.0", 1)

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, load_argv, send_argv=None, stop_argv=None, restore_argv=None):
        success = (sys.executable, "-c", "pass")
        return RendererConfig(
            kind="command",
            load_argv=tuple(load_argv),
            send_argv=tuple(send_argv or success),
            stop_argv=tuple(stop_argv or success),
            restore_argv=tuple(restore_argv or success),
        )

    async def test_load_substitutes_only_whole_value_placeholders(self):
        output = self.root / "load.json"
        script = "import json,sys,pathlib; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))"
        renderer = CommandRenderer(self.config(
            (sys.executable, "-c", script, str(output), "{url}", "{session_id}", "{app_id}", "{version}")
        ))

        await renderer.load("http://127.0.0.1/app?a=1", self.session)
        await renderer.wait_ready(2.0)

        self.assertEqual(json.loads(output.read_text()), [
            "http://127.0.0.1/app?a=1", "session-1", "demo_app", "1.0.0"
        ])

    async def test_rejects_embedded_and_unknown_placeholders(self):
        for bad in ("prefix-{url}", "{unknown}", "plain{value}"):
            with self.subTest(bad=bad), self.assertRaises(SmartAppError) as raised:
                CommandRenderer(self.config((sys.executable, "-c", "pass", bad)))
            self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    async def test_rejects_placeholders_in_context_free_stop_and_restore_commands(self):
        success = (sys.executable, "-c", "pass")
        for field in ("stop_argv", "restore_argv"):
            values = {field: (sys.executable, "-c", "pass", "{session_id}")}
            with self.subTest(field=field):
                with self.assertRaises(SmartAppError) as raised:
                    CommandRenderer(self.config(success, **values))
                self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    async def test_rejects_invalid_command_timeout(self):
        for timeout in (True, 0, -1, math.inf, math.nan):
            with self.subTest(timeout=timeout):
                with self.assertRaises(SmartAppError) as raised:
                    CommandRenderer(
                        self.config((sys.executable, "-c", "pass")),
                        command_timeout=timeout,
                    )
                self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    async def test_send_writes_compact_utf8_json_only_to_stdin(self):
        output = self.root / "payload.bin"
        script = "import sys,pathlib; pathlib.Path(sys.argv[1]).write_bytes(sys.stdin.buffer.read())"
        renderer = CommandRenderer(self.config(
            (sys.executable, "-c", "pass"),
            send_argv=(sys.executable, "-c", script, str(output)),
        ))
        await renderer.load("http://127.0.0.1/app", self.session)
        await renderer.wait_ready(2.0)

        await renderer.send({"event": "cloud_data", "seq": 1, "data": {"word": "苹果"}})

        self.assertEqual(
            output.read_bytes(),
            '{"event":"cloud_data","seq":1,"data":{"word":"苹果"}}\n'.encode("utf-8"),
        )

    async def test_nonzero_spawn_failure_and_timeout_map_to_sanitized_renderer_error(self):
        cases = (
            ((sys.executable, "-c", "raise SystemExit(7)", "secret-arg"), 1.0),
            ((str(self.root / "missing-secret-program"),), 1.0),
            ((sys.executable, "-c", "import time; time.sleep(60)", "secret-arg"), 0.02),
        )
        for argv, timeout in cases:
            with self.subTest(argv=argv):
                renderer = CommandRenderer(self.config(argv))
                await renderer.load("http://user:secret@example.test/app", self.session)
                with self.assertRaises(SmartAppError) as raised:
                    await renderer.wait_ready(timeout)
                self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)
                self.assertNotIn("secret", raised.exception.message)

    async def test_noisy_output_is_drained_and_bounded_without_deadlock(self):
        script = "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)"
        renderer = CommandRenderer(
            self.config((sys.executable, "-c", script)), max_output_bytes=1024
        )
        await renderer.load("http://127.0.0.1/app", self.session)
        await renderer.wait_ready(2.0)

    async def test_stop_and_restore_are_idempotent_after_success_but_retry_failures(self):
        stop_count = self.root / "stop-count"
        restore_count = self.root / "restore-count"
        script = (
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "p.write_text(str(int(p.read_text())+1) if p.exists() else '1')"
        )
        renderer = CommandRenderer(self.config(
            (sys.executable, "-c", "pass"),
            stop_argv=(sys.executable, "-c", script, str(stop_count)),
            restore_argv=(sys.executable, "-c", script, str(restore_count)),
        ))
        await renderer.stop()
        await renderer.stop()
        await renderer.restore_default()
        await renderer.restore_default()
        self.assertEqual(stop_count.read_text(), "1")
        self.assertEqual(restore_count.read_text(), "1")

        failed = CommandRenderer(self.config(
            (sys.executable, "-c", "pass"),
            stop_argv=(sys.executable, "-c", "raise SystemExit(1)"),
        ))
        for _ in range(2):
            with self.assertRaises(SmartAppError) as raised:
                await failed.stop()
            self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)

    async def test_send_stop_and_restore_commands_have_bounded_sanitized_timeout(self):
        hanging = (sys.executable, "-c", "import time; time.sleep(60)", "secret-arg")
        cases = ("send", "stop", "restore")
        for operation in cases:
            with self.subTest(operation=operation):
                overrides = {"{0}_argv".format(operation): hanging}
                renderer = CommandRenderer(
                    self.config((sys.executable, "-c", "pass"), **overrides),
                    command_timeout=0.03,
                )
                if operation == "send":
                    await renderer.load("http://127.0.0.1/app", self.session)
                    await renderer.wait_ready(1.0)
                    invocation = renderer.send({"event": "cloud_data", "seq": 1, "data": {}})
                elif operation == "stop":
                    invocation = renderer.stop()
                else:
                    invocation = renderer.restore_default()
                started = asyncio.get_running_loop().time()
                with self.assertRaises(SmartAppError) as raised:
                    await asyncio.wait_for(invocation, 0.5)
                elapsed = asyncio.get_running_loop().time() - started
                self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)
                self.assertNotIn("secret", raised.exception.message)
                self.assertLess(elapsed, 1.0)

    async def _wait_until_gone(self, pid, timeout=0.5):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            await asyncio.sleep(0.01)
        self.fail("owned renderer descendant is still alive: {0}".format(pid))

    @unittest.skipUnless(os.name == "posix", "POSIX process-group behavior")
    async def test_timeout_and_nonzero_exit_clean_owned_descendants_with_inherited_pipes(self):
        child_script = "import time; time.sleep(1.2)"
        parent_script = (
            "import pathlib,subprocess,sys,time; "
            "p=subprocess.Popen([sys.executable,'-c',sys.argv[2]]); "
            "pathlib.Path(sys.argv[1]).write_text(str(p.pid)); "
            "time.sleep(1.2) if sys.argv[3]=='hang' else sys.exit(7)"
        )
        for mode, timeout in (("hang", 0.08), ("exit", 2.0)):
            pid_path = self.root / "{0}-child.pid".format(mode)
            argv = (
                sys.executable, "-c", parent_script, str(pid_path), child_script, mode
            )
            renderer = CommandRenderer(self.config(argv))
            owned_pid = None
            started = asyncio.get_running_loop().time()
            try:
                await renderer.load("http://127.0.0.1/app", self.session)
                with self.assertRaises(SmartAppError) as raised:
                    await renderer.wait_ready(timeout)
                elapsed = asyncio.get_running_loop().time() - started
                self.assertEqual(raised.exception.code, ErrorCode.RENDERER_FAILED)
                self.assertLess(elapsed, 0.7)
                self.assertTrue(pid_path.exists())
                owned_pid = int(pid_path.read_text())
                await self._wait_until_gone(owned_pid)
            finally:
                if owned_pid is None and pid_path.exists():
                    owned_pid = int(pid_path.read_text())
                if owned_pid is not None:
                    try:
                        os.kill(owned_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
