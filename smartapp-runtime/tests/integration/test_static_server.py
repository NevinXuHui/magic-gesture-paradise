import http.client
import io
import os
import socket
import threading
from contextlib import redirect_stderr
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.infrastructure.persistence.pointers import AtomicPointers
from smartapp_runtime.infrastructure.web.server import StaticWebServer, _LoopbackHTTPServer


class CountingPointers:
    def __init__(self, pointers):
        self.pointers = pointers
        self.calls = 0

    def current_web_target(self):
        self.calls += 1
        return self.pointers.current_web_target()


def ipv6_loopback_available():
    if not socket.has_ipv6:
        return False
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.bind(("::1", 0))
        return True
    except OSError:
        return False
    finally:
        probe.close()


class StaticWebServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths.from_root(Path(self.temp.name) / "runtime")
        self.paths.ensure_layout()
        self.pointers = AtomicPointers(self.paths)
        self.counting_pointers = CountingPointers(self.pointers)
        self.v1 = self.create_web_version("v1", "version one")
        self.v2 = self.create_web_version("v2", "version two")
        self.outside = Path(self.temp.name) / "outside.txt"
        self.outside.write_text("private", encoding="utf-8")
        (self.v1 / "escape.txt").symlink_to(self.outside)
        self.server = StaticWebServer(self.counting_pointers, "127.0.0.1", 0, 1.0)
        self.started = False
        self.addCleanup(self.server.stop)

    def create_web_version(self, version, index):
        root = self.paths.apps_root / "demo" / version / "web"
        (root / "assets").mkdir(parents=True)
        (root / "private").mkdir()
        (root / "index.html").write_text(index, encoding="utf-8")
        (root / "assets" / "app.css").write_text("body{}", encoding="utf-8")
        (root / "private" / "index.html").write_text("private index", encoding="utf-8")
        return root

    def start(self):
        if not self.started:
            self.server.start()
            self.started = True
        return self.server.address

    def request(self, method, target):
        host, port = self.start()
        connection = http.client.HTTPConnection(host, port, timeout=2)
        self.addCleanup(connection.close)
        connection.request(method, target)
        response = connection.getresponse()
        body = response.read()
        headers = dict(response.getheaders())
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        return response.status, headers, body

    def raw_request(self, method, target):
        return self.raw_request_line((method + " " + target + " HTTP/1.1").encode("ascii"))

    def raw_request_line(self, request_line):
        host, port = self.start()
        connection = socket.create_connection((host, port), timeout=2)
        self.addCleanup(connection.close)
        request = request_line + (
            "\r\nHost: " + host + "\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        connection.sendall(request)
        response = http.client.HTTPResponse(connection)
        response.begin()
        body = response.read()
        headers = dict(response.getheaders())
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        return response.status, headers, body

    def test_address_requires_start_and_only_loopback_is_accepted(self):
        with self.assertRaisesRegex(RuntimeError, "not started"):
            _ = self.server.address
        with self.assertRaisesRegex(ValueError, "loopback"):
            StaticWebServer(self.pointers, "192.0.2.1", 0, 1.0)

    def test_healthz_get_and_head(self):
        status, headers, body = self.request("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/plain; charset=utf-8")
        self.assertEqual(headers.get("Content-Length"), "3")
        self.assertEqual(body, b"ok\n")

        status, headers, body = self.request("HEAD", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Length"), "3")
        self.assertEqual(body, b"")
        self.assertEqual(self.counting_pointers.calls, 0)

    def test_serves_current_root_assets_and_switches_roots_without_restart(self):
        self.pointers.set_current_web(self.v1)
        log_output = io.StringIO()
        with redirect_stderr(log_output):
            status, headers, body = self.request("GET", "/?secret=value")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/html")
        self.assertEqual(body, b"version one")
        self.assertNotIn("secret=value", log_output.getvalue())

        status, headers, body = self.request("HEAD", "/assets/app.css")
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("Content-Type"), "text/css")
        self.assertEqual(headers.get("Content-Length"), str(len(b"body{}")))
        self.assertEqual(body, b"")

        status, _, body = self.request("GET", "/private/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"private index")

        self.pointers.set_current_web(self.v2)
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"version two")

    def test_rejects_missing_roots_methods_directories_and_unsafe_paths(self):
        status, _, _ = self.request("GET", "/missing")
        self.assertEqual(status, 404)

        self.pointers.set_current_web(self.v1)
        for method in ("POST", "DELETE"):
            with self.subTest(method=method):
                self.counting_pointers.calls = 0
                status, headers, _ = self.request(method, "/")
                self.assertEqual(status, 405)
                self.assertEqual(headers.get("Allow"), "GET, HEAD")
                self.assertEqual(self.counting_pointers.calls, 1)

        for target in (
            "/assets/",
            "/%2e%2e/outside.txt",
            "/assets//app.css",
            "/assets/%5capp.css",
            "/assets/%00app.css",
            "/escape.txt",
            "/%ZZ",
        ):
            with self.subTest(target=target):
                status, _, _ = self.request("GET", target)
                self.assertEqual(status, 404)

    def test_rejects_original_request_target_with_leading_double_slash(self):
        self.pointers.set_current_web(self.v1)
        status, _, _ = self.raw_request("GET", "//assets/app.css")
        self.assertEqual(status, 404)

    def test_accepts_tab_separated_request_line_without_traceback(self):
        self.pointers.set_current_web(self.v1)
        log_output = io.StringIO()
        with redirect_stderr(log_output):
            status, _, body = self.raw_request_line(b"GET\t/\tHTTP/1.1")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"version one")
        self.assertEqual(log_output.getvalue(), "")

    @unittest.skipUnless(ipv6_loopback_available(), "IPv6 loopback is unavailable")
    def test_ipv6_loopback_uses_ipv6_socket_family(self):
        server = StaticWebServer(self.counting_pointers, "::1", 0, 1.0)
        self.addCleanup(server.stop)
        self.pointers.set_current_web(self.v1)
        server.start()
        host, port = server.address
        self.assertEqual(host, "::1")
        connection = http.client.HTTPConnection(host, port, timeout=2)
        self.addCleanup(connection.close)
        connection.request("GET", "/")
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b"version one")

    def test_pointer_validation_error_maps_to_not_found(self):
        os.symlink(str(self.outside.parent), str(self.paths.current_web))
        status, _, _ = self.request("GET", "/")
        self.assertEqual(status, 404)

    def test_stop_is_idempotent_and_instance_can_run_twice(self):
        self.server.start()
        first_address = self.server.address
        self.server.stop()
        self.started = False
        self.server.stop()
        self.server.start()
        self.started = True
        self.assertEqual(self.server.address[0], first_address[0])

    def test_thread_start_failure_closes_bound_socket_and_stop_is_safe(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = StaticWebServer(self.pointers, "127.0.0.1", port, 0.2)

        with patch.object(threading.Thread, "start", side_effect=RuntimeError("thread failed")):
            with self.assertRaisesRegex(RuntimeError, "thread failed"):
                server.start()

        server.stop()
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            replacement.bind(("127.0.0.1", port))
        finally:
            replacement.close()
        with self.assertRaises(RuntimeError):
            _ = server.address

    def test_thread_start_error_is_not_masked_by_close_error(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = StaticWebServer(self.pointers, "127.0.0.1", port, 0.2)
        real_close = _LoopbackHTTPServer.server_close

        def close_then_fail(instance):
            real_close(instance)
            raise RuntimeError("close failed")

        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("thread failed")
        ):
            with patch.object(_LoopbackHTTPServer, "server_close", new=close_then_fail):
                with self.assertRaisesRegex(RuntimeError, "thread failed"):
                    server.start()

        server.stop()
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            replacement.bind(("127.0.0.1", port))
        finally:
            replacement.close()

    def test_stop_close_before_failure_retains_ownership_for_retry(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = StaticWebServer(self.pointers, "127.0.0.1", port, 0.2)
        server.start()
        owned = server._server
        self.addCleanup(owned.socket.close)

        with patch.object(
            _LoopbackHTTPServer,
            "server_close",
            side_effect=RuntimeError("close before failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "close before failure"):
                server.stop()

        self.assertIs(server._server, owned)
        self.assertIsNone(server._thread)
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):
                occupied.bind(("127.0.0.1", port))
        finally:
            occupied.close()

        server.stop()
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            replacement.bind(("127.0.0.1", port))
        finally:
            replacement.close()

    def test_start_close_before_failure_retains_primary_error_and_retryable_socket(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        server = StaticWebServer(self.pointers, "127.0.0.1", port, 0.2)
        captured = []

        def fail_before_close(instance):
            captured.append(instance)
            raise RuntimeError("close before failure")

        with patch.object(
            threading.Thread, "start", side_effect=RuntimeError("thread failed")
        ):
            with patch.object(
                _LoopbackHTTPServer, "server_close", new=fail_before_close
            ):
                with self.assertRaisesRegex(RuntimeError, "thread failed"):
                    server.start()

        self.assertEqual(len(captured), 1)
        self.addCleanup(captured[0].socket.close)
        self.assertIs(server._server, captured[0])
        self.assertIsNone(server._thread)
        occupied = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(OSError):
                occupied.bind(("127.0.0.1", port))
        finally:
            occupied.close()

        server.stop()
        replacement = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            replacement.bind(("127.0.0.1", port))
        finally:
            replacement.close()


if __name__ == "__main__":
    unittest.main()
