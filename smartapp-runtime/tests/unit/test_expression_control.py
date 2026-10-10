import importlib.util
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


SCRIPT = Path(__file__).resolve().parents[2] / "renderer" / "expression-control.py"


class ExpressionControlTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location("expression_control", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.directory = tempfile.TemporaryDirectory()
        self.path = str(Path(self.directory.name) / "control.sock")
        self.stop = threading.Event()
        self.calls = []

    def tearDown(self):
        self.stop.set()
        if hasattr(self, "thread"):
            self.thread.join(2)
            self.assertFalse(self.thread.is_alive())
        self.directory.cleanup()

    def start_server(self, callback):
        self.thread = threading.Thread(
            target=self.module.serve, args=(callback, self.path, self.stop)
        )
        self.thread.start()
        deadline = time.monotonic() + 2
        while not Path(self.path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(Path(self.path).exists())

    def test_multiple_launches_use_the_same_live_controller(self):
        def disable(action, deadline):
            self.calls.append(action)

        self.start_server(disable)
        self.module.request(self.path)
        self.module.request(self.path)
        self.assertEqual(self.calls, ["disable", "disable"])
        self.assertTrue(self.thread.is_alive())

    def test_service_failure_is_reported_without_stopping_server(self):
        def disable(action, deadline):
            if not self.calls:
                self.calls.append("failed")
                raise RuntimeError("expression config failed")
            self.calls.append("success")

        self.start_server(disable)
        with self.assertRaisesRegex(RuntimeError, "expression config failed"):
            self.module.request(self.path)
        self.module.request(self.path)
        self.assertEqual(self.calls, ["failed", "success"])

    def test_restore_runs_after_a_pending_disable(self):
        entered = threading.Event()
        release = threading.Event()
        errors = []

        def configure(action, deadline):
            if action == "disable":
                entered.set()
                release.wait(2)
            self.calls.append(action)

        def call(action):
            try:
                self.module.request(self.path, action=action)
            except Exception as error:
                errors.append(error)

        self.start_server(configure)
        disable = threading.Thread(target=call, args=("disable",))
        restore = threading.Thread(target=call, args=("restore",))
        disable.start()
        self.assertTrue(entered.wait(1))
        restore.start()
        release.set()
        disable.join(2)
        restore.join(2)
        self.assertFalse(disable.is_alive())
        self.assertFalse(restore.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.calls, ["disable", "restore"])

    def test_expired_queued_request_does_not_change_expression(self):
        self.start_server(lambda action, deadline: self.calls.append(action))
        with self.assertRaisesRegex(RuntimeError, "expired"):
            self.module.request(self.path, timeout=0)
        self.assertEqual(self.calls, [])

    def test_absent_daemon_is_distinct_from_service_failure(self):
        with self.assertRaises(self.module.UnavailableError):
            self.module.request(self.path)

    def test_service_discovery_and_response_share_one_deadline(self):
        controller = self.module.RosController.__new__(self.module.RosController)
        controller.service = SimpleNamespace(Request=SimpleNamespace)
        future = Mock()
        future.result.return_value = SimpleNamespace(success=True)
        controller.client = Mock()
        controller.client.wait_for_service.return_value = True
        controller.client.call_async.return_value = future
        event = Mock()
        event.wait.return_value = True
        with patch.object(self.module.time, "monotonic", side_effect=[0, 4, 4]), patch.object(self.module.threading, "Event", return_value=event):
            controller.configure("disable", deadline=8)
        controller.client.wait_for_service.assert_called_once_with(timeout_sec=5)
        event.wait.assert_called_once_with(4)


if __name__ == "__main__":
    unittest.main()
