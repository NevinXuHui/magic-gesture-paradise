import copy
import unittest

from smartapp_runtime.adapters.fake_renderer import FakeRenderer
from smartapp_runtime.application.router import DeliveryFailure, MessageRouter
from smartapp_runtime.domain.commands import CloudData, MessageTarget
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest
from smartapp_runtime.domain.models import Session
from smartapp_runtime.ports.process import BackendEvent


def manifest(web=True, backend=True, default="web"):
    return Manifest.from_dict(
        {
            "schemaVersion": 1,
            "appId": "demo_app",
            "version": "1.0.0",
            "web": {"enabled": web, "entry": "index.html"},
            "backend": {
                "enabled": backend,
                "entry": "main.py",
                "dynamicService": False,
            },
            "routing": {"defaultTarget": default},
        }
    )


def cloud(seq, target=MessageTarget.AUTO, data=None, session_id="session-1", data_type=None):
    return CloudData(
        request_id="request-{0}".format(seq),
        session_id=session_id,
        seq=seq,
        target=target,
        data_type=data_type,
        trigger=None,
        data={} if data is None else data,
    )


class BackendSender:
    def __init__(self):
        self.calls = []
        self.error = None

    async def send(self, handle, message):
        if self.error is not None:
            raise self.error
        self.calls.append((handle, copy.deepcopy(message)))


class AgentSink:
    def __init__(self, available=True):
        self.available = available
        self.calls = []
        self.error = None

    async def __call__(self, event):
        if self.error is not None:
            raise self.error
        self.calls.append(copy.deepcopy(event))
        return self.available


class MessageRouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.session = Session("session-1", "demo_app", "1.0.0", 1)
        self.renderer = FakeRenderer()
        self.backend = BackendSender()
        self.sink = AgentSink()
        self.router = MessageRouter(self.renderer, self.backend, self.sink)

    async def begin_running(self, selected_manifest=None):
        selected = selected_manifest or manifest()
        await self.router.begin_session(self.session, selected, backend_handle="backend-1")
        await self.router.mark_running(self.session.session_id)

    async def test_routing_matrix_resolves_auto_and_broadcast_in_stable_order(self):
        cases = (
            (manifest(web=False, backend=True, default="python"), MessageTarget.AUTO, ("python",)),
            (manifest(web=False, backend=True, default="python"), MessageTarget.PYTHON, ("python",)),
            (manifest(web=True, backend=False, default="web"), MessageTarget.AUTO, ("web",)),
            (manifest(web=True, backend=False, default="web"), MessageTarget.WEB, ("web",)),
            (manifest(), MessageTarget.AUTO, ("web",)),
            (manifest(), MessageTarget.PYTHON, ("python",)),
            (manifest(), MessageTarget.WEB, ("web",)),
            (manifest(), MessageTarget.BROADCAST, ("python", "web")),
        )

        for selected_manifest, target, expected in cases:
            with self.subTest(web=selected_manifest.web.enabled, backend=selected_manifest.backend.enabled,
                              target=target):
                renderer = FakeRenderer()
                backend = BackendSender()
                router = MessageRouter(renderer, backend, AgentSink())
                handle = "backend" if selected_manifest.backend.enabled else None
                await router.begin_session(self.session, selected_manifest, handle)
                await router.mark_running(self.session.session_id)
                await router.route_cloud_data(cloud(1, target, {"value": 1}))

                actual = []
                if backend.calls:
                    actual.append("python")
                if renderer.sent_messages:
                    actual.append("web")
                self.assertEqual(tuple(actual), expected)
                expected_message = {"event": "cloud_data", "seq": 1, "data": {"value": 1}}
                if backend.calls:
                    self.assertEqual(backend.calls[0], ("backend", expected_message))
                if renderer.sent_messages:
                    self.assertEqual(renderer.sent_messages[0], expected_message)

    async def test_disabled_explicit_target_is_rejected_without_advancing_sequence(self):
        await self.begin_running(manifest(web=True, backend=False, default="web"))

        with self.assertRaises(SmartAppError) as raised:
            await self.router.route_cloud_data(cloud(4, MessageTarget.PYTHON))
        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

        await self.router.route_cloud_data(cloud(4, MessageTarget.WEB))
        self.assertEqual(self.renderer.sent_messages[0]["seq"], 4)

    async def test_old_session_and_duplicate_or_out_of_order_sequence_are_rejected(self):
        await self.begin_running()

        with self.assertRaises(SmartAppError) as old:
            await self.router.route_cloud_data(cloud(1, session_id="old-session"))
        self.assertEqual(old.exception.code, ErrorCode.SESSION_MISMATCH)

        await self.router.route_cloud_data(cloud(2, MessageTarget.WEB))
        for seq in (2, 1):
            with self.subTest(seq=seq), self.assertRaises(SmartAppError) as duplicate:
                await self.router.route_cloud_data(cloud(seq, MessageTarget.WEB))
            self.assertEqual(duplicate.exception.code, ErrorCode.SEQ_OUT_OF_ORDER)

    async def test_starting_queue_flushes_fifo_with_resolved_targets(self):
        await self.router.begin_session(self.session, manifest(), backend_handle="backend-1")
        await self.router.route_cloud_data(cloud(1, MessageTarget.AUTO, {"order": 1}))
        await self.router.route_cloud_data(cloud(2, MessageTarget.PYTHON, {"order": 2}))

        await self.router.mark_running(self.session.session_id)

        self.assertEqual([message["data"]["order"] for _, message in self.backend.calls], [2])
        self.assertEqual([message["data"]["order"] for message in self.renderer.sent_messages], [1])

    async def test_starting_queue_rejects_newest_on_count_overflow_and_keeps_sequence_reusable(self):
        router = MessageRouter(self.renderer, self.backend, self.sink,
                               max_queue_messages=1, max_queue_bytes=4096)
        await router.begin_session(self.session, manifest(), backend_handle="backend-1")
        await router.route_cloud_data(cloud(1, MessageTarget.WEB))

        with self.assertRaises(SmartAppError) as raised:
            await router.route_cloud_data(cloud(2, MessageTarget.WEB))
        self.assertEqual(raised.exception.code, ErrorCode.QUEUE_FULL)

        await router.mark_running(self.session.session_id)
        await router.route_cloud_data(cloud(2, MessageTarget.WEB))
        self.assertEqual([message["seq"] for message in self.renderer.sent_messages], [1, 2])

    async def test_starting_queue_honors_exact_compact_utf8_byte_boundary(self):
        encoded_size = len(
            b'{"targets":["web"],"message":{"event":"cloud_data","seq":1,"data":{}}}'
        )
        accepted = MessageRouter(self.renderer, self.backend, self.sink,
                                 max_queue_messages=2, max_queue_bytes=encoded_size)
        await accepted.begin_session(self.session, manifest(), backend_handle="backend-1")
        await accepted.route_cloud_data(cloud(1, MessageTarget.WEB))

        rejected = MessageRouter(FakeRenderer(), BackendSender(), AgentSink(),
                                 max_queue_messages=2, max_queue_bytes=encoded_size - 1)
        await rejected.begin_session(self.session, manifest(), backend_handle="backend-1")
        with self.assertRaises(SmartAppError) as raised:
            await rejected.route_cloud_data(cloud(1, MessageTarget.WEB))
        self.assertEqual(raised.exception.code, ErrorCode.QUEUE_FULL)

    async def test_mark_running_requires_backend_binding_and_running_delivery_is_serialized(self):
        await self.router.begin_session(self.session, manifest(), backend_handle=None)
        with self.assertRaises(SmartAppError) as raised:
            await self.router.mark_running(self.session.session_id)
        self.assertEqual(raised.exception.code, ErrorCode.SESSION_MISMATCH)

        await self.router.bind_backend(self.session.session_id, "backend-1")
        await self.router.mark_running(self.session.session_id)
        await self.router.route_cloud_data(cloud(1, MessageTarget.BROADCAST))
        self.assertEqual(len(self.backend.calls), 1)
        self.assertEqual(len(self.renderer.sent_messages), 1)

    async def test_downstream_payload_is_defensively_copied(self):
        await self.begin_running()
        data = {"nested": [1]}
        await self.router.route_cloud_data(cloud(1, MessageTarget.BROADCAST, data, data_type="kind"))
        data["nested"].append(2)
        self.backend.calls[0][1]["data"]["nested"].append(3)

        self.assertEqual(self.renderer.sent_messages[0], {
            "event": "cloud_data", "seq": 1, "dataType": "kind", "data": {"nested": [1]}
        })

    async def test_running_component_error_is_explicit_delivery_failure(self):
        await self.begin_running(manifest(web=False, backend=True, default="python"))
        original = SmartAppError(
            ErrorCode.SESSION_MISMATCH, "backend handle is not active"
        )
        self.backend.error = original

        with self.assertRaises(DeliveryFailure) as raised:
            await self.router.route_cloud_data(cloud(1, MessageTarget.PYTHON))

        self.assertIs(raised.exception.error, original)

    async def test_starting_flush_keeps_original_component_error_mapping(self):
        await self.router.begin_session(
            self.session, manifest(web=True, backend=False, default="web")
        )
        original = SmartAppError(ErrorCode.VALIDATION_ERROR, "renderer rejected")
        self.renderer.set_send_error(original)
        await self.router.route_cloud_data(cloud(1, MessageTarget.WEB))

        with self.assertRaises(SmartAppError) as raised:
            await self.router.mark_running(self.session.session_id)

        self.assertIs(raised.exception, original)

    async def test_app_data_is_validated_and_enriched_with_authoritative_ids(self):
        await self.begin_running()
        message = {"event": "app_data", "dataType": "result", "data": {"ok": True}}

        await self.router.accept_app_data("web", message)
        message["data"]["ok"] = False

        self.assertEqual(self.sink.calls, [{
            "event": "app_data",
            "sessionId": "session-1",
            "appId": "demo_app",
            "dataType": "result",
            "data": {"ok": True},
        }])

    async def test_app_data_accepts_backend_event_and_rejects_source_or_shape_mismatch(self):
        await self.begin_running()
        await self.router.accept_app_data("python", BackendEvent("app_data", "result", {"score": 1}))
        self.assertEqual(self.sink.calls[0]["data"], {"score": 1})

        invalid = (
            ("native", {"event": "app_data", "dataType": "x", "data": {}}),
            ("python", {"event": "app_data", "dataType": "x", "data": {}}),
            ("web", BackendEvent("app_data", "x", {})),
            ("web", {"event": "app_data", "dataType": "", "data": {}}),
            ("web", {"event": "app_data", "dataType": "x", "data": [],}),
            ("web", {"event": "app_data", "dataType": "x", "data": {}, "sessionId": "fake"}),
        )
        for source, value in invalid:
            with self.subTest(source=source, value=value), self.assertRaises(SmartAppError) as raised:
                await self.router.accept_app_data(source, value)
            self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    async def test_app_data_requires_running_and_enabled_source(self):
        await self.router.begin_session(
            self.session, manifest(web=True, backend=False, default="web"), backend_handle=None
        )
        message = {"event": "app_data", "dataType": "x", "data": {}}
        with self.assertRaises(SmartAppError) as starting:
            await self.router.accept_app_data("web", message)
        self.assertEqual(starting.exception.code, ErrorCode.SESSION_MISMATCH)
        await self.router.mark_running(self.session.session_id)
        with self.assertRaises(SmartAppError) as disabled:
            await self.router.accept_app_data("python", BackendEvent("app_data", "x", {}))
        self.assertEqual(disabled.exception.code, ErrorCode.VALIDATION_ERROR)

    async def test_unavailable_upstream_queues_fifo_and_flushes_without_duplicates(self):
        self.sink.available = False
        await self.begin_running()
        await self.router.accept_app_data("web", {"event": "app_data", "dataType": "x", "data": {"n": 1}})
        await self.router.accept_app_data("python", BackendEvent("app_data", "x", {"n": 2}))
        self.assertEqual([item["data"]["n"] for item in self.sink.calls], [1, 1])

        self.sink.calls.clear()
        self.assertFalse(await self.router.flush_upstream())
        self.sink.available = True
        self.assertTrue(await self.router.flush_upstream())
        self.assertTrue(await self.router.flush_upstream())
        self.assertEqual([item["data"]["n"] for item in self.sink.calls], [1, 1, 2])

    async def test_upstream_queue_rejects_newest_at_count_and_exact_byte_limits(self):
        self.sink.available = False
        exact_event = (
            b'{"event":"app_data","sessionId":"session-1","appId":"demo_app",'
            b'"dataType":"x","data":{}}'
        )
        router = MessageRouter(self.renderer, self.backend, self.sink,
                               max_queue_messages=1, max_queue_bytes=len(exact_event))
        await router.begin_session(self.session, manifest(), backend_handle="backend-1")
        await router.mark_running(self.session.session_id)
        event = {"event": "app_data", "dataType": "x", "data": {}}
        await router.accept_app_data("web", event)
        with self.assertRaises(SmartAppError) as count_error:
            await router.accept_app_data("web", event)
        self.assertEqual(count_error.exception.code, ErrorCode.UPSTREAM_QUEUE_FULL)

        byte_router = MessageRouter(FakeRenderer(), BackendSender(), AgentSink(False),
                                    max_queue_messages=2, max_queue_bytes=len(exact_event) - 1)
        await byte_router.begin_session(self.session, manifest(), backend_handle="backend-1")
        await byte_router.mark_running(self.session.session_id)
        with self.assertRaises(SmartAppError) as byte_error:
            await byte_router.accept_app_data("web", event)
        self.assertEqual(byte_error.exception.code, ErrorCode.UPSTREAM_QUEUE_FULL)

    async def test_upstream_flush_stops_on_unavailable_and_sink_failures_are_sanitized(self):
        self.sink.available = False
        await self.begin_running()
        message = {"event": "app_data", "dataType": "x", "data": {}}
        await self.router.accept_app_data("web", message)
        self.sink.calls.clear()
        self.assertFalse(await self.router.flush_upstream())
        self.assertEqual(len(self.sink.calls), 1)

        self.sink.error = RuntimeError("secret sink detail")
        with self.assertRaises(SmartAppError) as raised:
            await self.router.flush_upstream()
        self.assertEqual(raised.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("secret", raised.exception.message)

        self.sink.error = SmartAppError(ErrorCode.DOWNLOAD_FAILED, "secret sink URL")
        with self.assertRaises(SmartAppError) as stable:
            await self.router.flush_upstream()
        self.assertEqual(stable.exception.code, ErrorCode.INTERNAL_ERROR)
        self.assertNotIn("secret", stable.exception.message)

    async def test_end_session_discards_queues_and_is_idempotent_for_stale_ids(self):
        self.sink.available = False
        await self.begin_running()
        await self.router.accept_app_data(
            "web", {"event": "app_data", "dataType": "x", "data": {}}
        )
        await self.router.end_session("stale")
        await self.router.end_session(self.session.session_id)
        await self.router.end_session(self.session.session_id)

        with self.assertRaises(SmartAppError) as raised:
            await self.router.route_cloud_data(cloud(2, MessageTarget.WEB))
        self.assertEqual(raised.exception.code, ErrorCode.SESSION_MISMATCH)
        self.sink.available = True
        self.assertTrue(await self.router.flush_upstream())
        self.assertEqual(len(self.sink.calls), 1)

    async def test_stale_renderer_callback_cannot_cross_session_generation(self):
        class CapturingRenderer(FakeRenderer):
            def __init__(self):
                super().__init__()
                self.handlers = []

            def set_message_handler(self, handler):
                super().set_message_handler(handler)
                if handler is not None:
                    self.handlers.append(handler)

        renderer = CapturingRenderer()
        sink = AgentSink()
        router = MessageRouter(renderer, BackendSender(), sink)
        first = Session("session-1", "demo_app", "1.0.0", 1)
        second = Session("session-1", "demo_app", "1.0.0", 2)
        web_manifest = manifest(web=True, backend=False, default="web")
        await router.begin_session(first, web_manifest)
        await router.mark_running(first.session_id)
        old_handler = renderer.handlers[-1]
        await router.end_session(first.session_id)
        await router.begin_session(second, web_manifest)
        await router.mark_running(second.session_id)

        with self.assertRaises(SmartAppError) as raised:
            await old_handler({"event": "app_data", "dataType": "x", "data": {"old": True}})

        self.assertEqual(raised.exception.code, ErrorCode.SESSION_MISMATCH)
        self.assertEqual(sink.calls, [])


if __name__ == "__main__":
    unittest.main()
