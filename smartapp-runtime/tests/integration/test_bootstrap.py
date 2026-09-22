import asyncio
import contextlib
import io
import signal
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.bootstrap import (
    RuntimeAssembly,
    _install_signal_handlers,
    _port_available,
    build_runtime,
    run_runtime,
)
from smartapp_runtime.config import (
    LoggingConfig,
    NetworkConfig,
    PathsConfig,
    RendererConfig,
    RuntimeConfig,
)
from smartapp_runtime.__main__ import main
from smartapp_runtime.adapters.fake_renderer import FakeRenderer
from smartapp_runtime.adapters.process_renderer import ProcessRenderer


class _SyncComponent:
    def __init__(self, events, start_name, stop_name, fail_start=False, fail_stop=False):
        self.events = events
        self.start_name = start_name
        self.stop_name = stop_name
        self.fail_start = fail_start
        self.fail_stop = fail_stop

    def start(self):
        self.events.append(self.start_name)
        if self.fail_start:
            raise RuntimeError(self.start_name + " failed")

    def stop(self):
        self.events.append(self.stop_name)
        if self.fail_stop:
            raise RuntimeError(self.stop_name + " failed")


class _AsyncComponent(_SyncComponent):
    async def start(self):
        super().start()

    async def stop(self):
        super().stop()

    async def close(self):
        self.events.append(self.stop_name)
        if self.fail_stop:
            raise RuntimeError(self.stop_name + " failed")


class _Paths:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail

    def ensure_layout(self):
        self.events.append("layout")
        if self.fail:
            raise RuntimeError("layout failed")


class _Recovery:
    def __init__(self, events, fail=False):
        self.events = events
        self.fail = fail

    async def recover(self):
        self.events.append("recovery")
        if self.fail:
            raise RuntimeError("recovery failed")


def _fake_assembly(events, fail_at=None, fail_stop=None):
    lock = _SyncComponent(events, "lock", "unlock", fail_at == "lock", fail_stop == "lock")
    lock.acquire = lock.start
    lock.release = lock.stop
    static = _SyncComponent(events, "static", "static-stop", fail_at == "static", fail_stop == "static")
    static.owns_resources = fail_at == "static"
    coordinator = _AsyncComponent(
        events, "coordinator", "coordinator-close",
        fail_at == "coordinator", fail_stop == "coordinator",
    )
    agent = _AsyncComponent(events, "agent", "agent-stop", fail_at == "agent", fail_stop == "agent")
    return RuntimeAssembly(
        config=RuntimeConfig(),
        paths=_Paths(events, fail_at == "layout"),
        instance_lock=lock,
        repository=object(), pointers=object(), downloader=object(), installer=object(),
        static_server=static, supervisor=object(), renderer=object(), publisher=object(),
        router=object(), coordinator=coordinator,
        recovery=_Recovery(events, fail_at == "recovery"), agent_server=agent,
    )


class RuntimeAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_startup_and_shutdown_order(self):
        events = []
        assembly = _fake_assembly(events)

        with patch("smartapp_runtime.bootstrap.configure_logging", side_effect=lambda _c: events.append("logging")):
            await assembly.start()
            await assembly.stop()

        self.assertEqual(events, [
            "lock", "layout", "logging", "recovery", "static", "coordinator", "agent",
            "agent-stop", "coordinator-close", "static-stop", "unlock",
        ])

    async def test_every_partial_start_failure_rolls_back_only_started_resources(self):
        expected = {
            "lock": ["lock"],
            "layout": ["lock", "layout", "unlock"],
            "logging": ["lock", "layout", "logging", "unlock"],
            "recovery": ["lock", "layout", "logging", "recovery", "unlock"],
            "static": [
                "lock", "layout", "logging", "recovery", "static",
                "static-stop", "unlock",
            ],
            "coordinator": [
                "lock", "layout", "logging", "recovery", "static", "coordinator",
                "static-stop", "unlock",
            ],
            "agent": [
                "lock", "layout", "logging", "recovery", "static", "coordinator", "agent",
                "coordinator-close", "static-stop", "unlock",
            ],
        }
        for fail_at, wanted in expected.items():
            with self.subTest(fail_at=fail_at):
                events = []
                assembly = _fake_assembly(events, fail_at=fail_at)

                def logging_configurer(_config):
                    events.append("logging")
                    if fail_at == "logging":
                        raise RuntimeError("logging failed")

                with patch("smartapp_runtime.bootstrap.configure_logging", side_effect=logging_configurer):
                    with self.assertRaisesRegex(RuntimeError, fail_at + " failed"):
                        await assembly.start()
                self.assertEqual(events, wanted)

    async def test_cleanup_continues_after_error_and_stop_is_idempotent(self):
        events = []
        assembly = _fake_assembly(events, fail_stop="agent")
        with patch("smartapp_runtime.bootstrap.configure_logging", side_effect=lambda _c: events.append("logging")):
            await assembly.start()
            with self.assertRaisesRegex(RuntimeError, "agent-stop failed"):
                await assembly.stop()
            assembly.agent_server.fail_stop = False
            await assembly.stop()

        self.assertEqual(events[-5:], [
            "agent-stop", "coordinator-close", "static-stop", "unlock", "agent-stop",
        ])

    async def test_startup_primary_error_is_not_masked_by_cleanup_error(self):
        events = []
        assembly = _fake_assembly(events, fail_at="agent", fail_stop="coordinator")
        with patch("smartapp_runtime.bootstrap.configure_logging", side_effect=lambda _c: events.append("logging")):
            with self.assertRaisesRegex(RuntimeError, "agent failed"):
                await assembly.start()
        self.assertIn("coordinator-close", events)
        self.assertIn("unlock", events)


