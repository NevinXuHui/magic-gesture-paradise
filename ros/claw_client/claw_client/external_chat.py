"""
外部 ROS2 同步收发工具。

向 hermes_bridge 发送 app_msg_request，并同步等待 device_msg_response。

用法:
  ros2 run claw_client external_chat --text "你好"
  ros2 run claw_client external_chat --text "今天天气怎么样" --timeout 60
  ros2 run claw_client external_chat --text "ping" --device-id test-device --stream

说明:
  - 请求: publish /homi_speech/sigc_event_topic (SIGCEvent)
  - 响应: serve  /homi_speech/sigc_data_service (SIGCData)
  - 若本机已有 SpeechCore 提供 sigc_data_service，本工具会冲突；
    可用 --no-service 只发请求，不接响应。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import rclpy
from rclpy.node import Node

from homi_speech_interface.msg import SIGCEvent
from homi_speech_interface.srv import SIGCData


DEFAULT_TOPIC = "/homi_speech/sigc_event_topic"
DEFAULT_SERVICE = "/homi_speech/sigc_data_service"


class ExternalChatNode(Node):
    """同步发送 app_msg_request，并接收 device_msg_response。"""

    def __init__(
        self,
        topic: str = DEFAULT_TOPIC,
        service: str = DEFAULT_SERVICE,
        provide_service: bool = True,
        show_stream: bool = False,
    ):
        super().__init__("external_chat")
        self._topic = topic
        self._service = service
        self._provide_service = provide_service
        self._show_stream = show_stream

        self._lock = threading.Lock()
        # event_id -> list[{text, is_final, raw, ts}]
        self._responses: Dict[str, List[Dict[str, Any]]] = {}
        self._events = threading.Event()

        self._pub = self.create_publisher(SIGCEvent, self._topic, 10)

        self._srv = None
        if self._provide_service:
            self._srv = self.create_service(
                SIGCData,
                self._service,
                self._on_sigc_data,
            )
            self.get_logger().info(f"已提供响应服务: {self._service}")
        else:
            self.get_logger().warning(
                f"未提供 {self._service}（--no-service），只能发请求，收不到响应"
            )

        self.get_logger().info(f"请求话题: {self._topic}")

    def _on_sigc_data(self, request: SIGCData.Request, response: SIGCData.Response):
        raw = request.data or ""
        try:
            data = json.loads(raw)
        except Exception as e:
            self.get_logger().error(f"响应 JSON 解析失败: {e}; raw={raw[:200]}")
            response.error_code = -1
            return response

        event = data.get("event", "")
        event_id = str(data.get("eventId", "") or "")
        body = data.get("body", {}) or {}
        texts: List[str] = []
        for item in body.get("content", []) or []:
            if isinstance(item, dict) and item.get("text"):
                texts.append(str(item["text"]))
        text = "\n".join(texts).strip()
        is_final = body.get("stopReason") == "stop"

        if event != "device_msg_response":
            self.get_logger().debug(f"忽略非响应事件: event={event} event_id={event_id}")
            response.error_code = 0
            return response

        if self._show_stream or is_final:
            tag = "FINAL" if is_final else "STREAM"
            preview = text.replace("\n", "\\n")
            if len(preview) > 120:
                preview = preview[:120] + "..."
            self.get_logger().info(
                f"[{tag}] event_id={event_id} text={preview}"
            )

        if event_id:
            with self._lock:
                self._responses.setdefault(event_id, []).append(
                    {
                        "text": text,
                        "is_final": is_final,
                        "raw": data,
                        "ts": time.time(),
                    }
                )
            self._events.set()

        # bridge 依赖 error_code=0 判定发送成功
        response.error_code = 0
        return response

    def wait_for_subscribers(self, timeout_sec: float = 3.0) -> bool:
        """等待至少 1 个订阅者（hermes_bridge）。"""
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            count = self._pub.get_subscription_count()
            if count > 0:
                self.get_logger().info(f"已匹配订阅者: {count}")
                return True
            rclpy.spin_once(self, timeout_sec=0.1)
        self.get_logger().warning(
            f"超时未匹配到 {self._topic} 订阅者，仍会发送（可能无人接收）"
        )
        return False

    def send_request(
        self,
        text: str,
        device_id: str = "test-device",
        event_id: Optional[str] = None,
        domain: str = "OPEN_CLAW",
    ) -> str:
        """发送一条 app_msg_request，返回 event_id。"""
        ts = str(int(time.time() * 1000))
        eid = event_id or f"evt-ext-{ts}"
        payload = {
            "deviceId": device_id,
            "domain": domain,
            "event": "app_msg_request",
            "eventId": eid,
            "seq": ts,
            "body": {"text": text},
        }
        msg = SIGCEvent()
        msg.event = json.dumps(payload, ensure_ascii=False)

        # 清掉同 event_id 的旧响应
        with self._lock:
            self._responses.pop(eid, None)
        self._events.clear()

        self._pub.publish(msg)
        self.get_logger().info(
            f"已发送 app_msg_request: device_id={device_id} event_id={eid} text={text[:80]}"
        )
        return eid

    def wait_response(
        self,
        event_id: str,
        timeout_sec: float = 30.0,
        spin_period: float = 0.1,
    ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
        """
        同步等待指定 event_id 的最终响应。

        Returns:
            (final_text, all_chunks)
        """
        if not self._provide_service:
            self.get_logger().warning("未提供响应服务，无法同步等待")
            return None, []

        deadline = time.time() + timeout_sec
        final_text: Optional[str] = None
        chunks: List[Dict[str, Any]] = []

        while time.time() < deadline:
            rclpy.spin_once(self, timeout_sec=spin_period)
            with self._lock:
                chunks = list(self._responses.get(event_id, []))
            for item in chunks:
                if item.get("text"):
                    final_text = item["text"]
                if item.get("is_final"):
                    return final_text, chunks

        # 超时：返回已收到的最后一段（可能没有 final）
        return final_text, chunks

    def chat_once(
        self,
        text: str,
        device_id: str = "test-device",
        timeout_sec: float = 30.0,
        event_id: Optional[str] = None,
        wait_subscribers: bool = True,
    ) -> Tuple[str, Optional[str], List[Dict[str, Any]]]:
        """发送并同步等待最终响应。"""
        if wait_subscribers:
            self.wait_for_subscribers(timeout_sec=3.0)
        eid = self.send_request(text=text, device_id=device_id, event_id=event_id)
        if not self._provide_service:
            return eid, None, []
        reply, chunks = self.wait_response(eid, timeout_sec=timeout_sec)
        return eid, reply, chunks


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="外部 ROS2 同步收发：app_msg_request ↔ device_msg_response"
    )
    p.add_argument("--text", "-t", default="你好，外部 ROS2 同步请求", help="请求文本")
    p.add_argument("--device-id", default="test-device", help="deviceId / chat 映射源")
    p.add_argument("--timeout", type=float, default=30.0, help="等待最终响应超时秒数")
    p.add_argument("--event-id", default=None, help="自定义 eventId（默认自动生成）")
    p.add_argument("--topic", default=DEFAULT_TOPIC, help="请求话题")
    p.add_argument("--service", default=DEFAULT_SERVICE, help="响应服务名")
    p.add_argument(
        "--no-service",
        action="store_true",
        help="不提供 sigc_data_service（只发不收）",
    )
    p.add_argument(
        "--stream",
        action="store_true",
        help="打印流式中间包（默认只打印最终结果）",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 打印结果（便于脚本消费）",
    )
    p.add_argument(
        "--no-wait-sub",
        action="store_true",
        help="不等待订阅者匹配，直接发送",
    )
    return p


def main(args=None):
    parser = build_arg_parser()
    parsed, ros_args = parser.parse_known_args(args=args)

    rclpy.init(args=ros_args or None)
    node = ExternalChatNode(
        topic=parsed.topic,
        service=parsed.service,
        provide_service=not parsed.no_service,
        show_stream=parsed.stream,
    )

    exit_code = 0
    try:
        t0 = time.time()
        event_id, reply, chunks = node.chat_once(
            text=parsed.text,
            device_id=parsed.device_id,
            timeout_sec=parsed.timeout,
            event_id=parsed.event_id,
            wait_subscribers=not parsed.no_wait_sub,
        )
        elapsed = time.time() - t0

        if parsed.json:
            out = {
                "ok": bool(reply),
                "event_id": event_id,
                "text": parsed.text,
                "reply": reply,
                "elapsed_sec": round(elapsed, 3),
                "chunk_count": len(chunks),
                "final": any(c.get("is_final") for c in chunks),
            }
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print()
            print("=" * 60)
            print(f"event_id : {event_id}")
            print(f"request  : {parsed.text}")
            print(f"elapsed  : {elapsed:.2f}s")
            print(f"chunks   : {len(chunks)}")
            if reply:
                print("-" * 60)
                print("reply:")
                print(reply)
                print("=" * 60)
            elif parsed.no_service:
                print("-" * 60)
                print("已发送请求（--no-service，不接收响应）")
                print("=" * 60)
            else:
                print("-" * 60)
                print("⚠ 超时未收到 device_msg_response")
                print("检查:")
                print("  1) hermes_bridge 是否在跑（:8800）")
                print("  2) hermes gateway 是否 connected")
                print("  3) 本节点是否成功提供 /homi_speech/sigc_data_service")
                print("  4) 是否与 SpeechCore 抢占了同一 service")
                print("=" * 60)
                exit_code = 2
                if not any(c.get("is_final") for c in chunks) and reply is None:
                    exit_code = 2

        if not parsed.no_service and not reply:
            exit_code = 2
    except KeyboardInterrupt:
        node.get_logger().info("用户中断")
        exit_code = 130
    finally:
        node.destroy_node()
        rclpy.shutdown()

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
