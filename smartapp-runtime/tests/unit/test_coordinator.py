import asyncio
import copy
import gc
import unittest
from dataclasses import replace
from pathlib import Path

from smartapp_runtime.application.coordinator import (
    CommandResult,
    RuntimeCoordinator,
    WorkflowSucceeded,
)
from smartapp_runtime.application.router import DeliveryFailure
from smartapp_runtime.domain.commands import (
    CloudData,
    GetStatus,
    MessageTarget,
    StartApp,
    StopApp,
    StopReason,
)
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.ports.process import BackendEvent, BackendHandle
from smartapp_runtime.ports.repository import PersistedRuntimeState, PointerSnapshot


def start(request_id="start-1", session_id="session-1", version="1.0.0", sha="a" * 64):
    return StartApp(request_id, session_id, "demo_app", version, "https://example.test/app.tgz", 10, sha, {"mode": "test"})


def manifest(web=True, backend=True, dynamic=False):
    default = "web" if web else "python"
    return Manifest.from_dict({
        "schemaVersion": 1,
        "appId": "demo_app",
        "version": "1.0.0",
        "web": {"enabled": web, "entry": "index.html"},
        "backend": {"enabled": backend, "entry": "main.py", "dynamicService": dynamic},
        "routing": {"defaultTarget": default},
    })


class Installed:
    def __init__(self, selected=None, cache_hit=True):
        self.root = Path("/apps/demo_app/1.0.0")
        self.manifest = selected or manifest()
        self.cache_hit = cache_hit


class Installer:
    def __init__(self, installed=None):
        self.installed = installed or Installed()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = None
        self.calls = 0
        self.commands = []

    async def ensure_installed(self, command, progress=None):
        self.calls += 1
        self.commands.append(command)
        self.entered.set()
        await self.release.wait()
        if self.error:
            raise self.error
        if not self.installed.cache_hit and progress is not None:
            for state in (RuntimeState.DOWNLOADING, RuntimeState.VERIFYING, RuntimeState.INSTALLING):
                await progress(state)
        return self.installed


class Repository:
    def __init__(self, initial=None):
        self.value = initial or PersistedRuntimeState(state=RuntimeState.IDLE)
        self.saved = []

    def load(self):
        return self.value

    def save(self, value):
        # Mirror a process supervisor writing identity between coordinator saves.
        self.value = value
        self.saved.append(value)


class Pointers:
    def __init__(self):
        self.snapshot_value = PointerSnapshot("demo_app", "0.9.0", None, "apps/old/web")
        self.calls = []
        self.repository = None
        self.state_at_web_mutation = None

    def snapshot(self, app_id):
        self.calls.append(("snapshot", app_id))
        return self.snapshot_value

    def set_current_web(self, root):
        if self.repository is not None:
            self.state_at_web_mutation = self.repository.value
        self.calls.append(("web", root))

    def set_previous(self, app_id, root):
        self.calls.append(("previous", app_id, root))

    def set_current(self, app_id, root):
        self.calls.append(("current", app_id, root))

    def restore(self, snapshot):
        self.calls.append(("restore", snapshot))

    def current_target(self, app_id):
        return Path("/apps/demo_app/0.9.0")


class Supervisor:
    def __init__(self, repository):
        self.repository = repository
        self.handle = None
        self.on_event = None
        self.on_exit = None
        self.stop_calls = []
        self.start_error = None
        self.immediate_event = None
        self.stop_error_once = None
        self.stop_error_always = None
        self.start_gate = None
        self.ready_gate = None
        self.started = asyncio.Event()
        self.fail_after_owned = None
        self.exit_before_return = None
        self.stop_gate = None
        self.stop_entered = asyncio.Event()

    async def start(self, installed, session, init_data, on_event, on_exit,
                    on_stderr=None, on_owned=None):
        if self.start_gate is not None:
            await self.start_gate.wait()
        if self.start_error:
            raise self.start_error
        self.handle = BackendHandle(321, session, installed.root)
        self.on_event = on_event
        self.on_exit = on_exit
        self.init_data = copy.deepcopy(init_data)
        self.repository.value = replace(self.repository.value, backend_process=object())
        if on_owned is not None:
            on_owned(self.handle)
        self.started.set()
        if self.fail_after_owned is not None:
            raise self.fail_after_owned
        if self.ready_gate is not None:
            await self.ready_gate.wait()
        if self.immediate_event is not None:
            await on_event(self.immediate_event)
        if self.exit_before_return is not None:
            await on_exit(self.handle, self.exit_before_return)
        return self.handle

    async def stop(self, handle, reason):
        self.stop_calls.append((handle, reason))
        self.stop_entered.set()
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_error_once is not None:
            error, self.stop_error_once = self.stop_error_once, None
            raise error
        if self.stop_error_always is not None:
            raise self.stop_error_always
        self.handle = None


