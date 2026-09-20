import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.config import LoggingConfig, PathsConfig, RuntimeConfig
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.logging import configure_logging, sanitize_url


class LoggingTests(unittest.TestCase):
    def tearDown(self):
        root = logging.getLogger()
        handlers = list(root.handlers)
        root.handlers = []
        for handler in handlers:
            handler.close()

    def test_sanitize_url_removes_credentials_query_and_fragment(self):
        value = "https://alice:top-secret@example.test:8443/apps/demo?a=token#section"

        sanitized = sanitize_url(value)

        self.assertEqual(sanitized, "https://example.test:8443/apps/demo")
        self.assertNotIn("alice", sanitized)
        self.assertNotIn("top-secret", sanitized)
        self.assertNotIn("token", sanitized)

    def test_sanitize_url_fails_closed_for_malformed_or_non_url_input(self):
        for value in (
            "https://alice:top-secret@[::1/path?token=secret",
            "alice:top-secret@example.test?token=secret",
            "https://example.test/\x00secret?token=secret",
        ):
            with self.subTest(value=value):
                sanitized = sanitize_url(value)
                self.assertEqual(sanitized, "<invalid-url>")
                self.assertNotIn("secret", sanitized)

    def test_json_lines_have_fixed_fields_allowlisted_correlation_and_no_payload(self):
        stream = io.StringIO()
        config = RuntimeConfig(logging=LoggingConfig(level="DEBUG", target="stderr"))

        with patch("smartapp_runtime.logging.sys.stderr", stream):
            configure_logging(config)
        logging.getLogger("smartapp.test").info(
            "accepted",
            extra={
                "requestId": "request-1",
                "sessionId": 7,
                "appId": "demo",
                "state": "RUNNING",
                "errorCode": "NONE",
                "initData": {"password": "init-secret"},
                "data": {"token": "data-secret"},
                "unknown": "must-not-appear",
            },
        )

        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(
            set(record),
            {
                "timestamp", "level", "logger", "message", "requestId",
                "sessionId", "appId", "state", "errorCode",
            },
        )
        self.assertEqual(record["level"], "INFO")
        self.assertEqual(record["logger"], "smartapp.test")
        self.assertEqual(record["message"], "accepted")
        self.assertEqual(record["sessionId"], "7")
        self.assertNotIn("secret", lines[0])
        self.assertNotIn("unknown", lines[0])

    def test_message_urls_are_redacted_before_serialization(self):
        stream = io.StringIO()
        config = RuntimeConfig(logging=LoggingConfig(level="INFO", target="stderr"))
        with patch("smartapp_runtime.logging.sys.stderr", stream):
            configure_logging(config)

        logging.getLogger("smartapp.test").error(
            "download failed: https://alice:top-secret@example.test/pkg?token=data-secret"
        )

        record = json.loads(stream.getvalue())
        self.assertEqual(record["message"], "download failed: https://example.test/pkg")
        self.assertNotIn("secret", stream.getvalue())

    def test_structured_message_and_args_are_replaced_without_stringifying_payloads(self):
        stream = io.StringIO()
        config = RuntimeConfig(logging=LoggingConfig(level="INFO", target="stderr"))
        with patch("smartapp_runtime.logging.sys.stderr", stream):
            configure_logging(config)

        logging.getLogger("smartapp.test").info(
            "command=%s", {"initData": {"password": "args-secret"}}
        )
        logging.getLogger("smartapp.test").info(
            {"event": "cloud_data", "data": {"token": "message-secret"}}
        )

        output = stream.getvalue()
        self.assertEqual(len(output.splitlines()), 2)
        self.assertNotIn("initData", output)
        self.assertNotIn('"data"', output)
        self.assertNotIn("secret", output)
        for line in output.splitlines():
            self.assertIn("redacted", json.loads(line)["message"])

    def test_handler_write_failure_never_reflects_original_record(self):
        class BrokenStream:
            def write(self, _value):
                raise OSError("write failed")

            def flush(self):
                return None

        diagnostic = io.StringIO()
        config = RuntimeConfig(logging=LoggingConfig(level="INFO", target="stderr"))
        with patch("smartapp_runtime.logging.sys.stderr", BrokenStream()):
            configure_logging(config)

        with patch("sys.stderr", diagnostic):
            logging.getLogger("smartapp.test").error(
                "payload=%s", {"data": {"password": "handler-secret"}}
            )

        self.assertNotIn("payload", diagnostic.getvalue())
        self.assertNotIn("data", diagnostic.getvalue())
        self.assertNotIn("handler-secret", diagnostic.getvalue())

    def test_file_open_uses_trusted_parent_fd_when_path_is_replaced(self):
        with tempfile.TemporaryDirectory() as raw:
            paths = RuntimePaths.from_root(Path(raw) / "runtime")
            paths.ensure_layout()
            original_logs = paths.root / "logs-original"
            outside = Path(raw) / "outside"
            outside.mkdir()
            config = RuntimeConfig(
                paths=PathsConfig(
                    root=paths.root,
                    socket=paths.root / "run/runtime.sock",
                    log=paths.logs_root / "runtime.jsonl",
                ),
                logging=LoggingConfig(level="INFO", target="file"),
            )
            real_open = __import__("os").open
            swapped = []

            def racing_open(path, flags, *args, **kwargs):
                if (
                    path == "runtime.jsonl"
                    and kwargs.get("dir_fd") is not None
                    and not swapped
                ):
                    paths.logs_root.rename(original_logs)
                    paths.logs_root.symlink_to(outside, target_is_directory=True)
                    swapped.append(True)
                return real_open(path, flags, *args, **kwargs)

            with patch("smartapp_runtime.logging.os.open", side_effect=racing_open):
                configure_logging(config)
            logging.getLogger("smartapp.test").info("safe")
            logging.getLogger().handlers[0].close()

            self.assertTrue(swapped)
            self.assertFalse((outside / "runtime.jsonl").exists())
            self.assertTrue((original_logs / "runtime.jsonl").is_file())

    def test_reconfiguration_replaces_and_closes_handlers_without_duplicates(self):
        first_stream = io.StringIO()
        second_stream = io.StringIO()
        config = RuntimeConfig(logging=LoggingConfig(level="INFO", target="stderr"))
        with patch("smartapp_runtime.logging.sys.stderr", first_stream):
            configure_logging(config)
        first = logging.getLogger().handlers[0]
        with patch("smartapp_runtime.logging.sys.stderr", second_stream):
            configure_logging(config)

        self.assertEqual(len(logging.getLogger().handlers), 1)
        self.assertTrue(first._closed)
        logging.getLogger("smartapp.test").warning("once")
        self.assertEqual(first_stream.getvalue(), "")
        self.assertEqual(len(second_stream.getvalue().splitlines()), 1)

    def test_file_target_writes_utf8_json_after_parent_exists(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "runtime"
            log_path = root / "logs" / "runtime.jsonl"
            log_path.parent.mkdir(parents=True)
            config = RuntimeConfig(
                paths=PathsConfig(root=root, log=log_path),
                logging=LoggingConfig(level="INFO", target="file"),
            )

            configure_logging(config)
            logging.getLogger("smartapp.test").info("中文")
            logging.getLogger().handlers[0].flush()

            record = json.loads(log_path.read_text(encoding="utf-8"))
            self.assertEqual(record["message"], "中文")


if __name__ == "__main__":
    unittest.main()
