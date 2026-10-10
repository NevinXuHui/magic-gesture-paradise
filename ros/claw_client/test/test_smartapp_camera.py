import ast
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict


class SmartAppCameraTest(unittest.TestCase):
    def forward(self, app_id, data):
        source = Path(__file__).resolve().parents[1] / "claw_client" / "hermes_bridge.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        method = next(node for node in ast.walk(tree)
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_process_smartapp_start")
        namespace = {"uuid": uuid, "time": __import__("time"), "Dict": Dict, "Any": Any}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        commands = []
        bridge = SimpleNamespace(
            _send_runtime_command=lambda command, timeout: commands.append(command) or True,
            _game_sessions_lock=threading.Lock(), _active_game_sessions={},
            logger=SimpleNamespace(info=lambda message: None, error=lambda message: None),
        )
        body = {"sessionId": "camera-test", "appid": app_id, "version": "0.1.12",
                "packageUrl": "https://example.invalid/game.tar.gz", "packageSize": 100,
                "sha256": "a" * 64, "data": data}
        namespace["_process_smartapp_start"](bridge, "test-event", body)
        return commands[0]["initData"]

    def test_rps_defaults_to_forehead_without_mutating_event(self):
        data = {"locale": "zh-CN"}
        self.assertEqual(self.forward("rock_paper_scissors", data),
                         {"locale": "zh-CN", "cameraSource": "forehead"})
        self.assertEqual(data, {"locale": "zh-CN"})

    def test_explicit_neck_is_preserved(self):
        self.assertEqual(self.forward("rock_paper_scissors", {"cameraSource": "neck"}),
                         {"cameraSource": "neck"})

    def test_cloud_rps_id_defaults_to_forehead(self):
        self.assertEqual(self.forward("llm_app_2d424502d2f34173bfa77c704a823bcf", {}),
                         {"cameraSource": "forehead"})

    def test_cloud_rps_id_preserves_explicit_neck(self):
        self.assertEqual(self.forward("llm_app_2d424502d2f34173bfa77c704a823bcf",
                                     {"cameraSource": "neck"}),
                         {"cameraSource": "neck"})

    def test_other_apps_receive_original_data(self):
        self.assertEqual(self.forward("cloud_show_display", {"word": "hello"}),
                         {"word": "hello"})


if __name__ == "__main__":
    unittest.main()