class Renderer:
    def __init__(self):
        self.loads = []
        self.ready = asyncio.Event()
        self.ready.set()
        self.ready_error = None
        self.load_error = None
        self.send_error = None
        self.sent_messages = []
        self.stop_calls = 0
        self.restore_calls = 0

    async def load(self, url, session):
        if self.load_error:
            raise self.load_error
        self.loads.append((url, session))

    async def wait_ready(self, timeout):
        await self.ready.wait()
        if self.ready_error:
            raise self.ready_error

    async def send(self, message):
        if self.send_error:
            raise self.send_error
        self.sent_messages.append(message)

    async def stop(self):
        self.stop_calls += 1

    async def restore_default(self):
        self.restore_calls += 1


class Router:
    def __init__(self):
        self.calls = []
        self.route_error = None
        self.route_gate = None
        self.route_gates = {}
        self.route_entered = asyncio.Event()
        self.route_entered_by_request = {}

    async def begin_session(self, session, selected, backend_handle=None):
        self.calls.append(("begin", session, selected, backend_handle))

    async def bind_backend(self, session_id, handle):
        self.calls.append(("bind", session_id, handle))

    async def mark_running(self, session_id):
        self.calls.append(("running", session_id))

    async def end_session(self, session_id):
        self.calls.append(("end", session_id))

    async def route_cloud_data(self, command):
        self.route_entered.set()
        entered = self.route_entered_by_request.setdefault(command.request_id, asyncio.Event())
        entered.set()
        if self.route_gate is not None:
            await self.route_gate.wait()
        gate = self.route_gates.get(command.request_id)
        if gate is not None:
            await gate.wait()
        if self.route_error:
            raise self.route_error
        self.calls.append(("cloud", command))

    async def accept_app_data(self, source, event):
        self.calls.append(("app", source, event))


