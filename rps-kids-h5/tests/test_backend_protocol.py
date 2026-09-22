"""Exercise HTTP snapshots without a robot camera; inference has separate real-model tests."""
import importlib.util
import json
from pathlib import Path
import threading
import time
import unittest
from urllib.request import urlopen

spec = importlib.util.spec_from_file_location('robot_backend', Path(__file__).resolve().parents[1] / 'backend/main.py')
backend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backend)

class ProtocolTest(unittest.TestCase):
    def setUp(self):
        backend.latest_frame = b'jpeg-test'
        backend.latest_update = time.monotonic()
        backend.latest_recognition = dict(sequence=7, width=640, height=360, ms=12,
            result=dict(landmarks=[], worldLandmarks=[], gestures=[], handedness=[]))
        self.server = backend.CameraServer(('127.0.0.1', 0), backend.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s' % self.server.server_port
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
    def get(self, path):
        with urlopen(self.url+path) as response:
            return json.load(response)
    def test_snapshot_and_optional_preview(self):
        result = self.get('/api/recognition?preview=0')
        self.assertEqual(result['sequence'], 7)
        self.assertNotIn('preview', result)
        self.assertGreaterEqual(result['ageMs'], 0)
        self.assertEqual(self.get('/api/recognition?preview=1')['preview'], 'anBlZy10ZXN0')
    def test_stale_camera_is_not_ready(self):
        backend.latest_update = time.monotonic()-5
        self.assertFalse(self.get('/api/status')['ready'])
        from urllib.error import HTTPError
        with self.assertRaises(HTTPError) as ctx:
            self.get('/api/recognition')
        self.assertEqual(ctx.exception.code, 503)

class RuntimeLifecycleTest(unittest.TestCase):
    def test_ready_and_stop_protocol(self):
        import os, subprocess, sys
        backend_path = Path(__file__).resolve().parents[1] / 'backend/main.py'
        proc = subprocess.run([sys.executable, str(backend_path)],
            input='{"event":"runtime_init"}\n{"event":"app_stop"}\n',
            text=True, capture_output=True, timeout=20,
            env=dict(os.environ, SMARTAPP_DYNAMIC_PORT='0'))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([json.loads(line) for line in proc.stdout.splitlines()], [{"event":"app_ready"}])

if __name__ == '__main__':
    unittest.main()
