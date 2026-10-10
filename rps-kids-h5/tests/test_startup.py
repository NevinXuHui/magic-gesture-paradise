import importlib.util
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class StartupTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / 'backend/main.py'
        spec = importlib.util.spec_from_file_location('startup_backend', path)
        self.backend = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, cv2=types.ModuleType('cv2')):
            spec.loader.exec_module(self.backend)

    def exercise(self, open_error=None, model_error=None):
        loading = threading.Event()
        opening = threading.Event()
        calls = []
        model = Mock()

        def load():
            loading.set()
            if not opening.wait(2):
                raise AssertionError('camera and model initialization must overlap')
            if model_error:
                raise model_error
            return model

        class Stream:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                calls.append('start')
                opening.set()
                if not loading.wait(2):
                    raise AssertionError('model initialization did not start')
                if open_error:
                    raise open_error
                return self

            def __exit__(self, *args):
                calls.append('stop')

        with patch.dict(sys.modules, camera_stream=types.SimpleNamespace(CameraStream=Stream)), \
                patch.object(self.backend.signal, 'signal'), \
                patch.object(self.backend, 'read_runtime_init', return_value={'data': {'cameraSource': 'forehead'}}), \
                patch.object(self.backend, 'initialize_inference', side_effect=load, create=True), \
                patch.object(self.backend, 'run_game') as run:
            error = open_error or model_error
            if error:
                with self.assertRaisesRegex(RuntimeError, str(error)):
                    self.backend.main()
                run.assert_not_called()
            else:
                self.backend.main()
                run.assert_called_once_with(model)
        return calls, model

    def test_camera_and_model_start_in_parallel(self):
        calls, _ = self.exercise()
        self.assertEqual(calls, ['start', 'stop'])

    def test_camera_failure_closes_initialized_model(self):
        calls, model = self.exercise(open_error=RuntimeError('camera unavailable'))
        self.assertEqual(calls, ['start'])
        model.close.assert_called_once_with()

    def test_model_failure_releases_camera(self):
        calls, _ = self.exercise(model_error=RuntimeError('model unavailable'))
        self.assertEqual(calls, ['start', 'stop'])
