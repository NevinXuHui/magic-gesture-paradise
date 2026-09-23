"""
claw_client 发送验证脚本
用法: ros2 run claw_client test_send [--data '<json>']
默认发送 bind_status_query 测试包，deviceId 从 keep_alive 事件中自动获取
"""
import json
import sys
import time
import argparse

import rclpy

from claw_client.claw_client_node import SpeechCoreClientNode


def _wait_for_device_id(node: SpeechCoreClientNode, timeout_sec: float = 5.0) -> str:
    """从 keep_alive 事件中获取真实 deviceId，超时返回空字符串"""
    device_id = []

    def _capture(event_json: str):
        if device_id:
            return
        try:
            data = json.loads(event_json)
            did = data.get("deviceId", "")
            if did:
                device_id.append(did)
        except Exception:
            pass

    orig = node.on_speech_event
    node.on_speech_event = _capture

    node.get_logger().info("等待 keep_alive 获取 deviceId...")
    deadline = time.time() + timeout_sec
    while not device_id and time.time() < deadline:
        rclpy.spin_once(node, timeout_sec=0.2)

    node.on_speech_event = orig

    if device_id:
        node.get_logger().info(f"获取到 deviceId: {device_id[0]}")
        return device_id[0]
    else:
        node.get_logger().warning("未能获取 deviceId，使用空字符串")
        return ""


def main(args=None):
    parser = argparse.ArgumentParser(description='claw_client 发送验证')
    parser.add_argument('--data', type=str, default=None,
                        help='要发送的 JSON 字符串，默认使用 bind_status_query 测试包')
    parsed, ros_args = parser.parse_known_args()

    rclpy.init(args=ros_args or None)
    node = SpeechCoreClientNode()

    # 等待 SpeechCore 服务就绪（最多 5 秒）
    node.get_logger().info("等待 /homi_speech/sigc_data_service 就绪...")
    if not node.platform_client.wait_for_service(timeout_sec=5.0):
        node.get_logger().error("超时：sigc_data_service 未就绪，请确认 homi_speech 已启动")
        rclpy.shutdown()
        sys.exit(1)

    # 构造发送数据
    if parsed.data:
        data = parsed.data
    else:
        device_id = _wait_for_device_id(node, timeout_sec=15.0)
        ts = str(int(time.time() * 1000))
        payload = {
            "deviceId": device_id,
            "domain": "ROBOT_BUSINESS_DEVICE",
            "event": "bind_status_query",
            "eventId": ts,
            "seq": ts,
            "response": "false",
            "body": {}
        }
        data = json.dumps(payload, ensure_ascii=False)

    node.get_logger().info(f"发送数据:\n{json.dumps(json.loads(data), indent=2, ensure_ascii=False)}")

    ret = node.send(data)

    if ret == 0:
        node.get_logger().info("✓ 发送成功 (error_code=0)，继续监听事件中... (Ctrl+C 退出)")
    else:
        node.get_logger().error(f"✗ 发送失败 (error_code={ret})，继续监听事件中... (Ctrl+C 退出)")

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("检测到 KeyboardInterrupt，正在关闭...")
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
