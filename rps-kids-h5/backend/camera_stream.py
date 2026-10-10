import os
import json
import subprocess
import sys

_ros_environment = None


def call_stream(action):
    global _ros_environment
    # 独立系统 Python 进程加载 ROS，避免游戏包内依赖覆盖原厂 rclpy 依赖。
    script = """
set -e
unset COLCON_PREFIX_PATH AMENT_PREFIX_PATH CMAKE_PREFIX_PATH AMENT_CURRENT_PREFIX
source /opt/ros/foxy/setup.bash
source /usr/bin/cmcc_robot/install/setup.bash
source /opt/ros/unitree_ros2/cyclonedds_ws/install/setup.bash
export ROS_DOMAIN_ID=2 RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI='<CycloneDDS><Domain><General><Interfaces><NetworkInterface name="eth0" priority="default" multicast="default" /></Interfaces><AllowMulticast>spdp</AllowMulticast></General></Domain></CycloneDDS>'
exec /usr/bin/python3 "$1" "$2"
"""
    try:
        command = ["bash", "-c", script, "smartapp-camera", os.path.abspath(__file__), action]
        if _ros_environment is not None:
            command = ["/usr/bin/python3", os.path.abspath(__file__), action]
        result = subprocess.run(
            command, env=_ros_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20 if action == "start" else 4,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("额头视频流{0}超时".format(action)) from error
    if result.returncode:
        raise RuntimeError("额头视频流{0}失败：{1}".format(action, result.stderr.strip()))
    if action == "start":
        _ros_environment = json.loads(result.stdout.strip().splitlines()[-1])


class CameraStream:
    def __init__(self, source, call=call_stream, log=None):
        self.source = source
        self.call = call
        self.log = log or (lambda message: print(message, file=sys.stderr, flush=True))
        self.acquired = False

    def __enter__(self):
        if self.source == "forehead":
            self.call("start")
            self.acquired = True
            self.log("额头 JPEG 视频流已自动开启")
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.acquired:
            self.acquired = False
            try:
                self.call("stop")
                self.log("额头 JPEG 视频流已自动释放")
            except (OSError, RuntimeError) as error:
                self.log("释放额头视频流失败：{0}".format(error))
        return False


def ros_request(action):
    import rclpy
    from homi_speech_interface.srv import GetVideoStream, EndVideoStream

    starting = action == "start"
    service_type = GetVideoStream if starting else EndVideoStream
    service_name = "/video_gst/get_video_service" if starting else "/video_gst/end_video_service"
    timeout = 8 if starting else 1
    rclpy.init(args=[])
    node = rclpy.create_node("smartapp_forehead_stream")
    try:
        client = node.create_client(service_type, service_name)
        if not client.wait_for_service(timeout_sec=timeout):
            raise RuntimeError("服务不可用：" + service_name)
        request = service_type.Request()
        request.resolution = "jpeg"
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=timeout)
        if not future.done():
            raise RuntimeError("服务调用超时：" + service_name)
        response = future.result()
        if not response or not response.success:
            raise RuntimeError("服务返回失败：" + service_name)
        if starting and response.socket_path != "/tmp/foo_jpeg":
            raise RuntimeError("额头服务返回了非预期视频流：" + response.socket_path)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2 or sys.argv[1] not in ("start", "stop"):
            raise ValueError("用法：camera_stream.py start|stop")
        ros_request(sys.argv[1])
        if sys.argv[1] == "start":
            print(json.dumps(dict(os.environ)))
    except Exception as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
