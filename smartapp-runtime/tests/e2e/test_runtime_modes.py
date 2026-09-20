import asyncio
import http.client
import io
import tarfile
import unittest

from smartapp_runtime.domain.state import RuntimeState

from helpers.package_factory import build_fixture
from helpers.runtime_harness import RuntimeHarness


class RuntimeModesTests(unittest.IsolatedAsyncioTestCase):
    async def test_package_factory_ignores_generated_fixture_artifacts(self):
        package = build_fixture("python_only")
        with tarfile.open(fileobj=io.BytesIO(package.data), mode="r:gz") as archive:
            names = [member.name for member in archive]
        self.assertEqual(names, [
            "python_only/backend/main.py",
            "python_only/manifest.json",
        ])

    async def _exercise_cache_reuse(self, harness, client, app_id, url, package,
                                    second_session):
        second = RuntimeHarness.start_command(
            "start-cache", second_session, app_id, "1", url, package,
            {"run": "cached"},
        )
        result = await client.command(second)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["state"], "RUNNING")
        self.assertEqual(len(harness.downloader.calls), 1)
        stopped = await client.command(RuntimeHarness.stop_command(
            "stop-cache", second_session
        ))
        self.assertTrue(stopped["ok"], stopped)
        self.assertEqual(stopped["state"], "IDLE")

    async def test_python_only_real_stack_routes_init_cloud_and_reuses_cache(self):
        package = build_fixture("python_only")
        self.assertEqual(package, build_fixture("python_only"))
        url = "memory://python-only"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "start-python", "python-session-1", "python_only", "1",
                url, package, {"mode": "normal", "seed": 7},
            ))
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["state"], "RUNNING")
            self.assertIsNotNone(harness.assembly.supervisor._active)
            self.assertEqual(harness.assembly.renderer.load_calls, [])
            self.assertEqual(harness.assembly.renderer.sent_messages, [])

            initial = await client.wait_for(
                lambda event: event.get("event") == "app_data"
                and event.get("dataType") == "init"
            )
            self.assertEqual(initial["sessionId"], "python-session-1")
            self.assertEqual(initial["appId"], "python_only")
            self.assertEqual(initial["data"], {"mode": "normal", "seed": 7})

            cloud = await client.command(RuntimeHarness.cloud_command(
                "cloud-python", "python-session-1", 1, {"word": "rock"}
            ))
            self.assertTrue(cloud["ok"], cloud)
            echoed = await client.wait_for(
                lambda event: event.get("event") == "app_data"
                and event.get("dataType") == "echo"
            )
            self.assertEqual(echoed["data"], {
                "seq": 1, "payload": {"word": "rock"}
            })

            stopped = await client.command(RuntimeHarness.stop_command(
                "stop-python", "python-session-1", "wake_word"
            ))
            self.assertTrue(stopped["ok"], stopped)
            self.assertEqual(stopped["state"], "IDLE")
            self.assertIsNone(harness.assembly.supervisor._active)
            await self._exercise_cache_reuse(
                harness, client, "python_only", url, package, "python-session-2"
            )
        finally:
            await harness.stop()

    async def test_web_only_real_stack_serves_routes_and_reuses_cache(self):
        package = build_fixture("web_only")
        url = "memory://web-only"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "start-web", "web-session-1", "web_only", "1", url, package,
                {"view": "main"},
            ))
            self.assertTrue(result["ok"], result)
            self.assertIsNone(harness.assembly.supervisor._active)
            self.assertEqual(len(harness.assembly.renderer.load_calls), 1)
            self.assertIn("/index.html?v=1", harness.assembly.renderer.load_calls[0][0])
            self.assertEqual(harness.assembly.renderer.sent_messages, [{
                "event": "runtime_init",
                "sessionId": "web-session-1",
                "appId": "web_only",
                "version": "1",
                "data": {"view": "main"},
            }])

            connection = http.client.HTTPConnection(
                harness.config.network.static_host,
                harness.config.network.static_port,
                timeout=1.0,
            )
            try:
                connection.request("GET", "/index.html")
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
            self.assertEqual(response.status, 200)
            self.assertIn(b"SmartApp Web fixture", body)

            cloud = await client.command(RuntimeHarness.cloud_command(
                "cloud-web", "web-session-1", 1, {"screen": "score"}
            ))
            self.assertTrue(cloud["ok"], cloud)
            self.assertEqual(harness.assembly.renderer.sent_messages[-1], {
                "event": "cloud_data", "seq": 1, "dataType": "echo",
                "data": {"screen": "score"},
            })
            await asyncio.wait_for(
                harness.assembly.renderer.emit_message({
                    "event": "app_data", "dataType": "web-event",
                    "data": {"clicked": True},
                }),
                1.0,
            )
            event = await client.wait_for(
                lambda item: item.get("dataType") == "web-event"
            )
            self.assertEqual(event["sessionId"], "web-session-1")
            self.assertEqual(event["data"], {"clicked": True})

            stopped = await client.command(RuntimeHarness.stop_command(
                "stop-web", "web-session-1"
            ))
            self.assertEqual(stopped["state"], "IDLE")
            self.assertIsNone(harness.assembly.pointers.current_web_target())
            await self._exercise_cache_reuse(
                harness, client, "web_only", url, package, "web-session-2"
            )
        finally:
            await harness.stop()

    async def test_hybrid_real_stack_selects_explicit_targets_and_reuses_cache(self):
        package = build_fixture("hybrid")
        url = "memory://hybrid"
        harness = RuntimeHarness({url: package.data})
        try:
            client = await harness.start()
            result = await client.command(RuntimeHarness.start_command(
                "start-hybrid", "hybrid-session-1", "hybrid", "1", url,
                package, {"level": 2},
            ))
            self.assertTrue(result["ok"], result)
            self.assertIsNotNone(harness.assembly.supervisor._active)
            self.assertEqual(len(harness.assembly.renderer.load_calls), 1)
            self.assertEqual(harness.assembly.renderer.sent_messages, [{
                "event": "runtime_init",
                "sessionId": "hybrid-session-1",
                "appId": "hybrid",
                "version": "1",
                "data": {"level": 2},
            }])
            initial = await client.wait_for(
                lambda event: event.get("dataType") == "init"
            )
            self.assertEqual(initial["data"], {"level": 2})

            defaulted = await client.command(RuntimeHarness.cloud_command(
                "cloud-hybrid-python", "hybrid-session-1", 1,
                {"side": "python"}, "auto",
            ))
            self.assertTrue(defaulted["ok"], defaulted)
            echoed = await client.wait_for(
                lambda event: event.get("dataType") == "echo"
            )
            self.assertEqual(echoed["data"]["payload"], {"side": "python"})
            sent_before = len(harness.assembly.renderer.sent_messages)
            web = await client.command(RuntimeHarness.cloud_command(
                "cloud-hybrid-web", "hybrid-session-1", 2,
                {"side": "web"}, "web",
            ))
            self.assertTrue(web["ok"], web)
            self.assertEqual(len(harness.assembly.renderer.sent_messages), sent_before + 1)

            stopped = await client.command(RuntimeHarness.stop_command(
                "stop-hybrid", "hybrid-session-1"
            ))
            self.assertEqual(stopped["state"], RuntimeState.IDLE.value)
            await self._exercise_cache_reuse(
                harness, client, "hybrid", url, package, "hybrid-session-2"
            )
        finally:
            await harness.stop()


if __name__ == "__main__":
    unittest.main()
