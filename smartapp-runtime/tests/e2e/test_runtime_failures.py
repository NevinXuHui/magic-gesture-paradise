import os
import unittest

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.ports.repository import (
    BackendProcessIdentity,
    PersistedRuntimeState,
)

from helpers.package_factory import build_fixture, build_unsafe_path_archive
from helpers.runtime_harness import RuntimeHarness


class _RecordingSignalSender:
    def __init__(self):
        self.calls = []

    def send_group(self, pgid, signal_number):
        self.calls.append((pgid, signal_number))


class RuntimeFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_wrong_declared_sha_is_rejected_and_idles(self):
        package = build_fixture("web_only", app_id="bad_hash")
        url = "memory://bad-hash"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            command = RuntimeHarness.start_command(
                "bad-hash", "bad-hash-session", "bad_hash", "1", url, package
            )
            command["sha256"] = "0" * 64
            result = await client.command(command)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], ErrorCode.HASH_MISMATCH.value)
            self.assertEqual(result["state"], RuntimeState.IDLE.value)
            self.assertIsNone(harness.assembly.pointers.current_target("bad_hash"))
        finally:
            await harness.stop()

    async def test_hostile_archive_path_is_rejected_without_escape(self):
        package = build_unsafe_path_archive("unsafe_app")
        url = "memory://unsafe"
        harness = RuntimeHarness({url: package.data})
        escaped = harness.root / "tmp" / "escaped.txt"
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "unsafe", "unsafe-session", "unsafe_app", "1", url, package
            ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], ErrorCode.ARCHIVE_UNSAFE.value)
            self.assertEqual(result["state"], RuntimeState.IDLE.value)
            self.assertFalse(escaped.exists())
        finally:
            await harness.stop()

    async def test_backend_startup_timeout_reaps_process_and_rolls_back(self):
        package = build_fixture("python_only", app_id="slow_app")
        url = "memory://slow"
        harness = RuntimeHarness({url: package.data}, startup_timeout=0.12)
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "slow", "slow-session", "slow_app", "1", url, package,
                {"mode": "timeout"},
            ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], ErrorCode.START_TIMEOUT.value)
            self.assertEqual(result["state"], RuntimeState.IDLE.value)
            self.assertIsNone(harness.assembly.supervisor._active)
            self.assertIsNone(harness.assembly.pointers.current_target("slow_app"))
            self.assertIsNone(harness.assembly.pointers.current_web_target())
        finally:
            await harness.stop()

    async def test_backend_crash_after_running_cleans_to_idle(self):
        package = build_fixture("python_only", app_id="crash_app")
        url = "memory://crash"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            started = await client.command(RuntimeHarness.start_command(
                "crash-start", "crash-session", "crash_app", "1", url, package
            ))
            self.assertTrue(started["ok"], started)
            await client.wait_for(lambda event: event.get("dataType") == "init")
            sent = await client.command(RuntimeHarness.cloud_command(
                "crash-now", "crash-session", 1, {"action": "crash"}
            ))
            self.assertTrue(sent["ok"], sent)
            await harness.wait_state(RuntimeState.IDLE)
            self.assertIsNone(harness.assembly.supervisor._active)
            persisted = harness.assembly.repository.load()
            self.assertEqual(persisted.last_error["code"], ErrorCode.BACKEND_EXITED.value)
        finally:
            await harness.stop()

    async def test_renderer_load_failure_cleans_and_rolls_back(self):
        package = build_fixture("web_only", app_id="render_app")
        url = "memory://renderer-failure"
        harness = RuntimeHarness({url: package.data})
        harness.assembly.renderer.set_load_error()
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "render-fail", "render-session", "render_app", "1", url, package
            ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], ErrorCode.RENDERER_FAILED.value)
            self.assertEqual(result["state"], RuntimeState.IDLE.value)
            self.assertIsNone(harness.assembly.pointers.current_web_target())
            self.assertGreaterEqual(harness.assembly.renderer.stop_calls, 1)
        finally:
            await harness.stop()

    async def test_renderer_init_delivery_failure_is_startup_fatal(self):
        package = build_fixture("web_only", app_id="render_init_app")
        url = "memory://renderer-init-failure"
        harness = RuntimeHarness({url: package.data})
        harness.assembly.renderer.set_send_error()
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "render-init-fail", "render-init-session", "render_init_app",
                "1", url, package, {"must": "arrive"}
            ))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], ErrorCode.RENDERER_FAILED.value)
            self.assertEqual(result["state"], RuntimeState.IDLE.value)
            self.assertIsNone(harness.assembly.pointers.current_target("render_init_app"))
            self.assertIsNone(harness.assembly.pointers.current_web_target())
        finally:
            await harness.stop()

    async def test_backend_delivery_session_mismatch_is_session_fatal(self):
        package = build_fixture("python_only", app_id="delivery_app")
        url = "memory://delivery-failure"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            started = await client.command(RuntimeHarness.start_command(
                "delivery-start", "delivery-session", "delivery_app", "1",
                url, package
            ))
            self.assertTrue(started["ok"], started)
            await client.wait_for(lambda event: event.get("dataType") == "init")

            async def stale_backend_handle(_handle, _message):
                raise SmartAppError(
                    ErrorCode.SESSION_MISMATCH, "backend handle is not active"
                )

            harness.assembly.supervisor.send = stale_backend_handle
            failed = await client.command(RuntimeHarness.cloud_command(
                "delivery-cloud", "delivery-session", 1, {"value": 1}
            ))
            self.assertFalse(failed["ok"])
            self.assertEqual(
                failed["error"]["code"], ErrorCode.SESSION_MISMATCH.value
            )
            await harness.wait_state(RuntimeState.IDLE)
            self.assertIsNone(harness.assembly.supervisor._active)
        finally:
            await harness.stop()

    async def test_new_session_replaces_old_without_backend_overlap(self):
        package = build_fixture("python_only", app_id="replace_app")
        url = "memory://replace"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            first = await client.command(RuntimeHarness.start_command(
                "replace-first", "replace-session-1", "replace_app", "1",
                url, package
            ))
            self.assertTrue(first["ok"], first)
            old_pid = harness.assembly.supervisor._active.handle.pid
            second = await client.command(RuntimeHarness.start_command(
                "replace-second", "replace-session-2", "replace_app", "1",
                url, package
            ))
            self.assertTrue(second["ok"], second)
            self.assertEqual(second["state"], RuntimeState.RUNNING.value)
            active = harness.assembly.supervisor._active
            self.assertEqual(active.handle.session.session_id, "replace-session-2")
            self.assertNotEqual(active.handle.pid, old_pid)
            with self.assertRaises(ProcessLookupError):
                os.kill(old_pid, 0)
            self.assertEqual(len(harness.downloader.calls), 1)
            stopped = await client.command(RuntimeHarness.stop_command(
                "replace-stop", "replace-session-2"
            ))
            self.assertEqual(stopped["state"], RuntimeState.IDLE.value)
        finally:
            await harness.stop()

    async def test_failed_upgrade_restores_pre_start_pointer_snapshot(self):
        first_package = build_fixture("web_only", app_id="upgrade_app", version="1")
        second_package = build_fixture("web_only", app_id="upgrade_app", version="2")
        first_url = "memory://upgrade-v1"
        second_url = "memory://upgrade-v2"
        harness = RuntimeHarness({
            first_url: first_package.data,
            second_url: second_package.data,
        })
        try:
            client = await harness.start()
            first = await client.command(RuntimeHarness.start_command(
                "upgrade-v1", "upgrade-session-1", "upgrade_app", "1",
                first_url, first_package
            ))
            self.assertTrue(first["ok"], first)
            await client.command(RuntimeHarness.stop_command(
                "upgrade-stop-1", "upgrade-session-1"
            ))
            old_target = harness.assembly.pointers.current_target("upgrade_app")
            self.assertEqual(old_target.name, "1")

            harness.assembly.renderer.set_load_error()
            failed = await client.command(RuntimeHarness.start_command(
                "upgrade-v2", "upgrade-session-2", "upgrade_app", "2",
                second_url, second_package
            ))
            self.assertFalse(failed["ok"])
            self.assertEqual(failed["error"]["code"], ErrorCode.RENDERER_FAILED.value)
            self.assertEqual(
                harness.assembly.pointers.current_target("upgrade_app"), old_target
            )
            self.assertIsNone(harness.assembly.pointers.current_web_target())
        finally:
            await harness.stop()

    async def test_restart_recovery_ignores_unrelated_process_and_returns_idle(self):
        harness = RuntimeHarness({})
        marker = harness.root / "apps" / "recovery_app" / "1" / "backend" / "main.py"
        harness.assembly.paths.ensure_layout()
        marker.parent.mkdir(parents=True)
        marker.write_text("# recovery marker\n", encoding="utf-8")
        harness.assembly.repository.save(PersistedRuntimeState(
            state=RuntimeState.RUNNING,
            active_session=Session("recovery-session", "recovery_app", "1", 4),
            generation=4,
            backend_process=BackendProcessIdentity(
                os.getpid(), "999999999999999999", str(marker)
            ),
        ))
        signals = _RecordingSignalSender()
        harness.assembly.recovery.signal_sender = signals
        try:
            client = await harness.start()
            status = await client.command({
                "requestId": "recovery-status", "command": "get_status"
            })
            self.assertEqual(status["state"], RuntimeState.IDLE.value)
            self.assertEqual(signals.calls, [])
            self.assertGreaterEqual(harness.assembly.renderer.restore_calls, 1)
            self.assertEqual(harness.assembly.repository.load().state, RuntimeState.IDLE)
        finally:
            await harness.stop()

    async def test_old_session_and_out_of_order_sequence_are_stable_errors(self):
        package = build_fixture("web_only", app_id="sequence_app")
        url = "memory://sequence"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            started = await client.command(RuntimeHarness.start_command(
                "sequence-start", "sequence-session", "sequence_app", "1",
                url, package
            ))
            self.assertTrue(started["ok"], started)
            stale = await client.command(RuntimeHarness.cloud_command(
                "sequence-stale", "old-session", 1, {}
            ))
            self.assertFalse(stale["ok"])
            self.assertEqual(stale["error"]["code"], ErrorCode.SESSION_MISMATCH.value)

            accepted = await client.command(RuntimeHarness.cloud_command(
                "sequence-first", "sequence-session", 4, {"value": 1}
            ))
            self.assertTrue(accepted["ok"], accepted)
            duplicate = await client.command(RuntimeHarness.cloud_command(
                "sequence-duplicate", "sequence-session", 4, {"value": 2}
            ))
            self.assertFalse(duplicate["ok"])
            self.assertEqual(
                duplicate["error"]["code"], ErrorCode.SEQ_OUT_OF_ORDER.value
            )
            status = await client.command({
                "requestId": "sequence-status", "command": "get_status"
            })
            self.assertEqual(status["state"], RuntimeState.RUNNING.value)
        finally:
            await harness.stop()

    async def test_starting_queue_overflow_keeps_first_item_and_session_alive(self):
        package = build_fixture("python_only", app_id="queue_app")
        url = "memory://queue"
        harness = RuntimeHarness(
            {url: package.data}, startup_timeout=0.8, max_queue_messages=1
        )
        try:
            client = await harness.start()
            await client.send(RuntimeHarness.start_command(
                "queue-start", "queue-session", "queue_app", "1", url,
                package, {"readyDelay": 0.25},
            ))
            await harness.wait_state(RuntimeState.STARTING)
            first = await client.command(RuntimeHarness.cloud_command(
                "queue-first", "queue-session", 1, {"order": 1}
            ))
            self.assertTrue(first["ok"], first)
            self.assertEqual(first["state"], RuntimeState.STARTING.value)
            overflow = await client.command(RuntimeHarness.cloud_command(
                "queue-overflow", "queue-session", 2, {"order": 2}
            ))
            self.assertFalse(overflow["ok"])
            self.assertEqual(overflow["error"]["code"], ErrorCode.QUEUE_FULL.value)

            started = await client.result("queue-start")
            self.assertTrue(started["ok"], started)
            self.assertEqual(started["state"], RuntimeState.RUNNING.value)
            delivered = await client.wait_for(
                lambda event: event.get("event") == "app_data"
                and event.get("dataType") == "echo"
            )
            self.assertEqual(delivered["data"], {
                "seq": 1, "payload": {"order": 1}
            })
            await client.command(RuntimeHarness.stop_command(
                "queue-stop", "queue-session"
            ))
        finally:
            await harness.stop()


if __name__ == "__main__":
    unittest.main()