class ConcreteAssemblyTests(unittest.IsolatedAsyncioTestCase):
    def _config(self, root, static_port=18080, backend_port=18081):
        return RuntimeConfig(
            paths=PathsConfig(
                root=root,
                socket=root / "run" / "runtime.sock",
                log=root / "logs" / "runtime.jsonl",
                python_executable=Path("/usr/bin/python3"),
            ),
            network=NetworkConfig(static_port=static_port, backend_port=backend_port),
            logging=LoggingConfig(target="stderr"),
        )

    async def test_builds_two_independent_graphs_without_filesystem_side_effects(self):
        with tempfile.TemporaryDirectory() as raw:
            first_root = Path(raw) / "first"
            second_root = Path(raw) / "second"
            first = build_runtime(self._config(first_root))
            second = build_runtime(self._config(second_root))

            self.assertIsNot(first, second)
            self.assertIsNot(first.coordinator, second.coordinator)
            self.assertIsNot(first.publisher, second.publisher)
            self.assertEqual(first.paths.root, first_root.parent.resolve() / first_root.name)
            self.assertEqual(second.paths.root, second_root.parent.resolve() / second_root.name)
            self.assertFalse(first_root.exists())
            self.assertFalse(second_root.exists())
            self.assertIsInstance(first.renderer, FakeRenderer)

            await first.renderer.load(
                "http://127.0.0.1/app", __import__(
                    "smartapp_runtime.domain.models", fromlist=["Session"]
                ).Session("session-1", "demo", "1", 1),
            )
            await first.renderer.wait_ready(0.05)

    async def test_builds_process_renderer_from_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runtime"
            config = self._config(root)
            config = RuntimeConfig(
                paths=config.paths,
                network=config.network,
                logging=config.logging,
                renderer=RendererConfig(
                    kind="process",
                    process_argv=(sys.executable, "-c", "pass"),
                    restore_argv=(sys.executable, "-c", "pass"),
                ),
            )
            self.assertIsInstance(build_runtime(config).renderer, ProcessRenderer)

    async def test_port_probe_reports_exact_bound_ipv4_port(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        self.assertFalse(_port_available("127.0.0.1", port))
        listener.close()
        self.assertTrue(_port_available("127.0.0.1", port))

    async def test_port_probe_allows_rebind_during_time_wait(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        client = socket.create_connection(("127.0.0.1", port))
        connection, _ = listener.accept()
        connection.close()
        client.recv(1)
        client.close()
        listener.close()

        self.assertTrue(_port_available("127.0.0.1", port))


class SignalAndCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_installed_signal_handlers_set_event_and_are_removable(self):
        class Loop:
            def __init__(self):
                self.callbacks = {}
                self.removed = []

            def add_signal_handler(self, signum, callback):
                self.callbacks[signum] = callback

            def remove_signal_handler(self, signum):
                self.removed.append(signum)
                return True

        loop = Loop()
        event = asyncio.Event()
        remove = _install_signal_handlers(loop, event)

        loop.callbacks[signal.SIGTERM]()
        self.assertTrue(event.is_set())
        remove()
        self.assertEqual(loop.removed, [signal.SIGINT, signal.SIGTERM])

    async def test_run_runtime_stops_on_cancellation_and_propagates_it(self):
        class Assembly:
            def __init__(self):
                self.started = asyncio.Event()
                self.stopped = False

            async def start(self):
                self.started.set()

            async def stop(self):
                self.stopped = True

        assembly = Assembly()
        with patch("smartapp_runtime.bootstrap.build_runtime", return_value=assembly):
            task = asyncio.create_task(run_runtime(RuntimeConfig()))
            await assembly.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(assembly.stopped)

    async def test_run_runtime_cancellation_is_not_masked_by_shutdown_failure(self):
        class Assembly:
            def __init__(self):
                self.started = asyncio.Event()

            async def start(self):
                self.started.set()

            async def stop(self):
                raise RuntimeError("shutdown failed")

        assembly = Assembly()
        with patch("smartapp_runtime.bootstrap.build_runtime", return_value=assembly):
            task = asyncio.create_task(run_runtime(RuntimeConfig()))
            await assembly.started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_run_runtime_start_failure_is_not_masked_by_shutdown_failure(self):
        class Assembly:
            async def start(self):
                raise RuntimeError("startup failed")

            async def stop(self):
                raise RuntimeError("shutdown failed")

        with patch("smartapp_runtime.bootstrap.build_runtime", return_value=Assembly()):
            with self.assertRaisesRegex(RuntimeError, "startup failed"):
                await run_runtime(RuntimeConfig())

    async def test_check_config_has_no_runtime_filesystem_side_effects(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runtime"
            config_path = Path(raw) / "runtime.toml"
            config_path.write_text('[paths]\nroot = "' + str(root) + '"\n', encoding="utf-8")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                result = main(["--config", str(config_path), "--check-config"])

            self.assertEqual(result, 0)
            self.assertFalse(root.exists())
            self.assertIn("valid", stdout.getvalue().lower())
            self.assertEqual(stderr.getvalue(), "")

    async def test_invalid_config_is_concise_and_has_no_traceback(self):
        with tempfile.TemporaryDirectory() as raw:
            config_path = Path(raw) / "bad.toml"
            config_path.write_text("not = [valid", encoding="utf-8")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = main(["--config", str(config_path), "--check-config"])

            self.assertNotEqual(result, 0)
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(len(stderr.getvalue().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
