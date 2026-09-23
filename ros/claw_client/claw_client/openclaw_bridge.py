"""
OpenClaw Bridge 节点

接收 SpeechCore 下发的 app_msg_request（domain=OPEN_CLAW），
将 body.text 发给 OpenClaw Webhook，通过 SSE 实时接收回复后
以 device_msg_response 格式回发给 SpeechCore。
"""
import json
import time
import threading
import urllib.request
import urllib.error
import urllib.parse
import hmac
import hashlib
from typing import Optional
import queue

import rclpy

from homi_speech_interface.srv import SIGCData

try:
    from .claw_client_node import SpeechCoreClientNode
    from .logging_setup import setup_logging
except ImportError:
    from claw_client.claw_client_node import SpeechCoreClientNode
    from claw_client.logging_setup import setup_logging


class OpenClawBridge(SpeechCoreClientNode):
    """
    OpenClaw 桥接节点

    事件流:
      SpeechCore --[sigc_event_topic]--> on_speech_event
        --> 识别 app_msg_request / OPEN_CLAW
        --> 线程: POST text → OpenClaw Webhook → SSE 实时接收回复
        --> send device_msg_response → SpeechCore
    """

    def __init__(self):
        super().__init__(node_name='openclaw_bridge')

        # OpenClaw Webhook 配置
        self.declare_parameter('openclaw_url', 'http://127.0.0.1:8088')
        self.declare_parameter('openclaw_channel', 'xiaoli-chat')
        self.declare_parameter('openclaw_secret', 'd9ec7e44bb4ddf33329dbef245befce55e3bcbfa909d2e349f16c033ae260b57')
        self.declare_parameter('openclaw_post_timeout_sec', 120)
        self.declare_parameter('openclaw_reply_timeout_sec', 60)
        self.declare_parameter('use_sse', True)  # 是否使用 SSE

        # 异步请求 OpenClaw 服务鉴权信息（仅用于接收平台数据，不改变业务逻辑）
        self.logger.info("发送 OpenClaw 服务鉴权请求（异步）...")
        self.request_service_credential_async("openclaw", callback=None)

        # 读取参数（使用配置文件中的值，不会被平台鉴权信息覆盖）
        self.openclaw_url = self.get_parameter('openclaw_url').value
        self.openclaw_channel = self.get_parameter('openclaw_channel').value
        self.openclaw_secret = self.get_parameter('openclaw_secret').value
        self.openclaw_post_timeout = self.get_parameter('openclaw_post_timeout_sec').value
        self.openclaw_reply_timeout = self.get_parameter('openclaw_reply_timeout_sec').value
        self.use_sse = self.get_parameter('use_sse').value

        # 当前对话的 user_id（/clear 信号时重置）
        self._user_id = f"robot-{int(time.time())}"

        # 防止并发：每次只处理一条请求
        self._request_lock = threading.Lock()
        # /stop 信号标志
        self._stop_flag = threading.Event()
        # 记录已处理的消息 ID，避免重复发送（限制大小防止内存泄漏）
        self._processed_message_ids = set()
        self._max_processed_ids = 1000  # 最多保留 1000 条记录
        self._response_complete = False  # 响应是否完成的标志

        # SSE 相关
        self._sse_thread = None
        self._sse_stop_event = threading.Event()
        self._reply_queue = queue.Queue()  # 接收 SSE 消息的队列
        self._current_event_id = None  # 当前正在处理的 event_id
        self._current_device_id = None  # 当前正在处理的 device_id
        self._last_message_time = 0.0  # 最后一条消息的时间戳（用于超时检测）
        self._message_idle_timeout = 15.0  # 消息空闲超时时间（秒），适应长间隔消息

        # 启动 SSE 连接
        if self.use_sse:
            self._start_sse_connection()

        self.logger.info(
            f"OpenClawBridge 初始化完成: url={self.openclaw_url} "
            f"channel={self.openclaw_channel} user={self._user_id} use_sse={self.use_sse}"
        )

    # ──────────────────────────────────────────────────────────────────
    # SSE 连接管理
    # ──────────────────────────────────────────────────────────────────

    def _start_sse_connection(self):
        """启动 SSE 连接线程"""
        self._sse_stop_event.clear()
        self._sse_thread = threading.Thread(
            target=self._sse_listener,
            daemon=True
        )
        self._sse_thread.start()
        self.logger.info("SSE 连接线程已启动")

    def _stop_sse_connection(self):
        """停止 SSE 连接"""
        if self._sse_thread:
            self._sse_stop_event.set()
            self._sse_thread.join(timeout=5)
            self.logger.info("SSE 连接已停止")

    def _sse_listener(self):
        """SSE 监听线程，持续接收服务器推送的消息"""
        while not self._sse_stop_event.is_set():
            try:
                url = f"{self.openclaw_url}/sse?chatId={self.openclaw_channel}"
                self.logger.info(f"连接 SSE: {url}")

                req = urllib.request.Request(url)
                req.add_header('Accept', 'text/event-stream')
                req.add_header('Cache-Control', 'no-cache')

                with urllib.request.urlopen(req, timeout=None) as response:
                    self.logger.info("SSE 连接成功，开始接收消息")

                    # 逐行读取 SSE 流
                    for line in response:
                        if self._sse_stop_event.is_set():
                            break

                        line = line.decode('utf-8').strip()
                        self.logger.debug(f"SSE 原始行: {line[:100]}")  # 添加调试日志
                        if not line:
                            continue

                        # SSE 格式: data: {...}
                        if line.startswith('data: '):
                            data_str = line[6:]  # 去掉 "data: " 前缀
                            self.logger.info(f"SSE 接收到数据: {data_str[:200]}")  # 添加日志
                            try:
                                data = json.loads(data_str)
                                self._handle_sse_message(data)
                            except json.JSONDecodeError as e:
                                self.logger.warning(f"SSE 消息 JSON 解析失败: {e}, data={data_str[:100]}")

            except Exception as e:
                if not self._sse_stop_event.is_set():
                    self.logger.error(f"SSE 连接异常: {e}")
                    self.logger.info("5 秒后重连...")
                    time.sleep(5)

    def _handle_sse_message(self, data: dict):
        """处理 SSE 接收到的消息"""
        msg_id = data.get("id", "")
        text = data.get("text", "")
        chat_id = data.get("chatId", "")
        is_final = data.get("isFinal", False)  # 是否是最后一条消息

        self.logger.debug(f"SSE 原始消息: id={msg_id}, chatId={chat_id}, text_len={len(text)}, isFinal={is_final}")

        if chat_id != self.openclaw_channel:
            self.logger.debug(f"跳过非当前频道消息: {chat_id} != {self.openclaw_channel}")
            return  # 不是当前频道的消息

        # 检查是否是完成信号（空文本 + isFinal=true）
        if not text and is_final:
            self.logger.info(f"SSE 收到响应完成信号: id={msg_id}")
            if self._current_event_id and self._current_device_id:
                self.logger.info(f"[{self._current_event_id}] 收到响应完成信号（isFinal=true）")
                # 发送一个空的最终消息，带有 stopReason: "stop"
                self._send_response(self._current_device_id, self._current_event_id, "", is_final=True)
                self._response_complete = True
            return  # 完成信号处理完毕

        if not text:
            self.logger.debug(f"跳过空文本消息: id={msg_id}")
            return  # 跳过空文本消息（如连接成功消息）

        if msg_id in self._processed_message_ids:
            self.logger.warning(f"跳过重复消息: id={msg_id}, text={text[:50]}...")
            return  # 已处理过

        # 添加到已处理集合，并限制大小防止内存泄漏
        self._processed_message_ids.add(msg_id)
        if len(self._processed_message_ids) > self._max_processed_ids:
            # 移除最旧的一半记录（简单策略：清空后重新添加当前消息）
            self.logger.info(f"已处理消息记录达到上限 {self._max_processed_ids}，清理旧记录")
            old_ids = list(self._processed_message_ids)
            self._processed_message_ids.clear()
            # 保留最近的一半
            for old_id in old_ids[-self._max_processed_ids // 2:]:
                self._processed_message_ids.add(old_id)
            self._processed_message_ids.add(msg_id)
        self.logger.info(f"SSE 收到新消息: id={msg_id}, text={text[:50]}..., isFinal={is_final}")

        # 如果有当前正在处理的请求，立即上报
        if self._current_event_id and self._current_device_id:
            self.logger.info(f"[{self._current_event_id}] SSE 消息立即上报: {text[:50]}...")
            # 中间消息不设置 is_final，只有收到完成信号后才会发送 is_final=True 的消息
            self._send_response(self._current_device_id, self._current_event_id, text, is_final=False)

            # 如果是最后一条消息，设置完成标志（但这里不应该发生，因为最后一条应该是空文本）
            if is_final:
                self.logger.warning(f"[{self._current_event_id}] 收到带内容的最后一条消息（isFinal=true），这不应该发生")
                self._response_complete = True
            # 已经立即上报，不需要放入队列
        else:
            self.logger.warning(f"无当前请求上下文，消息放入队列: id={msg_id}")
            # 否则放入队列等待（兼容旧逻辑）
            self._reply_queue.put({
                "id": msg_id,
                "text": text,
                "timestamp": data.get("timestamp", int(time.time() * 1000)),
                "isFinal": is_final
            })

    # ──────────────────────────────────────────────────────────────────
    # 接收侧：覆写 on_speech_event
    # ──────────────────────────────────────────────────────────────────

    def on_speech_event(self, event_json: str):
        try:
            data = json.loads(event_json)
        except json.JSONDecodeError:
            self.logger.warning(f"JSON 解析失败: {event_json[:100]}")
            return

        if data.get("domain") != "OPEN_CLAW":
            return
        if data.get("event") != "app_msg_request":
            return

        device_id = data.get("deviceId", "")
        event_id = data.get("eventId", str(int(time.time() * 1000)))
        body = data.get("body", {})
        text = body.get("text", "").strip()
        signal = body.get("signal", "").strip()

        self.logger.info(f"收到 app_msg_request:\n{json.dumps(data, indent=2, ensure_ascii=False)}")

        # 处理控制信号
        if signal == "/clear":
            self._user_id = f"robot-{int(time.time())}"
            self._processed_message_ids.clear()  # 清除已处理消息记录
            # 清空队列中的旧消息
            while not self._reply_queue.empty():
                try:
                    self._reply_queue.get_nowait()
                except queue.Empty:
                    break
            self.logger.info(f"上下文已清除，新 user_id={self._user_id}")
            return
        if signal == "/stop":
            self._stop_flag.set()
            self.logger.info("收到 /stop 信号")
            return

        if not text:
            self.logger.warning("app_msg_request body.text 为空，忽略")
            return

        # 异步处理，不阻塞 ROS2 executor
        t = threading.Thread(
            target=self._handle_request,
            args=(device_id, event_id, text),
            daemon=True
        )
        t.start()

    # ──────────────────────────────────────────────────────────────────
    # 请求处理线程
    # ──────────────────────────────────────────────────────────────────

    def _handle_request(self, device_id: str, event_id: str, text: str):
        with self._request_lock:
            self._stop_flag.clear()
            self._response_complete = False  # 重置完成标志
            self._current_event_id = event_id
            self._current_device_id = device_id
            self.logger.info(f"[{event_id}] 发送到 OpenClaw: {text[:80]}")

            try:
                # 1. POST 到 OpenClaw Webhook（异步，服务端立即返回 202）
                reply = self._post_to_openclaw(text)
                if reply is None:
                    self.logger.error(f"[{event_id}] OpenClaw 请求失败")
                    return

                # 2. 根据配置选择接收方式
                if self.use_sse:
                    # 使用 SSE 实时接收（消息已在 _handle_sse_message 中立即上报）
                    self._wait_for_sse_replies(event_id, device_id)
                else:
                    # 使用轮询方式（兼容旧版）
                    ai_reply = self._poll_reply(event_id)
                    if ai_reply:
                        self._send_response(device_id, event_id, ai_reply)
            finally:
                # 清除当前请求上下文，避免后续无关消息被错误上报
                self._current_event_id = None
                self._current_device_id = None
                self.logger.info(f"[{event_id}] 请求处理完成")

    def _post_to_openclaw(self, text: str) -> Optional[str]:
        """向 OpenClaw Webhook 发送文本消息，返回 AI 回复文本（同步等待）"""
        ts = int(time.time())
        body = json.dumps({
            "event": "message",
            "timestamp": ts,
            "data": {
                "user": self._user_id,
                "message": text,
                "channel": self.openclaw_channel
            }
        }, ensure_ascii=False).encode("utf-8")

        sig = hmac.new(
            self.openclaw_secret.encode("utf-8"),
            body,
            hashlib.sha256
        ).hexdigest()

        try:
            req = urllib.request.Request(
                f"{self.openclaw_url}/webhook",
                data=body,
                headers={
                    "Content-Type": "application/json",
                    "x-xiaoli-signature": sig
                },
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=self.openclaw_post_timeout) as resp:
                resp_body = resp.read().decode('utf-8')
                self.logger.info(f"OpenClaw POST 响应: {resp.status} body={resp_body[:200]}")
                # 服务端同步返回: {"status":"ok"} 或直接返回文本
                # 回复需要从 /replies 接口获取
                return resp_body
        except Exception as e:
            self.logger.error(f"POST 到 OpenClaw 失败: {e}")
            return None

    def _wait_for_sse_replies(self, event_id: str, device_id: str):
        """等待 SSE 推送的回复消息（消息已在 _handle_sse_message 中立即上报）"""
        deadline = time.time() + self.openclaw_reply_timeout

        self.logger.info(f"[{event_id}] 等待 SSE 推送回复完成，超时时间: {self.openclaw_reply_timeout}秒")

        # 消息已经在 _handle_sse_message 中立即上报了
        # 这里只需要等待响应完成信号（isFinal=true）
        while time.time() < deadline:
            if self._stop_flag.is_set():
                self.logger.info(f"[{event_id}] 被 /stop 信号中断")
                return

            # 检查是否有遗留消息（不应该发生，但为了兼容性处理）
            if not self._reply_queue.empty():
                try:
                    reply = self._reply_queue.get_nowait()
                    self.logger.warning(f"[{event_id}] 发现遗留消息，立即上报: {reply['text'][:50]}...")
                    self._send_response(device_id, event_id, reply['text'])
                    # 检查是否是最后一条
                    if reply.get('isFinal', False):
                        self.logger.info(f"[{event_id}] SSE 回复完成（遗留消息 isFinal=true）")
                        return
                except queue.Empty:
                    pass

            # 检查是否收到响应完成信号（isFinal=true）
            if self._response_complete:
                self.logger.info(f"[{event_id}] SSE 回复完成（收到 isFinal=true）")
                return

            time.sleep(0.1)  # 短暂休眠，避免 CPU 占用过高

        self.logger.warning(f"[{event_id}] SSE 等待超时（{self.openclaw_reply_timeout}秒内未收到完成信号）")

    def _poll_reply(self, event_id: str) -> Optional[str]:
        """轮询 OpenClaw 获取所有新回复（兼容模式）"""
        # 增加 limit 以获取多条消息
        url = f"{self.openclaw_url}/replies?chatId={self.openclaw_channel}&limit=10"
        deadline = time.time() + self.openclaw_reply_timeout
        all_replies = []

        while time.time() < deadline:
            if self._stop_flag.is_set():
                self.logger.info(f"[{event_id}] 被 /stop 信号中断")
                return None

            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    replies = data.get("replies") or []

                    if replies:
                        self.logger.info(f"[{event_id}] /replies 响应: 获取到 {len(replies)} 条消息")

                        # 过滤出新消息（未处理过的）
                        new_replies = []
                        for reply in replies:
                            msg_id = reply.get("id", "")
                            reply_text = reply.get("text", "")

                            if msg_id and msg_id not in self._processed_message_ids and reply_text:
                                new_replies.append(reply)
                                self._processed_message_ids.add(msg_id)

                        if new_replies:
                            # 按时间戳排序（旧的在前）
                            new_replies.sort(key=lambda x: x.get("timestamp", 0))

                            # 发送所有新消息
                            for reply in new_replies:
                                reply_text = reply.get("text", "")
                                self.logger.info(f"[{event_id}] 处理新回复: id={reply.get('id')}, text={reply_text[:50]}...")
                                all_replies.append(reply_text)

                            # 返回所有回复的组合（用换行分隔）
                            return "\n\n".join(all_replies)

            except Exception as e:
                self.logger.debug(f"轮询 OpenClaw 异常: {e}")

            time.sleep(1.0)

        return None

    def _send_response(self, device_id: str, event_id: str, reply_text: str, is_final: bool = False):
        """将 OpenClaw 回复封装为 device_msg_response 发回 SpeechCore

        Args:
            device_id: 设备 ID
            event_id: 事件 ID
            reply_text: 回复文本
            is_final: 是否是最后一条消息（只有最后一条才设置 stopReason: "stop"）
        """
        ts = int(time.time() * 1000)
        payload = {
            "deviceId": device_id,
            "domain": "OPEN_CLAW",
            "event": "device_msg_response",
            "eventId": event_id,
            "seq": str(ts),
            "response": False,
            "body": {
                "id": str(ts),
                "parentId": event_id,
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": reply_text
                    }
                ],
                "timestamp": ts
            }
        }

        # 只有最后一条消息才设置 stopReason: "stop"
        if is_final:
            payload["body"]["stopReason"] = "stop"

        data = json.dumps(payload, ensure_ascii=False)
        self.logger.info(f"[{event_id}] 发送 device_msg_response (is_final={is_final}):\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

        # 从线程调用：使用 call_async + threading.Event 避免与主 executor 冲突
        ret = self._send_from_thread(data)
        if ret == 0:
            self.logger.info(f"[{event_id}] device_msg_response 发送成功")
        else:
            self.logger.error(f"[{event_id}] device_msg_response 发送失败 error_code={ret}")

    def _send_from_thread(self, data: str) -> int:
        """
        线程安全的发送：使用 call_async + done_callback + threading.Event，
        不调用 spin_until_future_complete（主线程已在 spin）。
        """
        if not self.platform_client.service_is_ready():
            self.logger.warning("sigc_data_service 未就绪")
            return -1

        req = SIGCData.Request()
        req.data = data

        result_event = threading.Event()
        result_holder = [None]

        def _done(future):
            try:
                result_holder[0] = future.result()
            except Exception as e:
                self.logger.error(f"_send_from_thread callback 异常: {e}")
            finally:
                result_event.set()

        try:
            future = self.platform_client.call_async(req)
            future.add_done_callback(_done)

            if result_event.wait(timeout=float(self.send_timeout_sec)):
                result = result_holder[0]
                if result is not None:
                    return result.error_code
                return -3
            else:
                self.logger.error("_send_from_thread 超时")
                return -2
        except Exception as e:
            self.logger.error(f"_send_from_thread 异常: {e}")
            return -3


def main(args=None):
    rclpy.init(args=args)
    node = OpenClawBridge()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("检测到 KeyboardInterrupt，正在关闭...")
    finally:
        try:
            # 停止 SSE 连接
            if node.use_sse:
                node._stop_sse_connection()
            node.destroy_node()
        except Exception as e:
            print(f"清理节点资源时发生错误: {e}")
        finally:
            rclpy.shutdown()


if __name__ == "__main__":
    main()
