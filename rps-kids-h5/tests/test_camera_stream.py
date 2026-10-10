import importlib.util
import unittest
from pathlib import Path


class CameraStreamTest(unittest.TestCase):
    def stream(self, source, call):
        path = Path(__file__).resolve().parents[1] / "backend" / "camera_stream.py"
        spec = importlib.util.spec_from_file_location("camera_stream", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.CameraStream(source, call=call, log=lambda message: None)

    def test_forehead_opens_and_closes(self):
        calls = []
        with self.stream("forehead", lambda action: calls.append(action)):
            self.assertEqual(calls, ["start"])
        self.assertEqual(calls, ["start", "stop"])

    def test_neck_does_not_touch_forehead(self):
        calls = []
        with self.stream("neck", lambda action: calls.append(action)):
            pass
        self.assertEqual(calls, [])

    def test_startup_failure_releases_stream(self):
        calls = []
        with self.assertRaisesRegex(ValueError, "inference failed"):
            with self.stream("forehead", lambda action: calls.append(action)):
                raise ValueError("inference failed")
        self.assertEqual(calls, ["start", "stop"])

    def test_failed_open_does_not_release_unacquired_stream(self):
        calls = []
        def call(action):
            calls.append(action)
            raise RuntimeError("service unavailable")
        with self.assertRaisesRegex(RuntimeError, "service unavailable"):
            with self.stream("forehead", call):
                pass
        self.assertEqual(calls, ["start"])

    def test_failed_close_does_not_mask_original_error(self):
        def call(action):
            if action == "stop":
                raise RuntimeError("close failed")
        with self.assertRaisesRegex(ValueError, "original"):
            with self.stream("forehead", call):
                raise ValueError("original")
