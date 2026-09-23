"""
Hermes Bridge 节点 - XiaoliChannel HTTP 服务器

将 SpeechCore 与 Hermes Gateway 连接的桥接节点。
作为 XiaoliChannel 兼容的 HTTP 服务器，提供 API 供 Hermes Gateway 连接。

通信流程:
    Hermes Gateway --[HTTP API]--> hermes_bridge (HTTP Server)
      --> 长轮询 /getupdates 或 SSE /stream 接收来自 SpeechCore 的消息
      --> POST /sendmessage 发送消息到 SpeechCore

    SpeechCore --[sigc_event_topic]--> on_speech_event
      --> OPEN_CLAW / app_msg_request
          --> 加入 outgoing_updates 队列
          --> 通过长轮询/SSE 推送给 Hermes Gateway
      --> DEVICE_ABILITY / item_search
          --> 发布 /findobj_ctl
              {"SN","prompt","scene","status":"00"}
          --> 订阅 /findobj/search_status
              --> 回传平台 item_search 响应
      --> DEVICE_ABILITY / dog_auto_delivery_demo
          --> 透传到 /dog_mission/platform_event
              完整事件 JSON（std_msgs/String）

    本体 --[/findobj_ctl]--> hermes_bridge --[订阅监控]--> 日志记录
      寻物控制指令（功能开关）
          {"SN","prompt","scene","status"}
          status: 00=开启, 01=结束, 02=暂停, 03=继续
"""
import json
import time
import threading
import asyncio
import queue
import os
import random
import socket
import uuid
import psutil
from typing import Optional, Dict, Any, Set, List
from datetime import datetime

import rclpy
from std_msgs.msg import String

try:
    from aiohttp import web
except ImportError:
    print("需要安装 aiohttp: pip install aiohttp")
    raise

try:
    from .claw_client_node import SpeechCoreClientNode
    from .logging_setup import setup_logging
    from .runtime_link import RuntimeAgentLink
    from .hermes_utils import (
        format_message_for_xiaolichannel,
        strip_mention,
        safe_id,
        compute_content_fingerprint,
    )
except ImportError:
    from claw_client.claw_client_node import SpeechCoreClientNode
    from claw_client.logging_setup import setup_logging
    from claw_client.runtime_link import RuntimeAgentLink
    from claw_client.hermes_utils import (
        format_message_for_xiaolichannel,
        strip_mention,
        safe_id,
        compute_content_fingerprint,
    )

from homi_speech_interface.msg import AssistantEvent
from homi_speech_interface.srv import AssistantSpeechText


