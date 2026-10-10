import unittest
from pathlib import Path


class RendererScriptTests(unittest.TestCase):
    def test_disable_expression_uses_cyclonedds_ros_environment(self):
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "renderer" / "disable-expression.sh"

        content = script.read_text(encoding="utf-8")

        self.assertIn("unset COLCON_PREFIX_PATH AMENT_PREFIX_PATH CMAKE_PREFIX_PATH AMENT_CURRENT_PREFIX", content)
        self.assertIn("/opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash", content)
        self.assertIn("RMW_IMPLEMENTATION=rmw_cyclonedds_cpp", content)
        self.assertIn("CYCLONEDDS_URI=", content)

    def test_screen_renderer_reclaims_mpv_without_reemitting_ready(self):
        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "renderer" / "screen-renderer.js"

        content = script.read_text(encoding="utf-8")

        self.assertIn("rendererReadyEmitted", content)
        self.assertIn("scheduleDisplayReclaim", content)
        self.assertIn("MPV path changed", content)
        self.assertIn("FFmpeg exited; reclaiming display", content)
        self.assertIn("接管屏幕失败，将重试", content)
