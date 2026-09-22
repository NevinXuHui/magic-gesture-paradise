"""Exercise the real native model and the browser-compatible result contract."""
import unittest
import cv2
import numpy as np
from server import Inference

class NativeInferenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = Inference()
    @classmethod
    def tearDownClass(cls):
        cls.engine.model.close()
    def test_real_model_blank_frame(self):
        ok, jpeg = cv2.imencode('.jpg', np.zeros((480, 640, 3), dtype=np.uint8))
        self.assertTrue(ok)
        for _ in range(2):
            result = self.engine.recognize(jpeg.tobytes())
            self.assertGreater(result['ms'], 0)
            self.assertEqual(result['result'], dict(landmarks=[], worldLandmarks=[], gestures=[], handedness=[]))
    def test_invalid_image(self):
        with self.assertRaises(ValueError):
            self.engine.recognize(b'not an image')
    def test_busy_drops_frame(self):
        _, jpeg = cv2.imencode('.jpg', np.zeros((480, 640, 3), dtype=np.uint8))
        with self.engine.lock:
            self.assertIsNone(self.engine.recognize(jpeg.tobytes()))
if __name__ == '__main__':
    unittest.main()