class HermesBridge(SpeechCoreClientNode):
    """
    Hermes Gateway 桥接节点 - XiaoliChannel HTTP 服务器模式

    作为 HTTP 服务器，提供 XiaoliChannel 兼容的 API：
      - GET /getupdates: 长轮询接收 SpeechCore 消息
      - GET /stream: SSE 流式推送 SpeechCore 消息
      - POST /sendmessage: 接收 Hermes Gateway 消息并转发到 SpeechCore
      - POST /editmessage: 编辑消息（流式输出）
    """

    def __init__(self):
        super().__init__(node_name='hermes_bridge')

        # HTTP 服务器配置
        self.declare_parameter('server_host', '0.0.0.0')
        self.declare_parameter('server_port', 8800)
        self.declare_parameter('app_id', 'hermes-app-id')
        self.declare_parameter('app_secret', 'hermes-app-secret')

        # 消息处理配置
        self.declare_parameter('enable_streaming', True)
        self.declare_parameter('message_timeout_sec', 60)
        # 是否将最终 ASR 解析结果推送给 XiaoliChannel / Hermes Gateway
        self.declare_parameter('forward_asr_to_channel', True)
        # 寻物：item_search → /findobj_ctl；结果 ← /findobj/search_status
        self.declare_parameter('findobj_ctl_topic', '/findobj_ctl')
        self.declare_parameter('findobj_status_topic', '/findobj/search_status')
        # TTS：/audio_center/play_tts
        self.declare_parameter('tts_service', '/audio_center/play_tts')
        self.declare_parameter('tts_timeout_sec', 5.0)

        # 访问控制配置
        self.declare_parameter('dm_policy', 'open')
        self.declare_parameter('group_policy', 'open')
        self.declare_parameter('require_mention', False)
        self.declare_parameter('allow_from', [])
        self.declare_parameter('group_allow_from', [])
        self.declare_parameter('mention_patterns', [])

        # 消息去重配置
        self.declare_parameter('max_processed_ids', 1000)
        self.declare_parameter('max_fingerprints', 500)
        self.declare_parameter('fingerprint_ttl_sec', 300)

        # 游戏功能配置（cloud show UDP，等价于 push_cmd.sh）
        self.declare_parameter('game_udp_host', '127.0.0.1')
        self.declare_parameter('game_udp_port', 24021)
        self.declare_parameter('touch_status_topic', '/touch_status')
        self.declare_parameter('game_direct_send', False)
        default_runtime_socket = os.path.abspath(os.path.join(
            os.path.dirname(os.path.realpath(__file__)), '..', '..', '..',
            'smartapp-runtime', 'runtime-data', 'smartapp-runtime', 'run', 'runtime.sock'
        ))
        self.declare_parameter('smartapp_runtime_socket', default_runtime_socket)

        # 异步请求 OpenClaw 服务鉴权信息（真正非阻塞）
        # 基类 send_async + request_service_credential_async(enable_retry=True)：
        # 发送失败 / 响应超时 / 空凭证 自动后台重试，不阻塞 __init__ / executor
        self.logger.info("发送 OpenClaw 服务鉴权请求（异步，失败自动重试）...")
        success = self.request_service_credential_async("openclaw", callback=None)
        self.logger.info(f"鉴权请求返回状态: success={success}")

        # 读取参数（使用配置文件中的值，不会被平台鉴权信息覆盖）
        self.server_host = self.get_parameter('server_host').value
        self.server_port = self.get_parameter('server_port').value
        self.app_id = self.get_parameter('app_id').value
        self.app_secret = self.get_parameter('app_secret').value
        self.enable_streaming = self.get_parameter('enable_streaming').value
        self.message_timeout = self.get_parameter('message_timeout_sec').value
        self.forward_asr_to_channel = self.get_parameter('forward_asr_to_channel').value
        self.findobj_ctl_topic = self.get_parameter('findobj_ctl_topic').value
        self.findobj_status_topic = self.get_parameter('findobj_status_topic').value
        self._tts_service_name = self.get_parameter('tts_service').value
        self._tts_timeout_sec = float(self.get_parameter('tts_timeout_sec').value)

        # 游戏功能配置
        self.game_udp_host = self.get_parameter('game_udp_host').value
        self.game_udp_port = int(self.get_parameter('game_udp_port').value)
        self.touch_status_topic = self.get_parameter('touch_status_topic').value
        self.game_direct_send = self.get_parameter('game_direct_send').value
        self.smartapp_runtime_socket = self.get_parameter('smartapp_runtime_socket').value

        # 访问控制参数
        self.dm_policy = self.get_parameter('dm_policy').value
        self.group_policy = self.get_parameter('group_policy').value
        self.require_mention = self.get_parameter('require_mention').value
        self.allow_from = set(self.get_parameter('allow_from').value or [])
        self.group_allow_from = set(self.get_parameter('group_allow_from').value or [])
        self.mention_patterns = self.get_parameter('mention_patterns').value or []

        # 消息去重
        self._processed_message_ids: Set[str] = set()
        self._content_fingerprints: Dict[str, float] = {}
        self._max_processed_ids = self.get_parameter('max_processed_ids').value
        self._max_fingerprints = self.get_parameter('max_fingerprints').value
        self._fingerprint_ttl = self.get_parameter('fingerprint_ttl_sec').value
        
        # event_id 去重（防止 ROS topic 重复发布触发 interrupt storm）
        self._seen_event_ids: Set[str] = set()
        self._max_seen_event_ids = 500  # 循环队列大小

        # 消息队列（ROS2 线程和 HTTP 服务器线程之间通信）
        # SpeechCore -> Hermes Gateway 的 updates 需要按 offset 非破坏性读取；不能用 Queue 消费后丢弃。
        self._outgoing_updates: List[Dict[str, Any]] = []
        self._outgoing_updates_lock = threading.Lock()
        self._max_outgoing_updates = 20
        self._incoming_messages = queue.Queue()  # Hermes Gateway -> SpeechCore

        # HTTP 服务器状态
        # 使用时间戳作为初始 update_id，避免重启后与 Gateway offset 冲突
        self._update_id = int(time.time())
        self._update_id_lock = threading.Lock()
        self._long_poll_waiters: List[asyncio.Future] = []
        self._sse_queues: Dict[str, asyncio.Queue] = {}
        self._context_tokens: Dict[str, str] = {}  # {chat_id: context_token}
        self._event_id_map: Dict[str, str] = {}  # {chat_id: event_id} 保持请求响应的 event_id 一致
        self._responded_event_ids: Set[str] = set()  # 已响应的 event_id，防止重复响应
        self._pending_responses: Dict[str, Dict[str, Any]] = {}  # {chat_id: pending response timeout info}
        self._pending_responses_lock = threading.Lock()
        self._processed_asr_sections: Set[str] = set()  # 已处理的最终 ASR section_id，防止重复触发

        # 寻物指令下发：平台 item_search → /findobj_ctl
        # 寻物状态回传：/findobj/search_status → 平台 item_search 响应
        self._findobj_ctl_pub = self.create_publisher(
            String, self.findobj_ctl_topic, 10
        )
        self._findobj_status_sub = self.create_subscription(
            String,
            self.findobj_status_topic,
            self._on_findobj_search_status,
            10,
        )
        # 寻物控制：本体 → 算法（功能开关）
        # 同一个话题，hermes_bridge 既发布（平台下发）也订阅（监控日志）
        self._findobj_ctl_sub = self.create_subscription(
            String,
            self.findobj_ctl_topic,
            self._on_findobj_ctl,
            10,
        )
        # SN -> 待回传的 item_search 上下文
        self._pending_item_searches: Dict[str, Dict[str, Any]] = {}
        self._pending_item_searches_lock = threading.Lock()
        self.logger.info(
            f"寻物话题: ctl={self.findobj_ctl_topic} status={self.findobj_status_topic}"
        )

        # 配送演示透传：dog_auto_delivery_demo → /dog_mission/platform_event
        self._dog_mission_pub = self.create_publisher(
            String, "/dog_mission/platform_event", 10
        )
        self.logger.info("配送演示话题: /dog_mission/platform_event")

        # 游戏会话跟踪：新版 SmartApp Runtime 会话或旧版 cloud show 会话
        self._active_game_sessions: Dict[str, Dict[str, Any]] = {}
        self._game_sessions_lock = threading.Lock()
        self._runtime_upstream_queue = queue.Queue(maxsize=128)
        self._runtime_upstream_seq: Dict[str, int] = {}

        # 游戏数据匹配队列：等待 ASR 确认后再下发 UDP
        # matchedText -> {data, timestamp, session_id, seq, event_id}
        self._pending_game_data: Dict[str, Dict[str, Any]] = {}
        self._pending_game_data_lock = threading.Lock()
        self._pending_game_data_ttl = 30.0  # 30秒超时

        self._head_touch_pins = {"36", "38", "39"}
        self._touch_status_sub = self.create_subscription(
            String,
            self.touch_status_topic,
            self._on_touch_status,
            10,
        )
        self.logger.info(
            f"触摸停游戏: topic={self.touch_status_topic} pins={sorted(self._head_touch_pins)}"
        )

        # TTS 客户端：/audio_center/play_tts
        self._tts_client = self.create_client(
            AssistantSpeechText, self._tts_service_name
        )
        self.logger.info(f"创建 TTS 客户端: {self._tts_service_name}")

        # 线程控制
        self._stop_event = threading.Event()
        self._server_thread = None
        self._server_loop = None
        self._app = None
        self._runner = None

        # 启动 HTTP 服务器
        self._start_http_server()

        # 启动消息处理线程
        self._message_processor_thread = threading.Thread(
            target=self._process_incoming_messages,
            daemon=True
        )
        self._message_processor_thread.start()

        # 订阅语音助手状态话题
        self._assistant_status_sub = self.create_subscription(
            AssistantEvent,
            '/homi_speech/speech_assistant_status_topic',
            self._on_assistant_status,
            10
        )
        self.logger.info("订阅话题: /homi_speech/speech_assistant_status_topic")

        self._runtime_link = RuntimeAgentLink(
            self.smartapp_runtime_socket, self._on_runtime_event, self.logger
        )
        self._runtime_upstream_thread = threading.Thread(
            target=self._forward_runtime_events,
            name="smartapp-runtime-platform-forwarder",
            daemon=True,
        )
        self._runtime_upstream_thread.start()
        self._runtime_link.start()

        self.logger.info(
            f"HermesBridge 初始化完成: "
            f"server=http://{self.server_host}:{self.server_port} "
            f"app_id={self.app_id} "
            f"forward_asr_to_channel={self.forward_asr_to_channel} "
            f"game_direct_send={self.game_direct_send} "
            f"dm_policy={self.dm_policy} group_policy={self.group_policy}"
        )

    # ──────────────────────────────────────────────────────────────────
    # HTTP 服务器管理
    # ──────────────────────────────────────────────────────────────────

    def _kill_process_using_port(self, port: int):
        """检测并关闭占用指定端口的进程"""
        try:
            current_pid = os.getpid()
            for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
                try:
                    # 检查进程是否使用了指定端口
                    for conn in proc.connections(kind='inet'):
                        if conn.laddr.port == port and conn.status == 'LISTEN':
                            # 跳过当前进程
                            if proc.pid == current_pid:
                                continue

                            # 检查是否是 hermes_bridge 进程
                            cmdline = ' '.join(proc.cmdline())
                            if 'hermes_bridge' in cmdline:
                                self.logger.warning(
                                    f"检测到端口 {port} 被旧的 hermes_bridge 进程占用 (PID: {proc.pid})，正在关闭..."
                                )
                                proc.terminate()
                                try:
                                    proc.wait(timeout=3)
                                    self.logger.info(f"已成功关闭旧进程 (PID: {proc.pid})")
                                except psutil.TimeoutExpired:
                                    self.logger.warning(f"进程 {proc.pid} 未响应 SIGTERM，使用 SIGKILL 强制关闭")
                                    proc.kill()
                                    proc.wait(timeout=1)
                                    self.logger.info(f"已强制关闭旧进程 (PID: {proc.pid})")
                            else:
                                self.logger.error(
                                    f"端口 {port} 被其他进程占用 (PID: {proc.pid}, CMD: {cmdline[:100]})"
                                )

                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

        except Exception as e:
            self.logger.warning(f"检查端口占用时出错: {e}")

    def _start_http_server(self):
        """在独立线程中启动 aiohttp 服务器"""
        # 先尝试关闭占用端口的旧进程
        self._kill_process_using_port(self.server_port)
        time.sleep(0.5)  # 等待端口释放

        self._server_thread = threading.Thread(
            target=self._run_http_server,
            daemon=True
        )
        self._server_thread.start()
        self.logger.info(f"HTTP 服务器线程已启动: {self.server_host}:{self.server_port}")

    def _run_http_server(self):
        """HTTP 服务器运行循环（在独立线程中）"""
        # 创建新的事件循环
        self._server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._server_loop)

        # 创建 aiohttp 应用
        self._app = web.Application()
        self._setup_routes()

        # 启动服务器
        try:
            self._server_loop.run_until_complete(self._start_server())
            self._server_loop.run_forever()
        except Exception as e:
            self.logger.error(f"HTTP 服务器异常: {e}")
        finally:
            self._server_loop.close()

    def _setup_routes(self):
        """设置 HTTP 路由"""
        self._app.router.add_get("/getupdates", self._handle_get_updates)
        self._app.router.add_get("/stream", self._handle_sse_stream)
        self._app.router.add_post("/sendmessage", self._handle_send_message)
        self._app.router.add_post("/editmessage", self._handle_edit_message)
        self._app.router.add_get("/getconfig", self._handle_get_config)

    async def _start_server(self):
        """启动 aiohttp 服务器"""
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.server_host, self.server_port)
        await site.start()
        self.logger.info(f"HTTP 服务器已启动: http://{self.server_host}:{self.server_port}")

    async def _stop_server(self):
        """停止 HTTP 服务器"""
        if self._runner:
            await self._runner.cleanup()
        self.logger.info("HTTP 服务器已停止")

    # ──────────────────────────────────────────────────────────────────
    # HTTP API 处理函数
    # ──────────────────────────────────────────────────────────────────

    async def _handle_get_config(self, request: web.Request) -> web.Response:
        """处理配置查询（健康检查）"""
        app_id = request.query.get("app_id", "")
        self.logger.debug(f"[GetConfig] app_id={app_id}")

        return web.json_response({
            "ret": 0,
            "config": {
                "app_id": app_id,
                "version": "1.0.0",
                # long_poll = GET /getupdates；sse = GET /stream 推送
                "features": ["text", "long_poll", "sse", "edit"],
                "preferred_connection_mode": "sse",
            }
        })

    async def _handle_get_updates(self, request: web.Request) -> web.Response:
        """处理长轮询请求"""
        app_id = request.query.get("app_id", "")
        offset = int(request.query.get("offset", "0"))
        timeout = int(request.query.get("timeout", "30"))

        self.logger.debug(f"[GetUpdates] app_id={app_id} offset={offset} timeout={timeout}")

        # 验证 app_id
        if app_id != self.app_id:
            return web.json_response({
                "ret": -1,
                "errcode": -1,
                "errmsg": "Invalid app_id"
            })

        # 检查是否有待推送的更新。按 offset 非破坏性读取，避免错误 offset 或探测请求吃掉消息。
        updates = self._get_updates_since(offset)

        if updates:
            self.logger.info(f"[GetUpdates] 返回 {len(updates)} 条更新")
            return web.json_response({
                "ret": 0,
                "updates": updates
            })

        # 没有更新，长轮询等待
        try:
            future = asyncio.Future()
            self._long_poll_waiters.append(future)

            await asyncio.wait_for(future, timeout=timeout)

            # 被唤醒，再次按 offset 非破坏性获取更新
            updates = self._get_updates_since(offset)

            return web.json_response({
                "ret": 0,
                "updates": updates
            })

        except asyncio.TimeoutError:
            # 超时，返回空更新
            return web.json_response({
                "ret": 0,
                "updates": []
            })
        finally:
            if future in self._long_poll_waiters:
                self._long_poll_waiters.remove(future)

    async def _write_sse_event(
        self,
        response: web.StreamResponse,
        event_type: str,
        payload: Any,
    ) -> None:
        """按 SSE 规范写入一个完整事件（event + data + 空行）。"""
        if isinstance(payload, (dict, list)):
            data_str = json.dumps(payload, ensure_ascii=False)
        else:
            data_str = str(payload)
        await response.write(f"event: {event_type}\n".encode("utf-8"))
        # data 行：多行 JSON 时按 SSE 规范逐行加 data: 前缀
        for line in data_str.split("\n"):
            await response.write(f"data: {line}\n".encode("utf-8"))
        await response.write(b"\n")
        await response.drain()

    async def _handle_sse_stream(self, request: web.Request) -> web.StreamResponse:
        """处理 SSE 流式连接。

        优化点相对旧实现：
        1. 连接建立后先回放 offset 之后的缓冲 updates（catch-up），避免断线漏消息
        2. 先注册 queue 再 catch-up，降低与实时推送的竞态丢消息概率
        3. 统一 SSE 写事件格式
        """
        app_id = request.query.get("app_id", "")
        try:
            offset = int(request.query.get("offset", "0"))
        except (TypeError, ValueError):
            offset = 0

        self.logger.info(f"[SSE] Client connected: app_id={app_id} offset={offset}")

        # 验证 app_id
        if app_id != self.app_id:
            return web.Response(status=401, text="Invalid app_id")

        # 创建 SSE 响应
        response = web.StreamResponse()
        response.headers['Content-Type'] = 'text/event-stream'
        response.headers['Cache-Control'] = 'no-cache'
        response.headers['Connection'] = 'keep-alive'
        response.headers['X-Accel-Buffering'] = 'no'
        response.enable_chunked_encoding()
        await response.prepare(request)

        # 先注册 queue，再做 catch-up，避免中间窗口丢实时消息
        sse_queue: asyncio.Queue = asyncio.Queue()
        queue_id = f"{app_id}_{id(sse_queue)}"
        self._sse_queues[queue_id] = sse_queue

        try:
            # 发送连接确认
            await self._write_sse_event(
                response, "connected", {"app_id": app_id, "offset": offset}
            )
            self.logger.info(f"[SSE] Connection established: queue_id={queue_id}")

            # Catch-up：只回放「最近一小段」缓冲，避免 Gateway 重启后
            # offset 从 1 重连时把历史 updates 全量灌回去，触发
            # multi-chat 排队 / 会话拼接 / 模型打满。
            #
            # _get_updates_since 是非破坏性读取；这里再做截断。
            backlog = self._get_updates_since(offset)
            if backlog:
                max_catchup = 3
                if len(backlog) > max_catchup:
                    self.logger.warning(
                        f"[SSE] Catch-up truncated: {len(backlog)} -> {max_catchup} "
                        f"for {queue_id} (client offset={offset}); "
                        f"drop old buffered updates to avoid reconnect storm"
                    )
                    backlog = backlog[-max_catchup:]
                else:
                    self.logger.info(
                        f"[SSE] Catch-up {len(backlog)} buffered update(s) "
                        f"for {queue_id} (offset>={offset})"
                    )
                for update in backlog:
                    await sse_queue.put(update)

            # 实时推送 + 心跳
            # 心跳 25s：适配器侧 watchdog 默认 60s，留足余量
            heartbeat_sec = 25.0
            while not self._stop_event.is_set():
                try:
                    update = await asyncio.wait_for(
                        sse_queue.get(), timeout=heartbeat_sec
                    )

                    update_id = int(update.get("update_id", 0) or 0)
                    if update_id < offset:
                        continue

                    self.logger.info(
                        f"[SSE] Sending update {update_id} to {queue_id}"
                    )
                    # 与 adapter SSE 解析约定：{"updates": [update]}
                    await self._write_sse_event(
                        response, "update", {"updates": [update]}
                    )

                except asyncio.TimeoutError:
                    # 心跳：event=ping，adapter 会识别并刷新 watchdog
                    self.logger.debug(f"[SSE] Sending heartbeat to {queue_id}")
                    await self._write_sse_event(response, "ping", {})

                except Exception as e:
                    self.logger.error(f"[SSE] Error in message loop: {e}")
                    break

        except (ConnectionResetError, asyncio.CancelledError) as e:
            self.logger.info(
                f"[SSE] Client disconnected: app_id={app_id} reason={type(e).__name__}"
            )
        except Exception as e:
            self.logger.error(f"[SSE] Unexpected error: {e}")
        finally:
            if queue_id in self._sse_queues:
                del self._sse_queues[queue_id]
            self.logger.info(f"[SSE] Connection closed: queue_id={queue_id}")

        return response

    async def _handle_send_message(self, request: web.Request) -> web.Response:
        """处理发送消息请求（从 Hermes Gateway 到 SpeechCore）"""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({
                "ret": -1,
                "errcode": -1,
                "errmsg": "Invalid JSON"
            })


        # 打印完整的请求数据用于调试
        self.logger.info(f"[DEBUG] SendMessage 完整数据: {json.dumps(data, ensure_ascii=False)}")

        app_id = data.get("app_id", "")
        chat_id = data.get("chat_id", "")
        user_id = data.get("user_id", "")
        text = data.get("content", "") or data.get("text", "")
        context_token = data.get("context_token", "")
        reply_to = data.get("reply_to", "")
        is_streaming_preview = "▉" in text
        is_final = bool(reply_to) and not is_streaming_preview

        # 从 chat_id 提取 device_id (格式: chat-{deviceId})
        device_id = chat_id.replace("chat-", "") if chat_id.startswith("chat-") else ""

        self.logger.info(
            f"[SendMessage] app_id={app_id} chat_id={chat_id} "
            f"user_id={user_id} reply_to={reply_to} text={safe_id(text, 50)}"
        )

        # 验证 app_id
        if app_id != self.app_id:
            return web.json_response({
                "ret": -1,
                "errcode": -1,
                "errmsg": "Invalid app_id"
            })

        # 保存 context_token
        if context_token:
            self._context_tokens[chat_id] = context_token

        # 生成消息 ID
        message_id = f"msg_{int(time.time() * 1000)}"

        # 加入处理队列。流式首包通常也带 reply_to，但内容仍包含光标，不能标记为最终消息。
        self._incoming_messages.put({
            "message_id": message_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": text,
            "is_final": is_final,
            "timestamp": int(time.time())
        })

        return web.json_response({
            "ret": 0,
            "message_id": message_id,
            "context_token": context_token,
            "timestamp": int(time.time())
        })

    async def _handle_edit_message(self, request: web.Request) -> web.Response:
        """处理编辑消息请求（流式输出）"""
        try:
            data = await request.json()
        except json.JSONDecodeError:
            return web.json_response({
                "ret": -1,
                "errcode": -1,
                "errmsg": "Invalid JSON"
            })

        app_id = data.get("app_id", "")
        chat_id = data.get("chat_id", "")
        message_id = data.get("message_id", "")
        content = data.get("content", "")
        context_token = data.get("context_token", "")
        finalize = bool(data.get("finalize", False))

        self.logger.info(
            f"[EditMessage] app_id={app_id} chat_id={chat_id} "
            f"message_id={message_id} finalize={finalize} content={safe_id(content, 50)}"
        )

        # 验证 app_id
        if app_id != self.app_id:
            return web.json_response({
                "ret": -1,
                "errcode": -1,
                "errmsg": "Invalid app_id"
            })

        if context_token:
            self._context_tokens[chat_id] = context_token

        if not message_id:
            message_id = f"msg_{int(time.time() * 1000)}"

        # editmessage 是流式更新的核心路径，需要把中间更新和最终 finalize 都转发给 SpeechCore。
        # edit API 会复用原 message_id；队列内部使用唯一 ID，避免被消息去重误判为重复更新。
        queue_message_id = f"{message_id}:edit:{int(time.time() * 1000)}"
        self._incoming_messages.put({
            "message_id": queue_message_id,
            "chat_id": chat_id,
            "user_id": data.get("user_id", ""),
            "text": content,
            "is_final": finalize,
            "timestamp": int(time.time())
        })

        return web.json_response({
            "ret": 0,
            "message_id": message_id
        })

    # ──────────────────────────────────────────────────────────────────
    # 消息处理：Hermes Gateway -> SpeechCore
    # ──────────────────────────────────────────────────────────────────

    def _process_incoming_messages(self):
        """处理来自 Hermes Gateway 的消息（独立线程）"""
        while not self._stop_event.is_set():
            try:
                # 阻塞获取消息（1秒超时）
                msg = self._incoming_messages.get(timeout=1.0)

                message_id = msg['message_id']
                chat_id = msg['chat_id']
                user_id = msg['user_id']
                text = msg['text']
                is_final = msg.get('is_final', False)  # 是否是最后一条消息

                # 消息去重
                if message_id in self._processed_message_ids:
                    self.logger.debug(f"跳过重复消息 ID: {message_id}")
                    continue

                self._processed_message_ids.add(message_id)
                if len(self._processed_message_ids) > self._max_processed_ids:
                    old_ids = list(self._processed_message_ids)
                    self._processed_message_ids.clear()
                    for old_id in old_ids[-self._max_processed_ids // 2:]:
                        self._processed_message_ids.add(old_id)

                # 内容指纹去重
                if self._is_duplicate_content(user_id, text):
                    self.logger.debug(f"跳过重复内容: user={safe_id(user_id)}")
                    continue

                # 移除 @提及
                text = strip_mention(text)

                # 提取 device_id (从 chat_id 中解析)
                device_id = self._extract_device_id(chat_id)

                self.logger.info(
                    f"处理 Gateway 消息: message_id={message_id} "
                    f"device_id={device_id} is_final={is_final} text={safe_id(text, 50)}"
                )

                # 转发到 SpeechCore（传递原始的 event_id 和 is_final 标记）
                self._forward_to_speechcore(device_id, chat_id, text, is_final)

            except queue.Empty:
                continue
            except Exception as e:
                self.logger.error(f"处理消息异常: {e}")

    def _register_response_timeout(self, device_id: str, chat_id: str, event_id: str):
        """登记 app_msg_request 的响应超时兜底。

        SpeechCore 依赖最终回复中的 stopReason=stop 结束本轮请求。
        如果 Hermes/Gateway 没有返回最终消息，则在 message_timeout_sec 后主动补一条最终响应，
        避免请求一直悬挂。
        """
        timeout_sec = float(self.message_timeout or 60)
        stale_pending = None

        with self._pending_responses_lock:
            existing = self._pending_responses.pop(chat_id, None)
            if existing:
                timer = existing.get("timer")
                if timer:
                    timer.cancel()
                if existing.get("event_id") != event_id:
                    stale_pending = existing

            timer = threading.Timer(
                timeout_sec,
                self._handle_response_timeout,
                args=(chat_id, event_id),
            )
            timer.daemon = True
            self._pending_responses[chat_id] = {
                "timer": timer,
                "device_id": device_id,
                "chat_id": chat_id,
                "event_id": event_id,
                "created_at": time.time(),
            }
            timer.start()

        if stale_pending:
            self.logger.info(
                "新的 app_msg_request 覆盖未完成请求，已静默清理旧请求定时器: old_event_id=%s new_event_id=%s",
                stale_pending.get("event_id"),
                event_id,
            )

        self.logger.info(
            "登记响应超时: chat_id=%s event_id=%s timeout=%.1fs",
            safe_id(chat_id),
            event_id,
            timeout_sec,
        )

    def _refresh_or_clear_response_timeout(self, chat_id: str, event_id: str, *, completed: bool = False):
        """刷新或清除响应超时兜底。

        非最终 device_msg_response 表示请求仍在持续响应，应重新计时；
        最终响应才清除计时器并标记完成。
        """
        timeout_sec = float(self.message_timeout or 60)

        with self._pending_responses_lock:
            pending = self._pending_responses.get(chat_id)
            if not pending or pending.get("event_id") != event_id:
                return

            timer = pending.get("timer")
            if timer:
                timer.cancel()

            if completed:
                self._pending_responses.pop(chat_id, None)
            else:
                timer = threading.Timer(
                    timeout_sec,
                    self._handle_response_timeout,
                    args=(chat_id, event_id),
                )
                timer.daemon = True
                pending["timer"] = timer
                pending["updated_at"] = time.time()
                timer.start()

        if completed:
            self._responded_event_ids.add(event_id)
            self.logger.info("响应已完成: chat_id=%s event_id=%s", safe_id(chat_id), event_id)
        else:
            self.logger.info(
                "已收到非最终响应，刷新超时计时: chat_id=%s event_id=%s timeout=%.1fs",
                safe_id(chat_id),
                event_id,
                timeout_sec,
            )

    def _handle_response_timeout(self, chat_id: str, event_id: str):
        """响应超时回调：补发 stopReason=stop。"""
        with self._pending_responses_lock:
            pending = self._pending_responses.get(chat_id)
            if not pending or pending.get("event_id") != event_id:
                return
            self._pending_responses.pop(chat_id, None)

        self.logger.warning(
            "等待 Hermes 最终响应超时，发送兜底 stop: chat_id=%s event_id=%s timeout=%ss",
            safe_id(chat_id),
            event_id,
            self.message_timeout,
        )
        self._send_timeout_response(pending, reason="timeout")

    def _send_timeout_response(self, pending: Dict[str, Any], reason: str):
        """发送请求超时最终响应。"""
        chat_id = pending.get("chat_id", "")
        event_id = pending.get("event_id", "")
        device_id = pending.get("device_id", "")
        if not chat_id or not event_id or not device_id:
            return

        previous_event_id = self._event_id_map.get(chat_id)
        self._event_id_map[chat_id] = event_id
        text = "请求处理超时，请稍后再试。"
        try:
            self._forward_to_speechcore(device_id, chat_id, text, is_final=True)
        finally:
            if previous_event_id and previous_event_id != event_id:
                self._event_id_map[chat_id] = previous_event_id

    def _forward_to_speechcore(self, device_id: str, chat_id: str, text: str, is_final: bool = False):
        """将 AI 回复发送回 SpeechCore（发送 device_msg_response）

        Args:
            device_id: 设备 ID
            chat_id: 聊天 ID
            text: 回复文本
            is_final: 是否是最后一条消息（只有最后一条才设置 stopReason: "stop"）
        """
        ts = int(time.time() * 1000)

        # 使用保存的 event_id，保持请求响应一致
        event_id = self._event_id_map.get(chat_id, str(ts))

        # 判断消息类型：terminal 命令标记为 thinking，普通消息标记为 text
        is_terminal = text.startswith("💻 terminal:") or text.startswith("🖥️ terminal:")
        content_type = "thinking" if is_terminal else "text"

        payload = {
            "deviceId": device_id,
            "domain": "OPEN_CLAW",
            "event": "device_msg_response",
            "eventId": event_id,  # 使用原始请求的 event_id
            "seq": str(ts),
            "response": False,
            "body": {
                "id": str(ts),
                "parentId": event_id,
                "role": "assistant",
                "content": [
                    {
                        "type": content_type,
                        "text": text
                    }
                ],
                "timestamp": ts
            }
        }

        # 只有最后一条消息才设置 stopReason: "stop"
        if is_final:
            payload["body"]["stopReason"] = "stop"

        data = json.dumps(payload, ensure_ascii=False)
        self.logger.info(
            f"发送 device_msg_response 到 SpeechCore: device_id={device_id} "
            f"event_id={event_id} is_final={is_final} text={safe_id(text, 50)}"
        )
        self.logger.info(f"[DEBUG] 完整 payload:\n{json.dumps(payload, indent=2, ensure_ascii=False)}")

        # 使用线程安全的发送方法，避免阻塞 ROS2 executor
        ret = self._send_from_thread(data)
        if ret == 0:
            self.logger.info("device_msg_response 发送成功")
            self._refresh_or_clear_response_timeout(chat_id, event_id, completed=is_final)
        else:
            self.logger.error(f"device_msg_response 发送失败 error_code={ret}")

    def _send_from_thread(self, data: str) -> int:
        """
        线程安全的发送：使用 call_async + done_callback + threading.Event，
        不调用 spin_until_future_complete（主线程已在 spin）。
        """
        if not self.platform_client.service_is_ready():
            self.logger.warning("sigc_data_service 未就绪")
            return -1

        from homi_speech_interface.srv import SIGCData
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

    # ──────────────────────────────────────────────────────────────────
    # 消息处理：SpeechCore -> Hermes Gateway
    # ──────────────────────────────────────────────────────────────────

    def _push_text_to_gateway(self, device_id: str, event_id: str, text: str, source: str = "platform"):
        """按 XiaoliChannel app_msg_request 等价格式推送文本给 Hermes Gateway。

        平台下发请求和语音助手最终 ASR 结果统一走这条路径，确保 Hermes
        看到相同的 chat_id/user_id/message 结构，后续响应也能用原 event_id
        回到 SpeechCore。
        """
        text = (text or "").strip()
        if not text:
            self.logger.warning("推送到 Gateway 的文本为空，忽略: source=%s event_id=%s", source, event_id)
            return

        chat_id = f"chat-{device_id}"
        self._event_id_map[chat_id] = event_id
        self._register_response_timeout(device_id, chat_id, event_id)

        formatted_text = format_message_for_xiaolichannel(text)

        with self._update_id_lock:
            self._update_id += 1
            update_id = self._update_id
        user_id = f"user-{device_id}"
        message_id = f"msg_{int(time.time() * 1000)}_{update_id}"

        update = {
            "update_id": update_id,
            "message": {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": "dm",
                "from": {
                    "user_id": user_id,
                    "nickname": device_id
                },
                "content": formatted_text,
                "timestamp": int(time.time()),
                "is_mentioned": False,
                "source": source,
            }
        }

        if chat_id in self._context_tokens:
            update["message"]["context_token"] = self._context_tokens[chat_id]

        with self._outgoing_updates_lock:
            self._outgoing_updates.append(update)
            if len(self._outgoing_updates) > self._max_outgoing_updates:
                self._outgoing_updates = self._outgoing_updates[-self._max_outgoing_updates:]

        self.logger.info(
            "推送 update %s 到 Gateway: message_id=%s source=%s event_id=%s",
            update_id,
            message_id,
            source,
            event_id,
        )
        self.logger.info(f"[DEBUG] Update 数据: {json.dumps(update, ensure_ascii=False)}")

        threading.Thread(
            target=self._notify_clients_thread_safe,
            args=(update,),
            daemon=True
        ).start()

    def on_speech_event(self, event_json: str):
        """处理来自 SpeechCore 的事件。

        当前支持：
        - OPEN_CLAW / app_msg_request → 推送给 Hermes Gateway
        - DEVICE_ABILITY / item_search → 发布 /findobj_ctl，状态由 /findobj/search_status 回传
        - DEVICE_ABILITY / robot_game_view → 游戏启动/停止控制
        - DEVICE_ABILITY / game_view_data → 游戏数据传输
        - DEVICE_ABILITY / dog_auto_delivery_demo → 自主配送演示任务
        """
        try:
            data = json.loads(event_json)
        except json.JSONDecodeError:
            self.logger.warning(f"JSON 解析失败: {event_json[:100]}")
            return

        domain = data.get("domain", "")
        event = data.get("event", "")

        if domain == "OPEN_CLAW" and event == "app_msg_request":
            self.logger.info(f"[DEBUG] 收到完整事件: {event_json}")
            self._handle_app_msg_request(data)
            return

        if domain == "DEVICE_ABILITY" and event == "item_search":
            self.logger.info(f"[DEBUG] 收到 item_search 事件: {event_json}")
            self._handle_item_search(data)
            return

        if domain == "DEVICE_ABILITY" and event == "robot_game_view":
            self.logger.info(f"[DEBUG] 收到 robot_game_view 事件: {event_json}")
            self._handle_robot_game_view(data)
            return

        if domain == "DEVICE_ABILITY" and event == "game_view_data":
            self.logger.info(f"[DEBUG] 收到 game_view_data 事件: {event_json}")
            self._handle_game_view_data(data)
            return

        if domain == "DEVICE_ABILITY" and event == "dog_auto_delivery_demo":
            self.logger.info(f"[DEBUG] 收到 dog_auto_delivery_demo 事件: {event_json}")
            self._handle_dog_auto_delivery_demo(data)
            return

    def _remember_event_id(self, event_id: str) -> bool:
        """event_id 去重。True=首次见到可继续处理，False=重复应跳过。"""
        if not event_id:
            return True
        if event_id in self._seen_event_ids:
            self.logger.debug(f"重复 event_id，跳过: {event_id}")
            return False
        self._seen_event_ids.add(event_id)
        if len(self._seen_event_ids) > self._max_seen_event_ids:
            to_remove = list(self._seen_event_ids)[: self._max_seen_event_ids // 2]
            for old_id in to_remove:
                self._seen_event_ids.discard(old_id)
        return True

    def _handle_app_msg_request(self, data: Dict[str, Any]) -> None:
        """处理 OPEN_CLAW / app_msg_request。"""
        device_id = data.get("deviceId", "")
        event_id = data.get("eventId", str(int(time.time() * 1000)))
        body = data.get("body", {}) or {}
        text = str(body.get("text", "") or "").strip()

        if not self._remember_event_id(event_id):
            return

        self.logger.info(
            f"收到 app_msg_request: device_id={device_id} event_id={event_id} text={safe_id(text, 50)}"
        )

        if not text:
            self.logger.warning("app_msg_request body.text 为空，忽略")
            return

        self._push_text_to_gateway(
            device_id=device_id,
            event_id=event_id,
            text=text,
            source="platform",
        )

    def _handle_item_search(self, data: Dict[str, Any]) -> None:
        """处理 DEVICE_ABILITY / item_search 寻物指令。

        平台下发示例：
        {
          "deviceId": "10012",
          "domain": "DEVICE_ABILITY",
          "event": "item_search",
          "eventId": "...",
          "body": {
            "itemName": "篮球",
            "itemCode": "basketball",
            "pointName": ""
          }
        }

        处理后发布到 /findobj/search_start (std_msgs/String)：
        {"SN":"<deviceId>","prompt":"<itemCode>","scene":"<pointName>"}
        """
        device_id = str(data.get("deviceId", "") or "")
        event_id = str(data.get("eventId", "") or str(int(time.time() * 1000)))
        body = data.get("body", {}) or {}
        if not isinstance(body, dict):
            self.logger.warning(
                f"item_search body 非法: device_id={device_id} event_id={event_id} body={body!r}"
            )
            return

        if not self._remember_event_id(event_id):
            return

        item_name = str(body.get("itemName", "") or "").strip()
        item_code = str(body.get("itemCode", "") or "").strip()
        point_name = str(body.get("pointName", "") or "").strip()

        self.logger.info(
            "收到 item_search: device_id=%s event_id=%s itemName=%s itemCode=%s pointName=%s",
            device_id,
            event_id,
            item_name or "-",
            item_code or "-",
            point_name or "-",
        )

        if not item_code:
            self.logger.warning(
                f"item_search 缺少 itemCode，忽略: event_id={event_id}"
            )
            return

        # 异步执行，避免阻塞 ROS 回调
        threading.Thread(
            target=self._process_item_search,
            kwargs={
                "device_id": device_id,
                "event_id": event_id,
                "item_name": item_name,
                "item_code": item_code,
                "point_name": point_name,
                "raw": data,
            },
            name=f"item-search-{event_id[:16]}",
            daemon=True,
        ).start()

    def _process_item_search(
        self,
        device_id: str,
        event_id: str,
        item_name: str,
        item_code: str,
        point_name: str,
        raw: Dict[str, Any],
    ) -> None:
        """item_search → 发布 /findobj_ctl，并登记待回传上下文。

        字段映射：
          SN     ← deviceId
          prompt ← itemCode
          scene  ← pointName
          status ← "00" (开启寻物)

        消息格式（与手动 ros2 topic pub 一致）：
          data: '{"SN":"...","prompt":"basketball","scene":"","status":"00"}'
        """
        try:
            sn = device_id or getattr(self, "device_id", "") or ""
            prompt = item_code
            scene = point_name

            payload = {
                "SN": sn,
                "prompt": prompt,
                "status": "00",  # 开启寻物
            }
            if scene:
                payload["scene"] = scene

            data_str = json.dumps(payload, ensure_ascii=False)

            # 登记 pending，供 /findobj/search_status 关联回传
            if sn:
                with self._pending_item_searches_lock:
                    self._pending_item_searches[sn] = {
                        "device_id": sn,
                        "event_id": event_id,
                        "item_name": item_name,
                        "item_code": item_code,
                        "point_name": point_name,
                        "ts": time.time(),
                    }

            self.logger.info(
                "[item_search] 发布 %s: event_id=%s data=%s",
                self.findobj_ctl_topic,
                event_id,
                data_str,
            )

            msg = String()
            msg.data = data_str
            self._findobj_ctl_pub.publish(msg)

            self.logger.info(
                "[item_search] 已下发寻物启动: event_id=%s SN=%s prompt=%s scene=%s status=00 itemName=%s",
                event_id,
                sn or "-",
                prompt or "-",
                scene or "-",
                item_name or "-",
            )
        except Exception as e:
            self.logger.error(f"[item_search] 处理异常: event_id={event_id} err={e}")

    def _handle_dog_auto_delivery_demo(self, data: Dict[str, Any]) -> None:
        """处理 dog_auto_delivery_demo 事件（自主配送演示）。

        事件格式：
          {
            "deviceId": "dog_001",
            "domain": "DEVICE_ABILITY",
            "event": "dog_auto_delivery_demo",
            "eventId": "mission_20260819_001",
            "body": {
              "targetTo": "",
              "targetGoods": "mimi",
              "sourcePlace": {
                "name": "起点",
                "x": 0.0,
                "y": 0.0,
                "angle": 0.0
              },
              "targetPlace": {
                "name": "目标点",
                "x": 5.0,
                "y": 3.0,
                "angle": 90.0
              },
              "targetPlace_en": ""
            }
          }
        """
        device_id = str(data.get("deviceId", "") or "")
        event_id = str(data.get("eventId", "") or str(int(time.time() * 1000)))
        body = data.get("body", {}) or {}

        if not self._remember_event_id(event_id):
            return

        target_to = str(body.get("targetTo", "") or "").strip()
        target_goods = str(body.get("targetGoods", "") or "").strip()
        source_place = body.get("sourcePlace", {}) or {}
        target_place = body.get("targetPlace", {}) or {}
        target_place_en = str(body.get("targetPlace_en", "") or "").strip()

        # 提取坐标信息
        source_name = str(source_place.get("name", "") or "").strip()
        source_x = source_place.get("x", 0.0)
        source_y = source_place.get("y", 0.0)
        source_angle = source_place.get("angle", 0.0)

        target_name = str(target_place.get("name", "") or "").strip()
        target_x = target_place.get("x", 0.0)
        target_y = target_place.get("y", 0.0)
        target_angle = target_place.get("angle", 0.0)

        self.logger.info("=" * 80)
        self.logger.info("🚚 [收到自主配送演示事件 dog_auto_delivery_demo]")
        self.logger.info(f"deviceId: {device_id}")
        self.logger.info(f"eventId: {event_id}")
        self.logger.info(f"targetTo: {target_to or '(空)'}")
        self.logger.info(f"targetGoods: {target_goods}")
        self.logger.info(f"起点: {source_name} (x={source_x}, y={source_y}, angle={source_angle})")
        self.logger.info(f"目标点: {target_name} (x={target_x}, y={target_y}, angle={target_angle})")
        self.logger.info(f"targetPlace_en: {target_place_en or '(空)'}")
        self.logger.info(f"完整数据: {json.dumps(data, ensure_ascii=False)}")
        self.logger.info("=" * 80)

        # 透传完整事件 JSON 到 /dog_mission/platform_event
        try:
            msg = String()
            msg.data = json.dumps(data, ensure_ascii=False)
            self._dog_mission_pub.publish(msg)
            self.logger.info(
                f"[dog_auto_delivery_demo] 已透传到 /dog_mission/platform_event: goods={target_goods} from={source_name} to={target_name}"
            )
        except Exception as e:
            self.logger.error(f"[dog_auto_delivery_demo] 透传失败: {e}")

    def _handle_robot_game_view(self, data: Dict[str, Any]) -> None:
        """处理 robot_game_view 事件（游戏启动/停止）。

        事件格式：
          {
            "deviceId": "11200",
            "domain": "DEVICE_ABILITY",
            "event": "robot_game_view",
            "eventId": "唯一id",
            "seq": "时间戳",
            "response": false,
            "body": {
              "game": "start" | "stop",
              "gameUrl": "https://...",  # start 时必需
              "sessionId": "abc123",
              "data": {...}         # start 时可选
            }
          }
        """
        event_id = data.get("eventId", "")
        if not self._remember_event_id(event_id):
            return

        body = data.get("body", {})
        game_action = body.get("game", "")

        if game_action == "start":
            threading.Thread(
                target=self._process_robot_game_start,
                kwargs={
                    "event_id": event_id,
                    "body": body,
                    "raw": data,
                },
                name=f"game-start-{event_id[:16]}",
                daemon=True,
            ).start()
        elif game_action == "stop":
            threading.Thread(
                target=self._process_robot_game_stop,
                kwargs={
                    "event_id": event_id,
                    "body": body,
                    "raw": data,
                },
                name=f"game-stop-{event_id[:16]}",
                daemon=True,
            ).start()
        else:
            self.logger.warning(
                f"[robot_game_view] 未知的 game 动作: {game_action}, event_id={event_id}"
            )

    def _send_game_udp(self, payload: str, tag: str) -> bool:
        """向 cloud show 守护进程发 UDP，协议与 push_cmd.sh 一致。"""
        host = self.game_udp_host
        port = self.game_udp_port
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.sendto(payload.encode("utf-8"), (host, port))
            self.logger.info(f"[{tag}] UDP sent {host}:{port} payload={payload}")
            return True
        except Exception as exc:
            self.logger.error(f"[{tag}] UDP 发送失败 {host}:{port} err={exc}")
            return False
        finally:
            if sock is not None:
                sock.close()

    def _push_game_stop(self, tag: str) -> bool:
        return self._send_game_udp("EXIT", tag)

    def _send_runtime_command(self, command: Dict[str, Any], timeout: float = 20.0) -> bool:
        result = self._runtime_link.send(command, timeout)
        if result is None:
            self.logger.error(f"[smartapp_runtime] {command['command']} timed out or disconnected")
            return False
        if result.get("ok") is not True:
            self.logger.error(f"[smartapp_runtime] {command['command']} failed: {result.get('error')}")
            return False
        return True

    def _on_runtime_event(self, event: Dict[str, Any]) -> None:
        session_id = event.get("sessionId")
        data = event.get("data")
        if not isinstance(session_id, str) or not session_id or type(data) is not dict:
            self.logger.warning("[smartapp_runtime] invalid app_data event")
            return
        sequence = max(int(time.time() * 1000), self._runtime_upstream_seq.get(session_id, 0) + 1)
        self._runtime_upstream_seq[session_id] = sequence
        body = {"sessionId": session_id, "data": data}
        if isinstance(event.get("dataType"), str) and event["dataType"]:
            body["type"] = event["dataType"]
        payload = {
            "deviceId": self.device_id,
            "domain": "DEVICE_ABILITY",
            "event": "game_view_data",
            "eventId": "robot-data-" + uuid.uuid4().hex,
            "seq": sequence,
            "body": body,
        }
        while not self._stop_event.is_set():
            try:
                self._runtime_upstream_queue.put(payload, timeout=0.2)
                return
            except queue.Full:
                continue

    def _forward_runtime_events(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._runtime_upstream_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            encoded = json.dumps(payload, ensure_ascii=False)
            while not self._stop_event.is_set():
                completed = threading.Event()
                result = []

                def on_sent(error_code):
                    result.append(error_code)
                    completed.set()

                self.send_async(encoded, callback=on_sent)
                completed.wait(timeout=float(self.send_timeout_sec) + 1.0)
                if result and result[0] == 0:
                    break
                self.logger.warning(
                    f"[smartapp_runtime] game_view_data upload retry: eventId={payload['eventId']}"
                )
                self._stop_event.wait(2.0)
            self._runtime_upstream_queue.task_done()

    def _process_smartapp_start(self, event_id: str, body: Dict[str, Any]) -> None:
        session_id = body.get("sessionId")
        app_id = body.get("appid")
        digest = body.get("sha256") or body.get("md5")
        digest_field = "sha256" if body.get("sha256") else "md5"
        if (not all((session_id, app_id, body.get("version"), body.get("packageUrl"), digest))
                or type(body.get("packageSize")) is not int
                or type(body.get("data", {})) is not dict):
            self.logger.error(f"[robot_game_view:start] invalid SmartApp package fields: {event_id}")
            return
        command = {
            "requestId": uuid.uuid4().hex,
            "command": "start_app",
            "sessionId": session_id,
            "appId": app_id,
            "version": body["version"],
            "packageUrl": body["packageUrl"],
            "packageSize": body["packageSize"],
            digest_field: digest,
            "initData": body.get("data", {}),
        }
        if not self._send_runtime_command(command, timeout=180.0):
            return
        with self._game_sessions_lock:
            self._active_game_sessions.clear()
            self._active_game_sessions[session_id] = {
                "runtime": True, "app_id": app_id, "start_time": time.time()
            }
        self.logger.info(f"[robot_game_view:start] SmartApp started: app_id={app_id} session_id={session_id}")

    def _stop_smartapp(self, session_id: str, reason: str) -> bool:
        if not session_id:
            self.logger.warning("[robot_game_view:stop] sessionId is required")
            return False
        command = {
            "requestId": uuid.uuid4().hex,
            "command": "stop_app",
            "sessionId": session_id,
            "reason": reason,
        }
        if not self._send_runtime_command(command):
            return False
        with self._game_sessions_lock:
            self._active_game_sessions.pop(session_id, None)
        self.logger.info(f"[robot_game_view:stop] SmartApp stopped: session_id={session_id}")
        return True

    def _on_touch_status(self, msg: String) -> None:
        """头部触摸上升沿：若游戏进行中则停止。"""
        raw = (msg.data or "").strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning(f"[touch_status] JSON 解析失败: {raw[:200]}")
            return
        if not isinstance(data, dict):
            return
        if data.get("command") != "touch_rising_edge":
            return
        pin = str(data.get("rising_edge_pin", ""))
        if pin not in self._head_touch_pins:
            return
        self._stop_running_game(f"touch:{pin}")

    def _stop_running_game(self, reason: str) -> None:
        with self._game_sessions_lock:
            sessions = list(self._active_game_sessions.items())
        if not sessions:
            status = self._runtime_link.send({
                "requestId": uuid.uuid4().hex, "command": "get_status",
            }, timeout=5.0)
            if status and status.get("ok") is True and status.get("sessionId"):
                stop_reason = "wake_word" if reason == "wake_word" else "operator"
                self._stop_smartapp(status["sessionId"], stop_reason)
            return
        for session_id, session in sessions:
            if session.get("runtime"):
                stop_reason = "wake_word" if reason == "wake_word" else "operator"
                self._stop_smartapp(session_id, stop_reason)
            elif self._push_game_stop(f"robot_game_view:stop:{reason}"):
                with self._game_sessions_lock:
                    self._active_game_sessions.pop(session_id, None)

    def _process_robot_game_start(
        self, event_id: str, body: Dict[str, Any], raw: Dict[str, Any]
    ) -> None:
        """处理游戏启动请求。

        参数：
          - gameUrl: 游戏 URL (示例: http://36.140.17.36:10000/robot/web/game-html/english/english_show_800x480.html)
          - sessionId: 会话 ID (UUID 格式)
          - data: 游戏数据（如果有 matchedText 则加入队列等待 ASR 确认，否则直接发送）
          - matchedText: ASR 匹配文本（可选，用于验证）

        实际数据示例：
          {
            "game": "start",
            "gameUrl": "http://36.140.17.36:10000/robot/web/game-html/english/english_show_800x480.html",
            "sessionId": "13d7e8b0-6ec6-47b8-bb96-c93e219e3446",
            "data": {
              "word": "wordy",
              "meaning": "同类词挑战"
            },
            "matchedText": "单词"  // 可选
          }
        """
        try:
            if "packageUrl" in body or "appid" in body:
                self._process_smartapp_start(event_id, body)
                return
            game_url = body.get("gameUrl", "")
            session_id = body.get("sessionId", "")
            data_payload = body.get("data", {})
            matched_text = body.get("matchedText", "")
            device_id = raw.get("deviceId", "")
            request_id = raw.get("requestId", "")

            if not game_url:
                self.logger.error(
                    f"[robot_game_view:start] gameUrl 为空，无法启动: event_id={event_id}"
                )
                return

            if not session_id:
                self.logger.warning(
                    f"[robot_game_view:start] sessionId 为空: event_id={event_id}"
                )

            self.logger.info(
                "[robot_game_view:start] 收到游戏启动请求\n"
                f"  event_id: {event_id}\n"
                f"  device_id: {device_id}\n"
                f"  request_id: {request_id}\n"
                f"  session_id: {session_id}\n"
                f"  game_url: {game_url}\n"
                f"  data: {data_payload}\n"
                f"  matchedText: {matched_text}"
            )

            # 从 gameUrl 中提取游戏类型（从路径中解析）
            # 示例: http://.../game-html/english/english_show_800x480.html -> english
            game_type = self._extract_game_type_from_url(game_url)
            if not game_type:
                self.logger.warning(
                    f"[robot_game_view:start] 无法从 URL 提取游戏类型: {game_url}"
                )
                game_type = "unknown"

            # 先发送游戏 URL
            url_to_send = game_url[4:] if game_url.startswith("URL:") else game_url
            if not self._send_game_udp(f"URL:{url_to_send}", "robot_game_view:start"):
                return

            # 根据配置开关决定数据发送方式
            if self.game_direct_send:
                # 直接发送模式：收到平台信令后立即发送 UDP
                if data_payload:
                    payload = json.dumps(data_payload, ensure_ascii=False)
                    if not self._send_game_udp(payload, "robot_game_view:start:direct"):
                        return
                    self.logger.info(
                        f"[robot_game_view:start] 直接发送模式: 已发送游戏数据 data={data_payload}"
                    )
                else:
                    # 没有 data 则发送 SHOW 命令
                    if not self._send_game_udp("SHOW", "robot_game_view:start:direct"):
                        return
            else:
                # ASR 匹配模式：如果有 matchedText，将 data 加入队列等待 ASR 确认
                if matched_text and data_payload:
                    with self._pending_game_data_lock:
                        # 清理过期数据
                        current_time = time.time()
                        expired_keys = [
                            k for k, v in self._pending_game_data.items()
                            if current_time - v.get("timestamp", 0) > self._pending_game_data_ttl
                        ]
                        for k in expired_keys:
                            self.logger.warning(f"[robot_game_view:start] 清理过期数据: matchedText={k}")
                            del self._pending_game_data[k]

                        # 添加新数据
                        self._pending_game_data[matched_text] = {
                            "data": data_payload,
                            "session_id": session_id,
                            "seq": 0,
                            "timestamp": current_time,
                            "event_id": event_id,
                        }
                        self.logger.info(
                            f"[robot_game_view:start] ASR匹配模式: 数据已加入待匹配队列: matchedText='{matched_text}' "
                            f"data={data_payload} (待 ASR 确认)"
                        )
                else:
                    # 没有 matchedText 或没有 data，按原逻辑处理
                    if data_payload:
                        payload = json.dumps(data_payload, ensure_ascii=False)
                        if not self._send_game_udp(payload, "robot_game_view:start"):
                            return
                    else:
                        # 没有 data 则发送 SHOW 命令
                        if not self._send_game_udp("SHOW", "robot_game_view:start"):
                            return

            with self._game_sessions_lock:
                self._active_game_sessions[session_id] = {
                    "game_type": game_type,
                    "game_url": game_url,
                    "data": data_payload,
                    "start_time": time.time(),
                    "device_id": device_id,
                    "event_id": event_id,
                }
            self.logger.info(
                f"[robot_game_view:start] 会话已记录: session_id={session_id} game_type={game_type}"
            )

        except Exception as e:
            self.logger.error(
                f"[robot_game_view:start] 处理异常: event_id={event_id} err={e}"
            )

    def _process_robot_game_stop(
        self, event_id: str, body: Dict[str, Any], raw: Dict[str, Any]
    ) -> None:
        """处理游戏停止请求。

        参数：
          - sessionId: 要停止的会话 ID

        TODO: 实现游戏停止逻辑
          1. 根据 sessionId 查找活跃会话
          2. 关闭游戏视图/容器
          3. 清理会话状态
          4. 回传停止结果
        """
        try:
            session_id = body.get("sessionId", "")
            device_id = raw.get("deviceId", "")
            request_id = raw.get("requestId", "")

            self.logger.info(
                "[robot_game_view:stop] 收到游戏停止请求\n"
                f"  event_id: {event_id}\n"
                f"  device_id: {device_id}\n"
                f"  request_id: {request_id}\n"
                f"  session_id: {session_id}"
            )

            with self._game_sessions_lock:
                session_info = self._active_game_sessions.get(session_id)
                legacy_sessions = any(not item.get("runtime") for item in self._active_game_sessions.values())
            if (session_info and session_info.get("runtime")) or not legacy_sessions:
                self._stop_smartapp(session_id, "cloud_stop")
                return
            if session_info is None:
                self.logger.warning(f"[robot_game_view:stop] unknown legacy session: {session_id}")
                return
            if not self._push_game_stop("robot_game_view:stop"):
                return

            with self._game_sessions_lock:
                if session_id in self._active_game_sessions:
                    session_info = self._active_game_sessions.pop(session_id)
                    self.logger.info(
                        f"[robot_game_view:stop] 会话已清理: session_id={session_id} "
                        f"game_type={session_info.get('game_type')}"
                    )
                else:
                    self.logger.warning(
                        f"[robot_game_view:stop] 会话未找到: session_id={session_id}"
                    )

        except Exception as e:
            self.logger.error(
                f"[robot_game_view:stop] 处理异常: event_id={event_id} err={e}"
            )

    def _handle_game_view_data(self, data: Dict[str, Any]) -> None:
        """处理 game_view_data 事件（游戏数据传输）。

        事件格式：
          {
            "deviceId": "11200",
            "domain": "DEVICE_ABILITY",
            "event": "game_view_data",
            "eventId": "唯一id",
            "seq": "时间戳",
            "body": {
              "sessionId": "abc123",           # 可选
              "seq": 1,
              "data": {
                "word": "wordy",               # 必需
                "meaning": "同类词挑战",        # 可选
                "gameUrl": "http://..."         # 可选，用于提取 game_type
              }
            }
          }

        game_type 获取优先级：
          1. 从 sessionId 关联的会话中获取
          2. 从 data.gameUrl 中提取
          3. 使用默认值 "english"
        """
        event_id = data.get("eventId", "")
        body = data.get("body", {})

        self.logger.info(
            f"[game_view_data] 启动异步处理线程: event_id={event_id[:16] if event_id else 'none'}..."
        )

        # 异步处理，避免阻塞 ROS 回调
        threading.Thread(
            target=self._process_game_view_data,
            kwargs={
                "event_id": event_id,
                "body": body,
                "raw": data,
            },
            name=f"game-data-{event_id[:16] if event_id else 'unknown'}",
            daemon=True,
        ).start()

    def _process_game_view_data(
        self, event_id: str, body: Dict[str, Any], raw: Dict[str, Any]
    ) -> None:
        """处理游戏视图数据消息。

        参数：
          - sessionId: 会话 ID（可选）
          - seq: 消息序号
          - data: 游戏数据对象（整体加入队列，不解析内部字段）
          - matchedText: ASR 匹配文本（可选，用于验证）

        处理模式（由 game_direct_send 配置控制）：
          - game_direct_send=true: 直接发送模式，收到平台信令后立即发送 UDP
          - game_direct_send=false: ASR 匹配模式，只有当收到 status=3 的 ASR 响应，
            且 msg 与 matchedText 一致时，才真正下发 UDP
        """
        try:
            session_id = body.get("sessionId", "")
            msg_seq = raw.get("seq", body.get("seq", 0))
            data_payload = body.get("data", {})
            matched_text = body.get("matchedText", "")

            with self._game_sessions_lock:
                session_info = self._active_game_sessions.get(session_id)
                legacy_sessions = any(not item.get("runtime") for item in self._active_game_sessions.values())
            if (session_info and session_info.get("runtime")) or not legacy_sessions:
                if type(msg_seq) is not int or type(data_payload) is not dict:
                    self.logger.warning(f"[game_view_data] invalid Runtime data: event_id={event_id}")
                    return
                command = {
                    "requestId": uuid.uuid4().hex,
                    "command": "cloud_data",
                    "sessionId": session_id,
                    "seq": msg_seq,
                    "data": data_payload,
                }
                for source, destination in (("target", "target"), ("type", "dataType"), ("trigger", "trigger")):
                    if source in body:
                        command[destination] = body[source]
                self._send_runtime_command(command)
                return
            if session_info is None:
                self.logger.warning(f"[game_view_data] unknown legacy session: {session_id}")
                return

            self.logger.info(
                f"[game_view_data] 收到游戏数据\n"
                f"  event_id: {event_id}\n"
                f"  session_id: {session_id}\n"
                f"  seq: {msg_seq}\n"
                f"  data: {data_payload}\n"
                f"  matchedText: {matched_text}\n"
                f"  game_direct_send: {self.game_direct_send}"
            )

            if not data_payload:
                self.logger.warning(
                    f"[game_view_data] data 为空，跳过推送: event_id={event_id}"
                )
                return

            # 根据配置开关决定数据发送方式
            if self.game_direct_send:
                # 直接发送模式：收到平台信令后立即发送 UDP
                payload = json.dumps(data_payload, ensure_ascii=False)
                if self._send_game_udp(payload, "game_view_data:direct"):
                    self.logger.info(
                        f"[game_view_data] 直接发送模式: UDP 已下发: session_id={session_id} "
                        f"seq={msg_seq} data={data_payload}"
                    )
                else:
                    self.logger.error(
                        f"[game_view_data] 直接发送模式: UDP 下发失败: event_id={event_id}"
                    )
            else:
                # ASR 匹配模式：将数据加入待匹配队列，等待 ASR 确认
                if not matched_text:
                    self.logger.warning(
                        f"[game_view_data] ASR匹配模式下 matchedText 为空，跳过推送: event_id={event_id}"
                    )
                    return

                with self._pending_game_data_lock:
                    # 清理过期数据
                    current_time = time.time()
                    expired_keys = [
                        k for k, v in self._pending_game_data.items()
                        if current_time - v.get("timestamp", 0) > self._pending_game_data_ttl
                    ]
                    for k in expired_keys:
                        self.logger.warning(f"[game_view_data] 清理过期数据: matchedText={k}")
                        del self._pending_game_data[k]

                    # 添加新数据（直接存储完整的 data 对象）
                    self._pending_game_data[matched_text] = {
                        "data": data_payload,
                        "session_id": session_id,
                        "seq": msg_seq,
                        "timestamp": current_time,
                        "event_id": event_id,
                    }
                    self.logger.info(
                        f"[game_view_data] ASR匹配模式: 数据已加入待匹配队列: matchedText='{matched_text}' "
                        f"data={data_payload} (待 ASR 确认)"
                    )

        except Exception as e:
            self.logger.error(
                f"[game_view_data] 处理异常: event_id={event_id} err={e}"
            )

    def _extract_game_type_from_url(self, game_url: str) -> str:
        """从游戏 URL 中提取游戏类型。

        示例:
          http://.../game-html/english/english_show_800x480.html -> english
          http://.../game-html/math/math_game.html -> math

        返回:
          游戏类型字符串，如果无法提取则返回空字符串
        """
        try:
            # 尝试从路径中提取 game-html/{type}/ 格式
            if "game-html/" in game_url:
                parts = game_url.split("game-html/")[1].split("/")
                if parts:
                    return parts[0]
            return ""
        except Exception as e:
            self.logger.warning(f"提取游戏类型失败: {e}")
            return ""

    # /findobj/search_status 状态码定义
    # 功能类 00-09；过程类 10-19 / 21-28
    # tts: str | List[str]
    #   - "" / []  → 不播报
    #   - str      → 单条文案
    #   - List[str]→ 随机选一条
    # 占位符 {item}：直接用 pending.item_name（平台 itemName 中文名），空则「目标」
    FINDOBJ_STATUS_TABLE: Dict[str, Dict[str, Any]] = {
        # ---- 功能类 ----
        "00": {
            "category": "func",
            "phase": "progress",
            "ok": True,
            "desc": "端侧收到开启信令",
            "tts": "",  # 技术确认，不播报
        },
        "01": {
            "category": "func",
            "phase": "terminal",
            "ok": False,
            "desc": "初始化异常_云端通信失败",
            "tts": "",
        },
        "02": {
            "category": "func",
            "phase": "terminal",
            "ok": False,
            "desc": "初始化异常_云端接流失败",
            "tts": "",
        },
        "09": {
            "category": "func",
            "phase": "progress",
            "ok": True,
            "desc": "初始化成功，云端开始推理",
            "tts": "开始寻找{item}",
        },
        # ---- 过程类 ----
        "10": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "发现目标，开始执行靠近流程",
            "tts": "发现{item}，正在靠近",
        },
        "11": {
            "category": "process",
            "phase": "terminal",
            "ok": False,
            "desc": "执行完搜索策略，但始终没有发现目标",
            "tts": "没有找到{item}",
        },
        "12": {
            "category": "process",
            "phase": "terminal",
            "ok": False,
            "desc": "发现目标后，在靠近过程中丢失目标",
            "tts": "靠近{item}时目标丢失了",
        },
        "13": {
            "category": "process",
            "phase": "terminal",
            "ok": True,
            "desc": "成功到达目标旁边",
            "tts": "已经到达{item}旁边",
        },
        "14": {
            "category": "process",
            "phase": "terminal",
            "ok": False,
            "desc": "推理过程中拉流异常（超过2s没有有效视频帧）",
            "tts": "视频流异常，寻物中断",
        },
        "19": {
            "category": "process",
            "phase": "terminal",
            "ok": True,
            "desc": "本轮任务结束，可下发下一次开启命令",
            "tts": "本轮寻物结束",
        },
        "21": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "重定位成功，开始导航",
            "tts": "",
        },
        "22": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "成功到达导航点位",
            "tts": "我到啦，让我瞪大眼睛360°仔细看看哦~",
        },
        "23": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "找到目标",
            # 多套话术随机播报，{item} 替换为 pending.item_name
            "tts": [
                "我找到{item}啦，快夸夸我！",
                "{item}在我面前呢，我看见啦！",
                "嘿嘿，我发现{item}啦，就在我前面！你看看是不是这个呀",
                "找到啦，{item}就在我眼前，你来看看吧！",
                "找到{item}啦，我是不是很棒！",
                "我记得没错，{item}就在这里呢，我可太聪明啦哈哈哈",
                "找到啦找到啦，{item}就在我面前呢！",
            ],
        },
        "24": {
            "category": "process",
            "phase": "progress",
            "ok": False,
            "desc": "未找到目标",
            # 多套话术随机播报，{item} 替换为物品名
            "tts": [
                "我仔细找啦，这里没有{item}。你先别急，我们想想是不是放到别处啦？",
                "这里没看到{item}呢。别着急，我们一起回忆下它可能去哪儿啦。",
                "我认真看过啦，{item}不在这儿。先别慌，也许它换地方待着啦。",
                "{item}好像不在这个位置。没关系，我们想想上次在哪儿见过它。",
                "我到这边找过啦，没发现{item}。你别急，我们再想想其他地方。",
                "这里空空的，没有{item}呀。别担心，它可能被放到别处啦。",
                "我看得很仔细，还是没找到{item}。先不急，我们一起想想线索。",
                "{item}不在这里呢。你先别急，也许后来又换位置啦。",
                "我确认过啦，这里没有{item}。没关系，我们再想想它可能在哪儿。",
            ],
        },
        "25": {
            "category": "process",
            "phase": "terminal",
            "ok": False,
            "desc": "下发了scene且与现有scene列表不匹配，直接终止本次寻物",
            "tts": "找不到点位，无法寻找{item}",
        },
        "26": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "第一个旋转90度期间仔细寻找",
            "tts": "我正在仔细找呢！别急别急等我看看",
        },
        "27": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "初始化原地找物（无scene），当前面前未找到",
            "tts": "我面前这里没有找到哎，我再去别的位置看看哦～",
        },
        "28": {
            "category": "process",
            "phase": "progress",
            "ok": True,
            "desc": "当前点位没有，前往下一个点位去找",
            "tts": "哎呀，我转了一圈没有发现你说的目标呢，稍等我一下，我再去其他地方找找看吧~",
        },
    }

    @staticmethod
    def _normalize_findobj_status_code(value: Any) -> str:
        """将 status 规范为两位字符串，如 0/'0'/00 → '00'，9 → '09'。"""
        if value is None:
            return ""
        text = str(value).strip()
        if not text:
            return ""
        # 允许 "status": 10 或 "10" 或 "Status_10"
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            return text.lower()
        try:
            return f"{int(digits):02d}"
        except ValueError:
            return text.lower()

    def _on_findobj_ctl(self, msg: String) -> None:
        """处理 /findobj_ctl 寻物控制消息（本体→算法功能开关）。

        期望 std_msgs/String.data 为 JSON：
          {
            "SN": "1222004229866666660002891",
            "prompt": "ball",
            "scene": "阳台",  // 可选，有则先导航再寻物
            "status": "00"    // 00=开启, 01=结束, 02=暂停, 03=继续
          }

        控制策略：
          - status=00: 开启寻物
            - 有 scene: 先导航到记忆中的 scene 点再寻物
            - 无 scene: 触发普通寻物
          - status=01: 结束寻物
          - status=02: 暂停寻物
          - status=03: 继续寻物
        """
        raw = (msg.data or "").strip()
        if not raw:
            self.logger.warning("[findobj_ctl] 空消息，忽略")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning(f"[findobj_ctl] JSON 解析失败: {raw[:200]}")
            return

        if not isinstance(data, dict):
            self.logger.warning(f"[findobj_ctl] 非对象 JSON，忽略: {type(data)}")
            return

        sn = str(data.get("SN") or data.get("sn") or "").strip()
        prompt = str(data.get("prompt") or "").strip()
        scene = str(data.get("scene") or "").strip()
        status = str(data.get("status") or "00").strip()

        # 状态名称映射
        status_names = {
            "00": "开启寻物",
            "01": "结束寻物",
            "02": "暂停寻物",
            "03": "继续寻物",
        }
        status_name = status_names.get(status, "未知")

        self.logger.info("=" * 80)
        self.logger.info("🔍 [收到寻物控制指令 /findobj_ctl]")
        self.logger.info(f"SN: {sn}")
        self.logger.info(f"status: {status} ({status_name})")
        if status == "00":
            self.logger.info(f"prompt: {prompt}")
            self.logger.info(f"scene: {scene or '(无，普通寻物)'}")
        self.logger.info(f"完整数据: {json.dumps(data, ensure_ascii=False)}")
        self.logger.info("=" * 80)

        # 验证必填字段
        if not sn:
            self.logger.error("[findobj_ctl] 缺少 SN 字段，忽略")
            return

        # 根据 status 执行不同策略
        if status == "00":  # 开启寻物
            if not prompt:
                self.logger.error("[findobj_ctl] status=00 开启寻物时缺少 prompt 字段，忽略")
                return
            if scene:
                self.logger.info(f"[findobj_ctl] 场景寻物: 导航至 '{scene}' 后寻找 '{prompt}'")
            else:
                self.logger.info(f"[findobj_ctl] 普通寻物: 直接寻找 '{prompt}'")
        elif status == "01":
            self.logger.info(f"[findobj_ctl] 结束寻物: SN={sn}")
        elif status == "02":
            self.logger.info(f"[findobj_ctl] 暂停寻物: SN={sn}")
        elif status == "03":
            self.logger.info(f"[findobj_ctl] 继续寻物: SN={sn}")
        else:
            self.logger.warning(f"[findobj_ctl] 未知 status={status}，忽略")
            return

        # 功能开关接口仅记录日志，实际控制由算法侧订阅 /findobj_ctl 处理
        self.logger.info(f"[findobj_ctl] 控制指令已接收，由算法侧订阅 {self.findobj_ctl_topic} 处理")

    def _on_findobj_search_status(self, msg: String) -> None:
        """处理 /findobj/search_status 寻物状态。

        期望 std_msgs/String.data 为 JSON：
          {"SN":"...","status":"00|01|...|28","prompt":"...","scene":"...","msg":"..."}

        status 码表见 FINDOBJ_STATUS_TABLE。
        - progress：过程/功能中间态，仅日志 + 预留钩子
        - terminal：终态，回传平台 item_search 并清除 pending
        """
        raw = (msg.data or "").strip()
        if not raw:
            self.logger.warning("[findobj_status] 空消息，忽略")
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self.logger.warning(f"[findobj_status] JSON 解析失败: {raw[:200]}")
            return

        if not isinstance(data, dict):
            self.logger.warning(f"[findobj_status] 非对象 JSON，忽略: {type(data)}")
            return

        sn = str(
            data.get("SN")
            or data.get("sn")
            or data.get("deviceId")
            or data.get("device_id")
            or ""
        ).strip()
        status_code = self._normalize_findobj_status_code(
            data.get("status", data.get("state", data.get("result", "")))
        )
        prompt = str(data.get("prompt") or data.get("itemCode") or "").strip()
        scene = str(data.get("scene") or data.get("pointName") or "").strip()

        meta = self.FINDOBJ_STATUS_TABLE.get(status_code)
        if meta is None:
            self.logger.warning(
                f"[findobj_status] 未知 status={status_code!r}，原始={raw[:200]}"
            )
            # 未知码仍尽量关联 pending 打日志，不回传
            self._lookup_pending_item_search(sn)
            return

        desc = str(meta.get("desc") or "")
        phase = str(meta.get("phase") or "progress")
        ok = bool(meta.get("ok", True))
        category = str(meta.get("category") or "")

        msg_text = str(
            data.get("msg")
            or data.get("message")
            or data.get("detail")
            or data.get("reason")
            or desc
            or status_code
        ).strip()

        self.logger.info(
            "[findobj_status] 收到: SN=%s status=%s(%s) phase=%s category=%s "
            "ok=%s prompt=%s scene=%s msg=%s",
            sn or "-",
            status_code,
            desc or "-",
            phase,
            category or "-",
            ok,
            prompt or "-",
            scene or "-",
            msg_text or "-",
        )

        pending = self._lookup_pending_item_search(sn)
        if pending is None:
            self.logger.warning(
                f"[findobj_status] 无匹配 pending: SN={sn or '-'} status={status_code}"
            )
            # 无 pending 也走预留钩子，便于调试
            self._dispatch_findobj_status(
                status_code=status_code,
                meta=meta,
                sn=sn,
                pending=None,
                data=data,
                msg_text=msg_text,
            )
            return

        # 更新 sn（兜底匹配时可能回填）
        sn = sn or pending.get("device_id") or ""

        # 分发到各 status 预留处理
        self._dispatch_findobj_status(
            status_code=status_code,
            meta=meta,
            sn=sn,
            pending=pending,
            data=data,
            msg_text=msg_text,
        )

        if phase != "terminal":
            self.logger.info(
                "[findobj_status] 过程/功能中间态，不回传平台: event_id=%s status=%s %s",
                pending.get("event_id") or "-",
                status_code,
                desc,
            )
            return

        # ---- 终态：回传平台并清 pending ----
        device_id = pending.get("device_id") or sn
        event_id = pending.get("event_id") or ""
        # 平台 body.code：成功 0，失败用两位 status 数值（如 01→1, 11→11）
        try:
            platform_code = 0 if ok else int(status_code)
        except ValueError:
            platform_code = 0 if ok else 1

        extra = {
            "status": status_code,
            "statusDesc": desc,
            "category": category,
            "itemName": pending.get("item_name") or "",
            "itemCode": pending.get("item_code") or prompt,
            "pointName": pending.get("point_name") or scene,
        }
        for key in ("x", "y", "z", "distance", "score", "bbox", "result", "detail"):
            if key in data and key not in extra:
                extra[key] = data[key]

        ret = self._send_item_search_response(
            device_id=device_id,
            event_id=event_id,
            code=platform_code,
            msg=msg_text or desc or ("ok" if ok else "failed"),
            extra=extra,
        )

        with self._pending_item_searches_lock:
            self._pending_item_searches.pop(sn, None)
            # 若 sn 与 pending device_id 不同，双清
            did = pending.get("device_id")
            if did and did != sn:
                self._pending_item_searches.pop(did, None)

        self.logger.info(
            "[findobj_status] 终态已回传平台: event_id=%s SN=%s status=%s code=%s send_ret=%s",
            event_id,
            sn or "-",
            status_code,
            platform_code,
            ret,
        )

    def _lookup_pending_item_search(self, sn: str) -> Optional[Dict[str, Any]]:
        """按 SN 查找 pending；仅有一个 pending 时允许无 SN 兜底。"""
        pending = None
        if sn:
            with self._pending_item_searches_lock:
                pending = self._pending_item_searches.get(sn)
        if pending is None:
            with self._pending_item_searches_lock:
                if len(self._pending_item_searches) == 1:
                    _, pending = next(iter(self._pending_item_searches.items()))
        return pending

    def _dispatch_findobj_status(
        self,
        status_code: str,
        meta: Dict[str, Any],
        sn: str,
        pending: Optional[Dict[str, Any]],
        data: Dict[str, Any],
        msg_text: str,
    ) -> None:
        """按 status 码分发到预留处理函数。"""
        handlers = {
            "00": self._on_findobj_status_00_ack,
            "01": self._on_findobj_status_01_cloud_comm_fail,
            "02": self._on_findobj_status_02_cloud_stream_fail,
            "09": self._on_findobj_status_09_init_ok,
            "10": self._on_findobj_status_10_found_approach,
            "11": self._on_findobj_status_11_not_found_after_search,
            "12": self._on_findobj_status_12_lost_during_approach,
            "13": self._on_findobj_status_13_arrived,
            "14": self._on_findobj_status_14_stream_timeout,
            "19": self._on_findobj_status_19_round_end,
            "21": self._on_findobj_status_21_ready_walk,
            "22": self._on_findobj_status_22_reached_place,
            "23": self._on_findobj_status_23_target_found,
            "24": self._on_findobj_status_24_target_not_found,
            "25": self._on_findobj_status_25_scene_mismatch,
            "26": self._on_findobj_status_26_rotating,
            "27": self._on_findobj_status_27_inplace_not_found,
            "28": self._on_findobj_status_28_goto_next_point,
        }
        handler = handlers.get(status_code)
        if handler is None:
            self.logger.debug(f"[findobj_status] 无专用钩子: status={status_code}")
            return
        try:
            handler(sn=sn, pending=pending, data=data, meta=meta, msg_text=msg_text)
        except Exception as e:
            self.logger.error(
                f"[findobj_status] 钩子异常 status={status_code}: {e}"
            )

        # 状态响应 TTS 播报
        self._play_findobj_status_tts(
            status_code=status_code,
            meta=meta,
            pending=pending,
            data=data,
            msg_text=msg_text,
        )

    @staticmethod
    def _pick_findobj_tts_template(tts_cfg: Any) -> str:
        """从码表 tts 配置选取一条模板。

        - "" / None / [] → 空串（不播报）
        - str → 原样
        - list/tuple → 过滤空项后 random.choice；全空则空串
        """
        if tts_cfg is None:
            return ""
        if isinstance(tts_cfg, str):
            return tts_cfg.strip()
        if isinstance(tts_cfg, (list, tuple)):
            candidates = [str(x).strip() for x in tts_cfg if str(x).strip()]
            if not candidates:
                return ""
            return random.choice(candidates)
        return str(tts_cfg).strip()

    def _play_findobj_status_tts(
        self,
        status_code: str,
        meta: Dict[str, Any],
        pending: Optional[Dict[str, Any]],
        data: Dict[str, Any],
        msg_text: str,
    ) -> None:
        """根据 search_status 码播报 TTS。

        tts 支持 str 或 List[str]（随机一条）。
        占位符 {item} 直接替换为 pending.item_name；为空则「目标」。
        tts 为空 / 空列表时不播报。
        """
        tts_text = self._pick_findobj_tts_template(meta.get("tts"))
        if not tts_text:
            self.logger.debug(
                f"[findobj_status] status={status_code} 无 TTS 文案，跳过播报"
            )
            return

        item_name = ""
        if pending:
            item_name = str(pending.get("item_name") or "").strip()

        tts_text = tts_text.replace("{item}", item_name or "目标")

        self.logger.info(
            f"[findobj_status] TTS 播报 status={status_code}: {tts_text}"
        )
        self.play_tts_async(tts_text, play_now=True, multi_wheel=False)

    # ─── 各 status 预留处理（日志 + 由 _play_findobj_status_tts 统一播报）───

    def _on_findobj_status_00_ack(self, **kwargs) -> None:
        """00 端侧收到开启信令。"""
        self.logger.info("[findobj_status:00] 端侧已收到开启信令")

    def _on_findobj_status_01_cloud_comm_fail(self, **kwargs) -> None:
        """01 初始化异常_云端通信失败。"""
        self.logger.warning("[findobj_status:01] 云端通信失败")

    def _on_findobj_status_02_cloud_stream_fail(self, **kwargs) -> None:
        """02 初始化异常_云端接流失败。"""
        self.logger.warning("[findobj_status:02] 云端接流失败")

    def _on_findobj_status_09_init_ok(self, **kwargs) -> None:
        """09 初始化成功，云端开始推理。"""
        self.logger.info("[findobj_status:09] 初始化成功，云端开始推理")

    def _on_findobj_status_10_found_approach(self, **kwargs) -> None:
        """10 发现目标，开始执行靠近流程。"""
        self.logger.info("[findobj_status:10] 发现目标，开始靠近")

    def _on_findobj_status_11_not_found_after_search(self, **kwargs) -> None:
        """11 执行完搜索策略，但始终没有发现目标。"""
        self.logger.warning("[findobj_status:11] 搜索结束仍未发现目标")

    def _on_findobj_status_12_lost_during_approach(self, **kwargs) -> None:
        """12 发现目标后，在靠近过程中丢失目标。"""
        self.logger.warning("[findobj_status:12] 靠近过程中丢失目标")

    def _on_findobj_status_13_arrived(self, **kwargs) -> None:
        """13 成功到达目标旁边。"""
        self.logger.info("[findobj_status:13] 成功到达目标旁边")

    def _on_findobj_status_14_stream_timeout(self, **kwargs) -> None:
        """14 推理过程中拉流异常（超过2s没有有效视频帧）。"""
        self.logger.warning("[findobj_status:14] 拉流异常/超时")

    def _on_findobj_status_19_round_end(self, **kwargs) -> None:
        """19 本轮任务结束，可下发下一次开启命令。"""
        self.logger.info("[findobj_status:19] 本轮任务结束，可接受下一次开启")

    def _on_findobj_status_21_ready_walk(self, **kwargs) -> None:
        """21 重定位成功，开始导航。"""
        self.logger.info("[findobj_status:21] 重定位成功，开始导航")

    def _on_findobj_status_22_reached_place(self, **kwargs) -> None:
        """22 成功到达导航点位。"""
        self.logger.info("[findobj_status:22] 成功到达导航点位")

    def _on_findobj_status_23_target_found(self, **kwargs) -> None:
        """23 找到目标。"""
        self.logger.info("[findobj_status:23] 找到目标")

    def _on_findobj_status_24_target_not_found(self, **kwargs) -> None:
        """24 未找到目标。"""
        self.logger.info("[findobj_status:24] 未找到目标")

    def _on_findobj_status_25_scene_mismatch(self, **kwargs) -> None:
        """25 下发 scene 与现有 scene 列表不匹配，直接终止本次寻物。"""
        self.logger.warning("[findobj_status:25] scene 不匹配，终止本次寻物")

    def _on_findobj_status_26_rotating(self, **kwargs) -> None:
        """26 第一个旋转90度期间仔细寻找。"""
        self.logger.info("[findobj_status:26] 旋转中仔细寻找")

    def _on_findobj_status_27_inplace_not_found(self, **kwargs) -> None:
        """27 初始化原地找物（无scene），当前面前未找到。"""
        self.logger.info("[findobj_status:27] 原地未找到，准备去其他位置")

    def _on_findobj_status_28_goto_next_point(self, **kwargs) -> None:
        """28 当前点位没有，前往下一个点位去找。"""
        self.logger.info("[findobj_status:28] 当前点位未找到，前往下一点位")

    # ──────────────────────────────────────────────────────────────────
    # TTS 语音合成（/audio_center/play_tts）
    # ──────────────────────────────────────────────────────────────────

    def play_tts(
        self,
        text: str,
        *,
        play_now: bool = True,
        multi_wheel: bool = False,
        tts_session: str = "",
        nlu_session: str = "",
        timeout_sec: Optional[float] = None,
        wait: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """调用 audio_center TTS 播报。

        Service: /audio_center/play_tts
        Type: homi_speech_interface/srv/AssistantSpeechText

        示例等价：
          ros2 service call /audio_center/play_tts \\
            homi_speech_interface/srv/AssistantSpeechText \\
            "{msg: '你好', play_now: true, multi_wheel: false, tts_session: '', nlu_session: ''}"

        Args:
            text: 待合成文本
            play_now: True=打断当前立即播；False=排队
            multi_wheel: TTS 完成后是否继续下一轮对话
            tts_session: TTS 会话标识（JSON 字符串，可含 speed 等）
            nlu_session: NLU 会话标识
            timeout_sec: 等待服务/响应超时（秒）
            wait: True=同步等待响应；False=异步发送（默认）

        Returns:
            wait=True → {"section_id": str, "error_code": int}
            wait=False → 提交成功返回 None；提交失败返回带 error_code 的 dict
        """
        text = (text or "").strip()
        if not text:
            self.logger.warning("[TTS] 文本为空，跳过播报")
            return {"section_id": "", "error_code": -1}

        timeout = float(timeout_sec if timeout_sec is not None else self._tts_timeout_sec)

        if not self._tts_client.service_is_ready():
            self.logger.warning(f"[TTS] 服务未就绪，等待: {self._tts_service_name}")
            if not self._tts_client.wait_for_service(timeout_sec=min(timeout, 2.0)):
                self.logger.error(f"[TTS] 等待服务超时: {self._tts_service_name}")
                return {"section_id": "", "error_code": -1}

        req = AssistantSpeechText.Request()
        req.msg = text
        req.play_now = bool(play_now)
        req.multi_wheel = bool(multi_wheel)
        req.tts_session = tts_session or ""
        req.nlu_session = nlu_session or ""

        self.logger.info(
            "[TTS] 播报: text=%s play_now=%s multi_wheel=%s wait=%s",
            text if len(text) <= 50 else text[:50] + "...",
            play_now,
            multi_wheel,
            wait,
        )

        if not wait:
            future = self._tts_client.call_async(req)

            def _done(fut):
                try:
                    resp = fut.result()
                    self.logger.info(
                        "[TTS] 异步完成: section_id=%s error_code=%s",
                        getattr(resp, "section_id", ""),
                        getattr(resp, "error_code", None),
                    )
                except Exception as e:
                    self.logger.error(f"[TTS] 异步回调异常: {e}")

            future.add_done_callback(_done)
            return None

        # 同步：兼容工作线程调用（不依赖 spin_until_future_complete）
        result_holder: Dict[str, Any] = {"section_id": "", "error_code": -2}
        done_event = threading.Event()

        def _done_sync(fut):
            try:
                resp = fut.result()
                result_holder["section_id"] = str(getattr(resp, "section_id", "") or "")
                result_holder["error_code"] = int(getattr(resp, "error_code", -3))
            except Exception as e:
                self.logger.error(f"[TTS] 同步回调异常: {e}")
                result_holder["error_code"] = -3
            finally:
                done_event.set()

        future = self._tts_client.call_async(req)
        future.add_done_callback(_done_sync)

        if not done_event.wait(timeout=timeout):
            self.logger.error(f"[TTS] 等待响应超时 ({timeout}s)")
            return {"section_id": "", "error_code": -20}

        if result_holder["error_code"] == 0:
            self.logger.info(
                "[TTS] 播报成功: section_id=%s", result_holder["section_id"]
            )
        else:
            self.logger.warning(
                "[TTS] 播报失败: error_code=%s section_id=%s",
                result_holder["error_code"],
                result_holder["section_id"],
            )
        return result_holder

    def play_tts_async(
        self,
        text: str,
        *,
        play_now: bool = True,
        multi_wheel: bool = False,
        tts_session: str = "",
        nlu_session: str = "",
    ) -> None:
        """异步 TTS 播报（不阻塞调用线程）。"""
        self.play_tts(
            text,
            play_now=play_now,
            multi_wheel=multi_wheel,
            tts_session=tts_session,
            nlu_session=nlu_session,
            wait=False,
        )

    def _send_item_search_response(
        self,
        device_id: str,
        event_id: str,
        code: int = 0,
        msg: str = "ok",
        extra: Optional[Dict[str, Any]] = None,
    ) -> int:
        """回传 item_search 执行结果（预留）。

        响应格式对齐 DEVICE_ABILITY 惯例：
        {
          "deviceId": "...",
          "domain": "DEVICE_ABILITY",
          "event": "item_search",
          "eventId": "<原请求 eventId>",
          "seq": "<ts>",
          "body": {"code": 0, "msg": "ok", ...}
        }
        """
        ts = int(time.time() * 1000)
        body: Dict[str, Any] = {"code": code, "msg": msg}
        if extra:
            body.update(extra)

        payload = {
            "deviceId": device_id,
            "domain": "DEVICE_ABILITY",
            "event": "item_search",
            "eventId": event_id,
            "seq": str(ts),
            "body": body,
        }
        data = json.dumps(payload, ensure_ascii=False)
        self.logger.info(
            "[item_search] 发送响应: device_id=%s event_id=%s code=%s msg=%s",
            device_id,
            event_id,
            code,
            msg,
        )
        self.logger.debug(
            "[item_search] 响应 payload:\n%s",
            json.dumps(payload, ensure_ascii=False, indent=2),
        )
        return self._send_from_thread(data)

    def _notify_clients_thread_safe(self, update: Dict[str, Any]):
        """在独立线程中通知客户端，避免阻塞 ROS2 线程"""
        try:
            self._notify_long_poll_waiters()
            self._notify_sse_clients(update)
        except Exception as e:
            self.logger.error(f"通知客户端异常: {e}")

    def _get_updates_since(self, offset: int) -> List[Dict[str, Any]]:
        """按 offset 获取 updates，不消费队列内容。

        Hermes Gateway 会用 offset 追踪已读 update。这里必须保留短期历史，
        否则任意一次 offset 较大的请求都会把 Queue 中较小 update 取出并丢弃，
        造成后续请求没有响应。
        """
        with self._outgoing_updates_lock:
            return [u for u in self._outgoing_updates if int(u.get("update_id", 0)) >= offset]

    def _notify_long_poll_waiters(self):
        """唤醒所有长轮询等待者"""
        if not self._server_loop:
            return

        async def wake_waiters():
            for waiter in self._long_poll_waiters[:]:
                if not waiter.done():
                    waiter.set_result(None)

        asyncio.run_coroutine_threadsafe(wake_waiters(), self._server_loop)

    def _notify_sse_clients(self, update: Dict[str, Any]):
        """推送更新到所有 SSE 客户端"""
        if not self._server_loop:
            return

        async def push_to_clients():
            for queue_id, sse_queue in list(self._sse_queues.items()):
                try:
                    sse_queue.put_nowait(update)
                    self.logger.debug(f"[SSE] Update {update['update_id']} queued for {queue_id}")
                except asyncio.QueueFull:
                    self.logger.warning(f"[SSE] Queue full for {queue_id}")

        asyncio.run_coroutine_threadsafe(push_to_clients(), self._server_loop)

    # ──────────────────────────────────────────────────────────────────
    # 语音助手状态监控
    # ──────────────────────────────────────────────────────────────────

    def _on_assistant_status(self, msg: AssistantEvent):
        """
        处理语音助手状态消息

        状态说明：
        - status: 1 (Running) - 运行中
        - status: 9 (Wakeup) - 唤醒
        - status: 2 (AsrResponsed) - ASR 响应
        - status: 3 - 用于游戏数据匹配验证
        """
        # 解析 msg 字段（如果是 JSON）
        msg_data = None
        if msg.msg:
            try:
                msg_data = json.loads(msg.msg)
            except json.JSONDecodeError:
                msg_data = msg.msg

        # 打印状态信息（包含完整 msg 数据）
        self.logger.info(
            f"[语音助手状态] status={msg.status} ({msg.description}) "
            f"section_id={msg.section_id} msg_data={msg_data}"
        )

        if msg.status == 9:
            threading.Thread(
                target=self._stop_running_game,
                args=("wake_word",),
                name="game-wake-word-stop",
                daemon=True,
            ).start()
            return

        # status=3: 游戏数据匹配验证
        if msg.status == 3:
            # 直接使用 msg.msg 字符串进行匹配
            matched_text = msg.msg if isinstance(msg.msg, str) else ""
            if matched_text:
                self.logger.info(f"[游戏匹配] 收到 status=3 消息: '{matched_text}'")
                self._check_and_push_game_data(matched_text)
            else:
                self.logger.warning("[游戏匹配] status=3 但 msg 为空")
            return

        # 如果有 ASR 结果，打印详细信息
        if msg.status == 2 and msg_data and isinstance(msg_data, dict):
            result = msg_data.get('result', {})
            text = result.get('text', '')
            finish = result.get('finish', False)
            index = result.get('index', 0)

            self.logger.info(
                f"  → ASR 结果: text='{text}' finish={finish} index={index}"
            )

            if finish and text:
                if not self.forward_asr_to_channel:
                    self.logger.debug(
                        "forward_asr_to_channel=false，跳过 ASR 推送: text=%s",
                        safe_id(text, 50),
                    )
                    return

                section_id = msg.section_id or f"asr_{int(time.time() * 1000)}"
                if section_id in self._processed_asr_sections:
                    self.logger.debug(f"跳过重复最终 ASR: section_id={section_id}")
                    return
                self._processed_asr_sections.add(section_id)
                if len(self._processed_asr_sections) > self._max_processed_ids:
                    self._processed_asr_sections = set(list(self._processed_asr_sections)[-self._max_processed_ids // 2:])

                device_id = self._extract_device_id_from_section(section_id) or self.device_id
                self.logger.info(
                    "最终 ASR 结果推送给 Hermes: device_id=%s event_id=%s text=%s",
                    device_id,
                    section_id,
                    safe_id(text, 50),
                )
                self._push_text_to_gateway(
                    device_id=device_id,
                    event_id=section_id,
                    text=text,
                    source="asr",
                )

    # ──────────────────────────────────────────────────────────────────
    # 工具函数
    # ──────────────────────────────────────────────────────────────────

    def _extract_device_id(self, chat_id: str) -> str:
        """从 chat_id 中提取 device_id"""
        if chat_id.startswith('chat-'):
            return chat_id[5:]
        return chat_id

    def _check_and_push_game_data(self, asr_text: str) -> None:
        """检查 ASR 文本是否包含待推送的游戏数据的 matchedText，匹配则下发 UDP。

        Args:
            asr_text: ASR 识别的文本
        """
        with self._pending_game_data_lock:
            # 遍历所有待匹配数据，检查 asr_text 是否包含 matchedText
            matched_items = []
            for matched_text, pending_data in self._pending_game_data.items():
                if matched_text in asr_text:
                    matched_items.append((matched_text, pending_data))

            if matched_items:
                for matched_text, pending_data in matched_items:
                    data_payload = pending_data["data"]
                    session_id = pending_data["session_id"]
                    seq = pending_data["seq"]
                    event_id = pending_data["event_id"]

                    self.logger.info(
                        f"[game_view_data] ASR 匹配成功: asr_text='{asr_text}' "
                        f"matchedText='{matched_text}' data={data_payload}"
                    )

                    # 下发 UDP 消息（发送完整的 data JSON）
                    payload = json.dumps(data_payload, ensure_ascii=False)
                    if self._send_game_udp(payload, "game_view_data"):
                        self.logger.info(
                            f"[game_view_data] UDP 已下发: session_id={session_id} seq={seq} "
                            f"data={data_payload}"
                        )
                    else:
                        self.logger.error(
                            f"[game_view_data] UDP 下发失败: event_id={event_id}"
                        )

                    # 从队列中移除
                    del self._pending_game_data[matched_text]
            else:
                self.logger.debug(
                    f"[game_view_data] ASR 文本无匹配: '{asr_text}'"
                )

    def _extract_device_id_from_section(self, section_id: str) -> str:
        """从语音助手 section_id 中提取 device_id。

        常见格式：sph{device_id}_{timestamp}，例如：
        sph1222004229866666660004657_1781829111001
        """
        raw = str(section_id or "").strip()
        if not raw:
            return ""
        if raw.startswith("sph") and "_" in raw:
            return raw[3:].rsplit("_", 1)[0]
        return ""

    def _is_duplicate_content(self, user_id: str, text: str) -> bool:
        """检查是否是重复内容（基于内容指纹）"""
        fingerprint = compute_content_fingerprint(user_id, text)
        current_time = time.time()

        # 清理过期指纹
        expired_keys = [
            k for k, t in self._content_fingerprints.items()
            if current_time - t > self._fingerprint_ttl
        ]
        for k in expired_keys:
            del self._content_fingerprints[k]

        # 检查是否存在
        if fingerprint in self._content_fingerprints:
            return True

        # 记录新指纹
        self._content_fingerprints[fingerprint] = current_time

        # 限制缓存大小
        if len(self._content_fingerprints) > self._max_fingerprints:
            sorted_items = sorted(
                self._content_fingerprints.items(),
                key=lambda x: x[1]
            )
            for k, _ in sorted_items[:self._max_fingerprints // 2]:
                del self._content_fingerprints[k]

        return False

    def cleanup(self):
        """清理资源"""
        self.logger.info("正在清理 HermesBridge 资源...")

        # 停止标志
        self._stop_event.set()
        self._runtime_link.close()

        with self._pending_responses_lock:
            for pending in self._pending_responses.values():
                timer = pending.get("timer")
                if timer:
                    timer.cancel()
            self._pending_responses.clear()

        # 停止 HTTP 服务器
        if self._server_loop and self._runner:
            asyncio.run_coroutine_threadsafe(
                self._stop_server(),
                self._server_loop
            ).result(timeout=5)

        # 停止事件循环
        if self._server_loop:
            self._server_loop.call_soon_threadsafe(self._server_loop.stop)

        # 等待线程结束
        if self._server_thread and self._server_thread.is_alive():
            self._server_thread.join(timeout=3)

        if self._message_processor_thread and self._message_processor_thread.is_alive():
            self._message_processor_thread.join(timeout=3)

        if self._runtime_upstream_thread.is_alive():
            self._runtime_upstream_thread.join(timeout=3)

        self.logger.info("HermesBridge 资源清理完成")


def main(args=None):
    rclpy.init(args=args)
    node = HermesBridge()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("检测到 KeyboardInterrupt，正在关闭...")
    finally:
        try:
            node.cleanup()
            node.destroy_node()
        except Exception as e:
            print(f"清理节点资源时发生错误: {e}")
        finally:
            rclpy.shutdown()


if __name__ == "__main__":
    main()