class ObservedQueue(asyncio.Queue):
    def __init__(self, maxsize):
        super().__init__(maxsize=maxsize)
        self.put_started_count = 0
        self.put_count = 0
        self.changed = asyncio.Condition()

    async def put(self, item):
        async with self.changed:
            self.put_started_count += 1
            self.changed.notify_all()
        await super().put(item)
        async with self.changed:
            self.put_count += 1
            self.changed.notify_all()

    async def wait_put_started_count(self, expected):
        async with self.changed:
            await self.changed.wait_for(lambda: self.put_started_count >= expected)

    async def wait_put_count(self, expected):
        async with self.changed:
            await self.changed.wait_for(lambda: self.put_count >= expected)


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repository = Repository()
        self.installer = Installer()
        self.pointers = Pointers()
        self.pointers.repository = self.repository
        self.supervisor = Supervisor(self.repository)
        self.renderer = Renderer()
        self.router = Router()
        self.port_available = True
        self.coordinator = RuntimeCoordinator(
            self.installer, self.pointers, self.repository, self.supervisor,
            self.renderer, self.router, lambda host, port: self.port_available,
            "http://127.0.0.1:18080", startup_timeout=0.2, renderer_timeout=0.2,
        )
        await self.coordinator.start()

    async def asyncTearDown(self):
        await self.coordinator.close()

    async def test_cache_hit_persists_preparing_starting_running_and_activates(self):
        result = await self.coordinator.submit(start())

        self.assertTrue(result.ok)
        self.assertEqual(result.state, RuntimeState.RUNNING)
        self.assertEqual([item.state for item in self.repository.saved], [
            RuntimeState.PREPARING, RuntimeState.STARTING, RuntimeState.RUNNING,
        ])
        self.assertEqual(self.pointers.calls[0], ("snapshot", "demo_app"))
        self.assertEqual(self.pointers.calls[-2][0], "previous")
        self.assertEqual(self.pointers.calls[-1][0], "current")
        self.assertEqual(self.renderer.loads[0][0], "http://127.0.0.1:18080/index.html?v=1.0.0")
        self.assertEqual(self.renderer.sent_messages, [{
            "event": "runtime_init",
            "sessionId": "session-1",
            "appId": "demo_app",
            "version": "1.0.0",
            "data": {"mode": "test"},
        }])
        self.assertIsNot(
            self.renderer.sent_messages[0]["data"],
            self.installer.commands[0].init_data,
        )
        self.assertEqual(self.router.calls[-1], ("running", "session-1"))
        self.assertIsNotNone(self.repository.value.backend_process)
        self.assertEqual(self.pointers.state_at_web_mutation.state, RuntimeState.STARTING)
        self.assertEqual(
            self.pointers.state_at_web_mutation.pointer_snapshot,
            self.pointers.snapshot_value,
        )

    async def test_cold_install_persists_real_progress_sequence(self):
        self.installer.installed.cache_hit = False
        result = await self.coordinator.submit(start())
        self.assertTrue(result.ok)
        self.assertEqual([item.state for item in self.repository.saved], [
            RuntimeState.PREPARING, RuntimeState.DOWNLOADING, RuntimeState.VERIFYING,
            RuntimeState.INSTALLING, RuntimeState.STARTING, RuntimeState.RUNNING,
        ])

    async def test_install_failure_returns_sanitized_error_and_idles(self):
        self.installer.error = SmartAppError(ErrorCode.HASH_MISMATCH, "bad https://u:p@host/a?q=secret")
        result = await self.coordinator.submit(start())
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.HASH_MISMATCH)
        self.assertNotIn("secret", result.error.message)
        self.assertEqual(result.state, RuntimeState.IDLE)
        self.assertEqual(self.repository.value.last_error["code"], "HASH_MISMATCH")

    async def test_renderer_failure_rolls_back_all_owned_resources(self):
        self.renderer.ready_error = SmartAppError(ErrorCode.RENDERER_FAILED, "not ready")
        result = await self.coordinator.submit(start())
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.RENDERER_FAILED)
        self.assertEqual(self.supervisor.stop_calls[0][1], StopReason.RUNTIME_ERROR)
        self.assertIn(("restore", self.pointers.snapshot_value), self.pointers.calls)
        self.assertEqual(self.renderer.restore_calls, 1)
        self.assertEqual(result.state, RuntimeState.IDLE)

    async def test_renderer_load_failure_still_stops_owned_renderer(self):
        self.renderer.load_error = SmartAppError(ErrorCode.RENDERER_FAILED, "load failed")
        result = await self.coordinator.submit(start())
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.RENDERER_FAILED)
        self.assertEqual(self.renderer.stop_calls, 1)
        self.assertIn(("restore", self.pointers.snapshot_value), self.pointers.calls)

    async def test_backend_ready_timeout_is_structured_and_rolls_back(self):
        self.supervisor.start_gate = asyncio.Event()
        coordinator = RuntimeCoordinator(
            self.installer, self.pointers, self.repository, self.supervisor,
            self.renderer, self.router, lambda host, port: True,
            "http://127.0.0.1:18080", startup_timeout=0.001, renderer_timeout=0.2,
        )
        await self.coordinator.close()
        self.coordinator = coordinator
        await coordinator.start()
        result = await coordinator.submit(start())
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.START_TIMEOUT)
        self.assertEqual(result.state, RuntimeState.IDLE)

    async def test_dynamic_service_port_conflict_stops_before_components_start(self):
        self.installer.installed = Installed(manifest(dynamic=True))
        self.port_available = False
        result = await self.coordinator.submit(start())
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.PORT_IN_USE)
        self.assertIsNone(self.supervisor.handle)
        self.assertEqual(self.renderer.loads, [])

    async def test_pending_duplicate_fans_out_result_with_each_request_id(self):
        self.installer.release.clear()
        first = asyncio.create_task(self.coordinator.submit(start("one")))
        await self.installer.entered.wait()
        second = asyncio.create_task(self.coordinator.submit(start("two")))
        barrier = asyncio.create_task(self.coordinator.submit(GetStatus("barrier")))
        await barrier
        self.installer.release.set()
        left, right = await asyncio.gather(first, second)
        self.assertEqual((left.request_id, right.request_id), ("one", "two"))
        self.assertTrue(left.ok and right.ok)
        self.assertEqual(self.installer.calls, 1)

    async def test_running_duplicate_succeeds_without_restart(self):
        self.assertTrue((await self.coordinator.submit(start("one"))).ok)
        duplicate = await self.coordinator.submit(start("two"))
        self.assertTrue(duplicate.ok)
        self.assertEqual(duplicate.request_id, "two")
        self.assertEqual(self.installer.calls, 1)

    async def test_new_session_replaces_running_only_after_old_is_idle(self):
        self.assertTrue((await self.coordinator.submit(start("old", "old-session"))).ok)
        old_handle = self.supervisor.handle
        replacement = await self.coordinator.submit(start("new", "new-session"))
        self.assertTrue(replacement.ok)
        self.assertEqual(replacement.session_id, "new-session")
        self.assertEqual(self.supervisor.stop_calls[0], (old_handle, StopReason.REPLACE))
        self.assertEqual(self.installer.calls, 2)

    async def test_same_session_conflict_does_not_disturb_pending_start(self):
        self.installer.release.clear()
        pending = asyncio.create_task(self.coordinator.submit(start("one")))
        await self.installer.entered.wait()
        conflict = await self.coordinator.submit(start("two", version="2.0.0", sha="b" * 64))
        self.assertEqual(conflict.error.code, ErrorCode.SESSION_CONFLICT)
        self.installer.release.set()
        self.assertTrue((await pending).ok)

    async def test_different_session_replaces_pending_start(self):
        self.installer.release.clear()
        old = asyncio.create_task(self.coordinator.submit(start("old", "old-session")))
        await self.installer.entered.wait()
        new = asyncio.create_task(self.coordinator.submit(start("new", "new-session")))
        old_result = await old
        self.assertEqual(old_result.error.code, ErrorCode.SESSION_CONFLICT)
        self.installer.release.set()
        self.assertTrue((await new).ok)
        self.assertEqual(self.installer.calls, 2)

    async def test_stop_during_download_cancels_and_repeated_stops_share_cleanup(self):
        self.installer.release.clear()
        pending = asyncio.create_task(self.coordinator.submit(start()))
        await self.installer.entered.wait()
        one = asyncio.create_task(self.coordinator.submit(StopApp("stop-1", "session-1", StopReason.CLOUD_STOP)))
        two = asyncio.create_task(self.coordinator.submit(StopApp("stop-2", "session-1", StopReason.CLOUD_STOP)))
        self.installer.release.set()
        started, stopped_one, stopped_two = await asyncio.gather(pending, one, two)
        self.assertFalse(started.ok)
        self.assertTrue(stopped_one.ok and stopped_two.ok)
        self.assertEqual(stopped_one.state, RuntimeState.IDLE)

    async def test_cancel_after_backend_ownership_keeps_cleaning_when_stop_cannot_release(self):
        self.supervisor.fail_after_owned = SmartAppError(
            ErrorCode.INTERNAL_ERROR, "identity persistence failed"
        )
        self.supervisor.stop_error_always = SmartAppError(ErrorCode.INTERNAL_ERROR, "owned")
        starting = asyncio.create_task(self.coordinator.submit(start()))
        await asyncio.wait_for(self.supervisor.started.wait(), 0.2)
        result = await starting
        self.assertFalse(result.ok)
        self.assertEqual(result.state, RuntimeState.CLEANING)
        self.assertEqual(len(self.supervisor.stop_calls), 1)
        self.supervisor.stop_error_always = None
        retry = await self.coordinator.submit(
            StopApp("retry", "session-1", StopReason.CLOUD_STOP)
        )
        self.assertTrue(retry.ok)

    async def test_stale_generation_completion_and_backend_callbacks_are_ignored(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        old_handle = self.supervisor.handle
        old_event = self.supervisor.on_event
        old_exit = self.supervisor.on_exit
        await self.coordinator.submit(StopApp("stop", "session-1", StopReason.REPLACE))
        self.assertTrue((await self.coordinator.submit(start("new", "session-2"))).ok)
        calls = copy.copy(self.router.calls)
        await old_event(BackendEvent("app_data", "x", {"stale": True}))
        await old_exit(old_handle, SmartAppError(ErrorCode.BACKEND_EXITED, "old"))
        self.assertEqual(self.router.calls, calls)

    async def test_late_success_during_cleanup_cannot_reactivate_session(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        resources = self.coordinator._resources
        self.supervisor.stop_gate = asyncio.Event()
        stopping = asyncio.create_task(self.coordinator.submit(
            StopApp("stop", "session-1", StopReason.CLOUD_STOP)
        ))
        await self.supervisor.stop_entered.wait()
        running_calls = list(self.router.calls)
        pointer_calls = list(self.pointers.calls)

        await self.coordinator._queue.put(WorkflowSucceeded(
            self.coordinator._generation, resources
        ))
        await self.coordinator.submit(GetStatus("barrier"))
        observed_router = list(self.router.calls)
        observed_pointers = list(self.pointers.calls)
        self.supervisor.stop_gate.set()
        self.assertTrue((await stopping).ok)
        self.assertEqual(observed_router, running_calls)
        self.assertEqual(observed_pointers, pointer_calls)

    async def test_component_exit_is_session_fatal_and_cleans(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        handle = self.supervisor.handle
        await self.supervisor.on_exit(handle, SmartAppError(ErrorCode.BACKEND_EXITED, "gone"))
        await self.coordinator.submit(StopApp("join", "session-1", StopReason.RUNTIME_ERROR))
        status = await self.coordinator.submit(GetStatus("status"))
        self.assertEqual(status.state, RuntimeState.IDLE)

    async def test_backend_exit_during_starting_cannot_activate_dead_process(self):
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        leaked_contexts = []
        loop.set_exception_handler(lambda _loop, context: leaked_contexts.append(context))
        self.supervisor.exit_before_return = SmartAppError(
            ErrorCode.BACKEND_EXITED, "exited after ready"
        )
        try:
            result = await self.coordinator.submit(start())
            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, ErrorCode.BACKEND_EXITED)
            self.assertEqual(result.state, RuntimeState.IDLE)
            self.assertNotIn(("running", "session-1"), self.router.calls)
            gc.collect()
            await asyncio.sleep(0)
            self.assertFalse(
                any(
                    context.get("message")
                    == "_GatheringFuture exception was never retrieved"
                    for context in leaked_contexts
                ),
                leaked_contexts,
            )
        finally:
            loop.set_exception_handler(previous_handler)

    async def test_cloud_send_failure_is_session_fatal(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        self.router.route_error = DeliveryFailure(
            SmartAppError(ErrorCode.SESSION_MISMATCH, "backend handle is not active")
        )
        cloud = CloudData("cloud", "session-1", 1, MessageTarget.BROADCAST, "x", None, {})
        result = await self.coordinator.submit(cloud)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.SESSION_MISMATCH)
        await self.coordinator.submit(StopApp("join", "session-1", StopReason.RUNTIME_ERROR))
        self.assertEqual((await self.coordinator.submit(GetStatus("status"))).state, RuntimeState.IDLE)

    async def test_router_client_rejection_does_not_stop_running_session(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        self.router.route_error = SmartAppError(ErrorCode.QUEUE_FULL, "queue full")

        result = await self.coordinator.submit(CloudData(
            "cloud", "session-1", 1, MessageTarget.WEB, "x", None, {}
        ))

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.QUEUE_FULL)
        self.assertEqual(
            (await self.coordinator.submit(GetStatus("status"))).state,
            RuntimeState.RUNNING,
        )

    async def test_event_arriving_before_backend_start_returns_is_not_lost(self):
        event = BackendEvent("app_data", "result", {"score": 1})
        self.supervisor.immediate_event = event
        result = await self.coordinator.submit(start())
        self.assertTrue(result.ok)
        self.assertIn(("app", "python", event), self.router.calls)

    async def test_cleanup_not_released_stays_cleaning_and_later_stop_retries(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        self.supervisor.stop_error_once = SmartAppError(ErrorCode.INTERNAL_ERROR, "still owned")
        first = await self.coordinator.submit(
            StopApp("stop-1", "session-1", StopReason.CLOUD_STOP)
        )
        self.assertFalse(first.ok)
        self.assertEqual(first.state, RuntimeState.CLEANING)
        second = await self.coordinator.submit(
            StopApp("stop-2", "session-1", StopReason.CLOUD_STOP)
        )
        self.assertTrue(second.ok)
        self.assertEqual(second.state, RuntimeState.IDLE)

    async def test_same_identity_start_conflicts_during_stopping_and_cleaning(self):
        self.assertTrue((await self.coordinator.submit(start("initial"))).ok)
        self.supervisor.stop_gate = asyncio.Event()
        stopping = asyncio.create_task(self.coordinator.submit(
            StopApp("stop", "session-1", StopReason.CLOUD_STOP)
        ))
        await self.supervisor.stop_entered.wait()
        conflict = await self.coordinator.submit(start("during-stop"))
        self.assertEqual(conflict.error.code, ErrorCode.SESSION_CONFLICT)
        self.supervisor.stop_gate.set()
        await stopping

        self.assertTrue((await self.coordinator.submit(start("again"))).ok)
        self.supervisor.stop_error_once = SmartAppError(ErrorCode.INTERNAL_ERROR, "owned")
        failed_stop = await self.coordinator.submit(
            StopApp("fail", "session-1", StopReason.CLOUD_STOP)
        )
        self.assertFalse(failed_stop.ok)
        conflict = await self.coordinator.submit(start("during-cleaning"))
        self.assertEqual(conflict.error.code, ErrorCode.SESSION_CONFLICT)
        await self.coordinator.submit(StopApp("retry", "session-1", StopReason.CLOUD_STOP))

    async def test_router_is_disabled_before_backend_stop_wait(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        self.supervisor.stop_gate = asyncio.Event()
        stopping = asyncio.create_task(self.coordinator.submit(
            StopApp("stop", "session-1", StopReason.CLOUD_STOP)
        ))
        await self.supervisor.stop_entered.wait()
        self.assertIn(("end", "session-1"), self.router.calls)
        self.supervisor.stop_gate.set()
        self.assertTrue((await stopping).ok)

    async def test_submit_normalizes_mutable_start_and_cloud_payloads(self):
        self.installer.release.clear()
        mutable_init = {"nested": [1]}
        command = start()
        object.__setattr__(command, "init_data", mutable_init)
        starting = asyncio.create_task(self.coordinator.submit(command))
        await self.installer.entered.wait()
        mutable_init["nested"].append(2)
        self.installer.release.set()
        self.assertTrue((await starting).ok)
        self.assertEqual(self.supervisor.init_data, {"nested": [1]})

        self.router.route_gate = asyncio.Event()
        mutable_data = {"items": [1]}
        cloud = CloudData("cloud-copy", "session-1", 1, MessageTarget.WEB, None, None, mutable_data)
        routing = asyncio.create_task(self.coordinator.submit(cloud))
        await self.router.route_entered.wait()
        mutable_data["items"].append(2)
        self.router.route_gate.set()
        self.assertTrue((await routing).ok)
        routed = [call[1] for call in self.router.calls if call[0] == "cloud"][-1]
        self.assertEqual(routed.data, {"items": [1]})

    async def test_submit_returns_validation_result_for_invalid_domain_instance(self):
        command = start("invalid")
        object.__setattr__(command, "init_data", {"bad": {1}})
        result = await self.coordinator.submit(command)
        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.VALIDATION_ERROR)

    async def test_failed_start_cleanup_retry_preserves_rollback_mode(self):
        self.renderer.ready_error = SmartAppError(ErrorCode.RENDERER_FAILED, "not ready")
        self.supervisor.stop_error_once = SmartAppError(ErrorCode.INTERNAL_ERROR, "still owned")
        failed = await self.coordinator.submit(start())
        self.assertFalse(failed.ok)
        self.assertEqual(failed.state, RuntimeState.CLEANING)

        retried = await self.coordinator.submit(
            StopApp("retry", "session-1", StopReason.RUNTIME_ERROR)
        )
        self.assertTrue(retried.ok)
        self.assertEqual(
            [call for call in self.pointers.calls if call[0] == "restore"],
            [("restore", self.pointers.snapshot_value), ("restore", self.pointers.snapshot_value)],
        )
        self.assertNotIn(("web", None), self.pointers.calls)

    async def test_command_result_is_defensive_and_status_is_immediate_snapshot(self):
        result = await self.coordinator.submit(GetStatus("status"))
        self.assertEqual(result.to_dict(), {
            "event": "command_result", "requestId": "status", "ok": True, "state": "IDLE",
        })
        error = SmartAppError(ErrorCode.INTERNAL_ERROR, "failed", {"nested": "value"})
        failed = CommandResult("x", False, RuntimeState.IDLE, error=error)
        encoded = failed.to_dict()
        encoded["error"]["details"]["nested"] = "changed"
        self.assertEqual(failed.to_dict()["error"]["details"]["nested"], "value")

    async def test_non_idle_recovered_state_is_refused(self):
        await self.coordinator.close()
        repository = Repository(PersistedRuntimeState(state=RuntimeState.RUNNING))
        coordinator = RuntimeCoordinator(
            self.installer, self.pointers, repository, self.supervisor, self.renderer,
            self.router, lambda host, port: True, "http://127.0.0.1:18080",
        )
        with self.assertRaises(SmartAppError) as raised:
            await coordinator.start()
        self.assertEqual(raised.exception.code, ErrorCode.INTERNAL_ERROR)

    async def test_submit_close_race_settles_and_rejects_new_submission(self):
        self.installer.release.clear()
        pending = asyncio.create_task(self.coordinator.submit(start()))
        await self.installer.entered.wait()
        closing = asyncio.create_task(self.coordinator.close())
        self.installer.release.set()
        result = await pending
        await closing
        self.assertFalse(result.ok)
        rejected = await self.coordinator.submit(GetStatus("late"))
        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.error.code, ErrorCode.INTERNAL_ERROR)

    async def test_full_queue_serializes_admitted_submits_before_close(self):
        await self.coordinator.close()
        queue = ObservedQueue(1)
        self.coordinator = RuntimeCoordinator(
            self.installer, self.pointers, self.repository, self.supervisor,
            self.renderer, self.router, lambda host, port: True,
            "http://127.0.0.1:18080", queue_size=1,
        )
        self.coordinator._queue = queue
        await self.coordinator.start()
        self.assertTrue((await self.coordinator.submit(start())).ok)
        baseline = queue.put_count
        self.router.route_gate = asyncio.Event()
        first = asyncio.create_task(self.coordinator.submit(
            CloudData("c1", "session-1", 1, MessageTarget.WEB, None, None, {})
        ))
        await self.router.route_entered.wait()
        second = asyncio.create_task(self.coordinator.submit(
            CloudData("c2", "session-1", 2, MessageTarget.WEB, None, None, {})
        ))
        await queue.wait_put_count(baseline + 2)
        third = asyncio.create_task(self.coordinator.submit(
            CloudData("c3", "session-1", 3, MessageTarget.WEB, None, None, {})
        ))
        closing = asyncio.create_task(self.coordinator.close())

        self.router.route_gate.set()
        results = await asyncio.gather(first, second, third)
        await closing
        self.assertTrue(all(result.ok for result in results))
        self.assertEqual([call[1].request_id for call in self.router.calls if call[0] == "cloud"][-3:],
                         ["c1", "c2", "c3"])

    async def test_cancelled_close_waits_for_token_admission_and_retry_settles(self):
        await self.coordinator.close()
        self.installer.installed = Installed(manifest(web=True, backend=False))
        queue = ObservedQueue(1)
        self.coordinator = RuntimeCoordinator(
            self.installer, self.pointers, self.repository, self.supervisor,
            self.renderer, self.router, lambda host, port: True,
            "http://127.0.0.1:18080", queue_size=1,
        )
        self.coordinator._queue = queue
        await self.coordinator.start()
        self.assertTrue((await self.coordinator.submit(start())).ok)
        baseline_started = queue.put_started_count
        baseline_put = queue.put_count

        first_gate, second_gate = asyncio.Event(), asyncio.Event()
        self.router.route_gates.update(c1=first_gate, c2=second_gate)
        first = asyncio.create_task(self.coordinator.submit(
            CloudData("c1", "session-1", 1, MessageTarget.WEB, None, None, {})
        ))
        await self.router.route_entered_by_request.setdefault("c1", asyncio.Event()).wait()
        second = asyncio.create_task(self.coordinator.submit(
            CloudData("c2", "session-1", 2, MessageTarget.WEB, None, None, {})
        ))
        await queue.wait_put_count(baseline_put + 2)
        third = asyncio.create_task(self.coordinator.submit(
            CloudData("c3", "session-1", 3, MessageTarget.WEB, None, None, {})
        ))

        first_gate.set()
        await self.router.route_entered_by_request.setdefault("c2", asyncio.Event()).wait()
        await queue.wait_put_count(baseline_put + 3)
        closing = asyncio.create_task(self.coordinator.close())
        await queue.wait_put_started_count(baseline_started + 4)
        self.assertFalse(closing.done(), "close token must be blocked behind accepted commands")

        closing.cancel()
        second_gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 0.5)

        results = await asyncio.wait_for(asyncio.gather(first, second, third), 0.5)
        self.assertTrue(all(result.ok for result in results))
        retry = asyncio.create_task(self.coordinator.close())
        try:
            await asyncio.wait_for(asyncio.shield(retry), 0.5)
        except asyncio.TimeoutError:
            retry.cancel()
            await asyncio.gather(retry, return_exceptions=True)
            self.coordinator._close_future = None
            await asyncio.wait_for(self.coordinator.close(), 0.5)
            raise
        self.assertIsNone(self.coordinator._close_enqueue_task)
        self.assertTrue(all(task.done() for task in (first, second, third)))

    async def test_concurrent_close_shares_one_bounded_cleanup_attempt(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        self.supervisor.stop_error_once = SmartAppError(ErrorCode.INTERNAL_ERROR, "retry")
        first = asyncio.create_task(self.coordinator.close())
        second = asyncio.create_task(self.coordinator.close())
        outcomes = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 0.5
        )
        self.assertTrue(all(isinstance(item, SmartAppError) for item in outcomes))
        self.assertEqual(len(self.supervisor.stop_calls), 1)
        await self.coordinator.close()
        self.assertEqual(len(self.supervisor.stop_calls), 2)

    async def test_close_failure_is_bounded_and_explicitly_retryable(self):
        self.assertTrue((await self.coordinator.submit(start())).ok)
        self.supervisor.stop_error_always = SmartAppError(ErrorCode.INTERNAL_ERROR, "owned")
        with self.assertRaises(SmartAppError):
            await asyncio.wait_for(self.coordinator.close(), 0.2)
        self.assertEqual(len(self.supervisor.stop_calls), 1)
        self.supervisor.stop_error_always = None
        await self.coordinator.close()
        self.assertEqual(len(self.supervisor.stop_calls), 2)
        self.assertIsNone(self.repository.value.last_error)


if __name__ == "__main__":
    unittest.main()
