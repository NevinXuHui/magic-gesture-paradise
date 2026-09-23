import time
import json
import os
import configparser
import subprocess
import threading
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

import rclpy
from rclpy.node import Node

try:
    import yaml
except ImportError:  # pragma: no cover - 运行环境缺 PyYAML 时回退 CLI
    yaml = None

from homi_speech_interface.srv import SIGCData, AssistantQuiet
from homi_speech_interface.msg import SIGCEvent

try:
    from .logging_setup import setup_logging
except ImportError:
    from claw_client.logging_setup import setup_logging


class SpeechCoreClientNode(Node):
    """
    用户与 SpeechCore 数据发送/接收接口节点

    发送: 调用 send(data) 方法，将数据通过 SIGCData 服务发送给 SpeechCore
    接收: 重写 on_speech_event(event_json) 处理来自 SpeechCore 的平台事件

    参考: network_node.py (InternetStartNode) 模式实现
    """

    def __init__(self, node_name: str = 'claw_client_node'):
        super().__init__(node_name)

        # === 参数声明 ===
        self.declare_parameter('console_log_level', 'INFO')
        self.declare_parameter('file_log_level', 'INFO')
        self.declare_parameter('send_timeout_sec', 5)
        self.declare_parameter('assistant_startup_mode', -1)  # -1=不设置, 0=关闭, 1=正常, 2=仅ASR

        console_level = self.get_parameter('console_log_level').value.upper()
        file_level = self.get_parameter('file_log_level').value.upper()

        self.logger, self.file_logging_enabled, self.log_file_path = setup_logging(
            'claw_client_node', console_level, file_level
        )
        self.logger.info("初始化 SpeechCoreClientNode 节点")

        self.send_timeout_sec = self.get_parameter('send_timeout_sec').value

        # === 读取设备 ID ===
        self.device_id = self._read_device_id_from_config()

        # === 发送侧：调用 SpeechCore 的 SIGCData 服务 ===
        self.platform_client = self.create_client(
            SIGCData, '/homi_speech/sigc_data_service'
        )
        self.logger.info("创建客户端: /homi_speech/sigc_data_service")

        # === 接收侧：订阅 SpeechCore 下发的平台事件 ===
        self.sigc_event_sub = self.create_subscription(
            SIGCEvent,
            '/homi_speech/sigc_event_topic',
            self._on_sigc_event,
            10
        )
        self.logger.info("订阅话题: /homi_speech/sigc_event_topic")

        # === 鉴权信息缓存 ===
        self._credential_cache: Dict[str, Dict[str, Any]] = {}
        self._credential_pending: Dict[str, Any] = {}  # 等待响应的请求
        self._credential_refresh_timers: Dict[str, Any] = {}  # 自动刷新定时器
        self._credential_refresh_advance_sec = 300  # 提前5分钟刷新（可配置）
        # 鉴权获取异常重试：覆盖发送失败、响应超时、空/非法凭证
        self._credential_response_timeout_sec = 10.0
        self._credential_retry_interval_sec = 2.0
        self._credential_retry_max = 10
        self._credential_retry_state: Dict[str, Dict[str, Any]] = {}

        # === Hermes 配置参数 ===
        # 优先按 name 匹配 custom_providers（默认 openclaw）；
        # index 仅作 name 未命中时的回退（0 起，-1 表示不回退）。
        self.declare_parameter('hermes_provider_name', 'openclaw')
        self.declare_parameter('hermes_custom_provider_index', 0)
        self.declare_parameter('hermes_config_timeout_sec', 30)
        self.declare_parameter('hermes_config_async', True)  # 异步写配置，避免阻塞 ROS 回调
        self._hermes_provider_name = self.get_parameter('hermes_provider_name').value
        self._hermes_provider_index = int(self.get_parameter('hermes_custom_provider_index').value)
        self._hermes_config_timeout_sec = float(self.get_parameter('hermes_config_timeout_sec').value)
        self._hermes_config_async = bool(self.get_parameter('hermes_config_async').value)
        self._hermes_config_lock = threading.Lock()

        # === 语音助手模式设置 ===
        self._assistant_mode_client = None
        self._assistant_startup_mode = self.get_parameter('assistant_startup_mode').value
        if self._assistant_startup_mode >= 0:
            self._setup_assistant_mode()

        self.logger.info("SpeechCoreClientNode 初始化完成")

    # ──────────────────────────────────────────────────────────────────
    # 设备信息读取
    # ──────────────────────────────────────────────────────────────────

    def _read_device_id_from_config(self) -> str:
        """从配置文件读取设备 ID"""
        config_path = "/etc/cmcc_robot/cmcc_dev.ini"
        try:
            config = configparser.ConfigParser()
            if not os.path.exists(config_path):
                raise FileNotFoundError(f"配置文件不存在: {config_path}")

            config.read(config_path)
            device_id = config.get("factory", "devSn")
            self.logger.info(f"成功读取设备 ID: {device_id}")
            return device_id
        except Exception as e:
            self.logger.error(f"设备 ID 配置读取失败: {str(e)}")
            # 返回默认值而不是终止，避免开发环境无法启动
            return "unknown"

    # ──────────────────────────────────────────────────────────────────
    # 发送接口
    # ──────────────────────────────────────────────────────────────────

    def send(self, data: str) -> int:
        """
        向 SpeechCore 发送数据（同步阻塞）。

        注意：内部使用 spin_until_future_complete，不可在已 spin 的
        executor 回调/定时器中调用，否则会嵌套 spin 导致整节点卡死。
        回调场景请用 send_async()。

        Args:
            data: 要发送的 JSON 字符串
        Returns:
            error_code: 0 成功，负数失败（-1 服务未就绪，-2 超时，-3 异常）
        """
        self.logger.info("=" * 80)
        self.logger.info("📤 [发送数据到 SpeechCore]")
        self.logger.info(f"完整数据: {data}")

        # 尝试解析并格式化打印 JSON
        try:
            parsed = json.loads(data)
            self.logger.info(f"格式化数据:\n{json.dumps(parsed, ensure_ascii=False, indent=2)}")
        except Exception:
            pass
        self.logger.info("=" * 80)

        if not self.platform_client.service_is_ready():
            self.logger.warning("sigc_data_service 未就绪")
            return -1

        req = SIGCData.Request()
        req.data = data

        try:
            future = self.platform_client.call_async(req)
            rclpy.spin_until_future_complete(self, future, timeout_sec=float(self.send_timeout_sec))
            if not future.done():
                self.logger.error("调用 sigc_data_service 超时")
                return -2

            result = future.result()
            self.logger.info(f"✅ 发送完成, error_code={result.error_code}")
            return result.error_code
        except Exception as e:
            self.logger.error(f"调用 sigc_data_service 异常: {e}")
            return -3

    def send_async(self, data: str, callback=None) -> bool:
        """
        向 SpeechCore 非阻塞发送数据（可在 executor 回调/定时器中安全调用）。

        使用 call_async + done_callback + 超时 timer，不调用
        spin_until_future_complete，避免嵌套 spin 阻塞整个节点。

        Args:
            data: 要发送的 JSON 字符串
            callback: 可选 callback(error_code: int)
                      0 成功；-1 服务未就绪；-2 超时；-3 异常；其它为服务返回码
                      可能在当前线程同步调用（服务未就绪），也可能在之后异步调用

        Returns:
            bool: True=请求已发出（结果经 callback）；False=立即失败（未就绪等）
        """
        self.logger.info("=" * 80)
        self.logger.info("📤 [异步发送数据到 SpeechCore]")
        self.logger.info(f"完整数据: {data}")
        try:
            parsed = json.loads(data)
            self.logger.info(f"格式化数据:\n{json.dumps(parsed, ensure_ascii=False, indent=2)}")
        except Exception:
            pass
        self.logger.info("=" * 80)

        if not self.platform_client.service_is_ready():
            self.logger.warning("sigc_data_service 未就绪")
            if callback is not None:
                try:
                    callback(-1)
                except Exception as e:
                    self.logger.error(f"send_async callback 异常: {e}")
            return False

        req = SIGCData.Request()
        req.data = data

        state: Dict[str, Any] = {"finished": False, "timer": None}

        def _finish(code: int) -> None:
            if state["finished"]:
                return
            state["finished"] = True
            timer = state.get("timer")
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
                state["timer"] = None
                # try:
                #     self.destroy_timer(timer)
                # except Exception:
                #     pass
            if callback is not None:
                try:
                    callback(int(code))
                except Exception as e:
                    self.logger.error(f"send_async callback 异常: {e}")

        def _on_future_done(fut) -> None:
            try:
                result = fut.result()
                code = int(getattr(result, "error_code", -3))
                self.logger.info(f"✅ 异步发送完成, error_code={code}")
            except Exception as e:
                self.logger.error(f"send_async future 异常: {e}")
                code = -3
            _finish(code)

        def _on_timeout() -> None:
            # create_timer 为周期定时器，先取消
            timer = state.get("timer")
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
            if state["finished"]:
                return
            self.logger.error("调用 sigc_data_service 超时 (async)")
            _finish(-2)

        try:
            future = self.platform_client.call_async(req)
            future.add_done_callback(_on_future_done)
            timeout = float(self.send_timeout_sec or 5)
            state["timer"] = self.create_timer(timeout, _on_timeout)
            return True
        except Exception as e:
            self.logger.error(f"send_async 异常: {e}")
            _finish(-3)
            return False

    # ──────────────────────────────────────────────────────────────────
    # 接收接口
    # ──────────────────────────────────────────────────────────────────

    def _on_sigc_event(self, msg):
        """SpeechCore 事件订阅回调，先处理 credential_get，再转交给 on_speech_event 处理"""
        try:
            self.logger.info("=" * 80)
            self.logger.info("📥 [收到 SpeechCore 事件]")
            self.logger.info(f"完整数据: {msg.event}")

            # 尝试解析并格式化打印 JSON
            try:
                parsed = json.loads(msg.event)
                self.logger.info(f"格式化数据:\n{json.dumps(parsed, ensure_ascii=False, indent=2)}")
            except:
                pass
            self.logger.info("=" * 80)

            # 尝试解析并处理 credential_get 响应
            try:
                event_data = json.loads(msg.event)
                event = event_data.get("event", "")
                event_id = event_data.get("eventId", "")

                if event == "credential_get" and event_id in self._credential_pending:
                    self._handle_credential_response(event_data)
                    return  # credential_get 已处理，不再传递给子类
            except Exception as e:
                self.logger.debug(f"解析 credential_get 事件异常（可能非 JSON）: {e}")

            # 其他事件交给子类处理
            self.on_speech_event(msg.event)
        except Exception as e:
            self.logger.error(f"处理 SpeechCore 事件异常: {e}")

    def on_speech_event(self, event_json: str):
        """
        收到来自 SpeechCore 的平台事件回调，子类或外部逻辑在此处理。

        Args:
            event_json: SpeechCore 下发的事件 JSON 字符串
        """
        pass

    # ──────────────────────────────────────────────────────────────────
    # 服务鉴权信息获取
    # ──────────────────────────────────────────────────────────────────

    def request_service_credential_async(
        self,
        service: str,
        callback=None,
        *,
        force: bool = False,
        enable_retry: bool = True,
        _from_retry: bool = False,
    ) -> bool:
        """
        异步请求服务鉴权信息（非阻塞，立即返回）

        Args:
            service: 服务名称（如 "openclaw"）
            callback: 可选的回调函数，签名为 callback(credential: Dict[str, Any])
                     当响应到达时会调用此回调
            force: True 时跳过缓存，强制向平台重新请求（重试/刷新场景）
            enable_retry: True 时对发送失败、响应超时、空凭证自动重试
            _from_retry: 内部标记，重试回调发起时为 True（不重置重试计数）

        Returns:
            bool: True=请求发送成功或命中有效缓存, False=发送失败（若 enable_retry 会继续后台重试）
        """
        # 外部新发起的请求：重置重试计数，允许新一轮获取异常重试
        if enable_retry and not _from_retry:
            state = self._credential_retry_state.get(service)
            if state is not None and state.get("retry_timer") is None:
                state["count"] = 0

        # 检查缓存是否有效
        cache_key = f"{service}:{self.device_id}"
        if not force and cache_key in self._credential_cache:
            cached = self._credential_cache[cache_key]
            expire_at = cached.get("expireAt", 0)
            if expire_at > int(time.time() * 1000) and self._is_credential_valid(cached):
                self.logger.info(f"使用缓存的鉴权信息: service={service}, device_id={self.device_id}")
                self._clear_credential_retry(service)
                if callback:
                    callback(cached)
                return True

        # 生成唯一 event_id
        event_id = f"cred_{service}_{int(time.time() * 1000)}"
        seq = str(int(time.time() * 1000))

        # 构造请求
        request = {
            "deviceId": self.device_id,
            "domain": "SYSTEM",
            "event": "credential_get",
            "eventId": event_id,
            "seq": seq,
            "body": {
                "service": service
            }
        }

        # 记录等待响应（不阻塞）
        self._credential_pending[event_id] = {
            "service": service,
            "device_id": self.device_id,
            "cache_key": cache_key,
            "callback": callback,  # 保存回调函数
            "enable_retry": enable_retry,
            "created_at": time.time(),
        }

        # 发送请求（真正非阻塞：不用 spin_until_future_complete）
        request_json = json.dumps(request, ensure_ascii=False)
        self.logger.info(
            f"异步请求服务鉴权信息: service={service}, device_id={self.device_id}, event_id={event_id}"
        )

        def _on_send_done(error_code: int) -> None:
            if error_code != 0:
                self.logger.error(f"发送 credential_get 请求失败: error_code={error_code}")
                # 可能已收到响应并清掉 pending；仅当仍是本次请求时清理
                pending_now = self._credential_pending.get(event_id)
                if pending_now is not None:
                    self._credential_pending.pop(event_id, None)
                if enable_retry:
                    self._schedule_credential_retry(
                        service,
                        callback=callback,
                        reason=f"发送失败 error_code={error_code}",
                    )
                return

            self.logger.info("credential_get 请求已发送，等待异步响应...")
            # 发送成功后再武装响应超时（平台 topic 回包）
            if enable_retry and event_id in self._credential_pending:
                self._arm_credential_response_timeout(event_id, service, callback)

        issued = self.send_async(request_json, callback=_on_send_done)
        if not issued:
            # 服务未就绪：callback 已同步触发重试调度
            return False
        return True

    def get_service_credential(self, service: str, timeout_sec: float = 5.0) -> Optional[Dict[str, Any]]:
        """
        获取服务鉴权信息（同步接口，阻塞等待响应）

        Args:
            service: 服务名称（如 "openclaw"）
            timeout_sec: 等待响应超时时间（秒）

        Returns:
            鉴权信息字典，格式：
            {
                "service": "openclaw",
                "credentialType": "api_key",
                "credential": "xxxxxxxxx",
                "expireAt": 1749703600000,
                "endpoint": "https://llm.xxx.com/v1",
                "extra": {}
            }
            失败返回 None
        """
        # 检查缓存是否有效
        cache_key = f"{service}:{self.device_id}"
        if cache_key in self._credential_cache:
            cached = self._credential_cache[cache_key]
            expire_at = cached.get("expireAt", 0)
            if expire_at > int(time.time() * 1000):
                self.logger.info(f"使用缓存的鉴权信息: service={service}, device_id={self.device_id}")
                return cached

        # 生成唯一 event_id
        event_id = f"cred_{service}_{int(time.time() * 1000)}"
        seq = str(int(time.time() * 1000))

        # 构造请求
        request = {
            "deviceId": self.device_id,
            "domain": "SYSTEM",
            "event": "credential_get",
            "eventId": event_id,
            "seq": seq,
            "body": {
                "service": service
            }
        }

        # 记录等待响应
        import threading
        response_event = threading.Event()
        response_holder = {"data": None}
        self._credential_pending[event_id] = {
            "event": response_event,
            "holder": response_holder,
            "service": service,
            "device_id": self.device_id,
            "cache_key": cache_key
        }

        # 发送请求
        request_json = json.dumps(request, ensure_ascii=False)
        self.logger.info(f"请求服务鉴权信息: service={service}, device_id={self.device_id}, event_id={event_id}")

        ret = self.send(request_json)
        if ret != 0:
            self.logger.error(f"发送 credential_get 请求失败: error_code={ret}")
            del self._credential_pending[event_id]
            return None

        # 等待响应
        if response_event.wait(timeout=timeout_sec):
            result = response_holder["data"]
            del self._credential_pending[event_id]
            return result
        else:
            self.logger.error(f"等待 credential_get 响应超时: service={service}, device_id={self.device_id}")
            del self._credential_pending[event_id]
            return None

    def _handle_credential_response(self, event_data: Dict[str, Any]):
        """处理平台下发的 credential_get 响应"""
        event_id = event_data.get("eventId", "")
        body = event_data.get("body", {})

        if event_id not in self._credential_pending:
            self.logger.warning(f"收到未知的 credential_get 响应: event_id={event_id}")
            return

        pending = self._credential_pending.pop(event_id)
        self._cancel_pending_response_timeout(event_id)

        service = pending["service"]
        device_id = pending["device_id"]
        cache_key = pending["cache_key"]
        callback = pending.get("callback")
        enable_retry = pending.get("enable_retry", True)

        # 平台可能返回错误字段
        error_code = body.get("errorCode", body.get("error_code", 0))
        error_msg = body.get("errorMsg", body.get("error_msg", body.get("errmsg", "")))

        # 提取鉴权信息
        credential_info = {
            "service": body.get("service", service),
            "credentialType": body.get("credentialType", ""),
            "credential": body.get("credential", ""),
            "expireAt": body.get("expireAt", 0),
            "endpoint": body.get("endpoint", ""),
            "extra": body.get("extra", {})
        }

        # 打印完整的鉴权响应数据
        self.logger.info(f"收到服务鉴权响应: service={service}, device_id={device_id}")
        self.logger.info(f"完整鉴权数据: {json.dumps(credential_info, ensure_ascii=False, indent=2)}")

        # 获取异常：错误码 / 空凭证 / 缺少 endpoint
        if error_code not in (0, "0", None, ""):
            reason = f"平台返回错误 errorCode={error_code} errorMsg={error_msg}"
            self.logger.error(f"credential_get 获取异常: service={service}, {reason}")
            if enable_retry:
                self._schedule_credential_retry(service, callback=callback, reason=reason)
            return

        if not self._is_credential_valid(credential_info):
            reason = "凭证内容无效（credential/endpoint 为空）"
            self.logger.error(
                f"credential_get 获取异常: service={service}, {reason}, "
                f"data={json.dumps(credential_info, ensure_ascii=False)}"
            )
            if enable_retry:
                self._schedule_credential_retry(service, callback=callback, reason=reason)
            return

        # 更新缓存
        self._credential_cache[cache_key] = credential_info
        self._clear_credential_retry(service)

        # 如果是 openclaw 服务，更新 Hermes 配置（失败仅告警，不走鉴权重试，避免无 hermes 时死循环）
        if service == "openclaw":
            self._update_hermes_config(credential_info)

        # 设置自动刷新定时器
        self._schedule_credential_refresh(service, credential_info)

        # 如果有同步等待线程，通知它
        if "holder" in pending and "event" in pending:
            pending["holder"]["data"] = credential_info
            pending["event"].set()

        # 如果有异步回调，执行它
        if callback:
            try:
                callback(credential_info)
            except Exception as e:
                self.logger.error(f"执行鉴权响应回调异常: {e}")

    def _is_credential_valid(self, credential_info: Dict[str, Any]) -> bool:
        """校验鉴权信息是否可用"""
        if not isinstance(credential_info, dict):
            return False
        credential = str(credential_info.get("credential", "") or "").strip()
        endpoint = str(credential_info.get("endpoint", "") or "").strip()
        return bool(credential and endpoint)

    def _arm_credential_response_timeout(self, event_id: str, service: str, callback=None):
        """为已发出的 credential_get 请求设置响应超时，超时后自动重试"""
        # 取消同服务上一次未完成的超时定时器
        state = self._credential_retry_state.setdefault(service, {
            "count": 0,
            "callback": callback,
            "retry_timer": None,
            "response_timers": {},
        })
        state["callback"] = callback

        old_timer = state.get("response_timers", {}).pop(event_id, None)
        if old_timer is not None:
            try:
                old_timer.cancel()
            except Exception:
                pass

        timer = self.create_timer(
            float(self._credential_response_timeout_sec),
            lambda eid=event_id, svc=service: self._on_credential_response_timeout(eid, svc),
        )
        state.setdefault("response_timers", {})[event_id] = timer
        self.logger.info(
            f"已设置 credential_get 响应超时: service={service}, event_id={event_id}, "
            f"timeout={self._credential_response_timeout_sec}s"
        )

    def _cancel_pending_response_timeout(self, event_id: str):
        """取消指定 event_id 的响应超时定时器"""
        for service, state in list(self._credential_retry_state.items()):
            timers = state.get("response_timers") or {}
            timer = timers.pop(event_id, None)
            if timer is not None:
                try:
                    timer.cancel()
                except Exception:
                    pass
                self.logger.debug(f"取消 credential 响应超时定时器: service={service}, event_id={event_id}")

    def _on_credential_response_timeout(self, event_id: str, service: str):
        """credential_get 响应超时回调（create_timer 为周期定时器，需先 cancel）"""
        # 先取消自身，避免周期触发
        self._cancel_pending_response_timeout(event_id)

        pending = self._credential_pending.pop(event_id, None)
        if pending is None:
            # 响应已处理，忽略迟到的超时回调
            return

        callback = pending.get("callback")
        enable_retry = pending.get("enable_retry", True)
        reason = (
            f"等待响应超时 ({self._credential_response_timeout_sec}s), "
            f"event_id={event_id}"
        )
        self.logger.error(f"credential_get 获取异常: service={service}, {reason}")

        if enable_retry:
            self._schedule_credential_retry(service, callback=callback, reason=reason)

    def _schedule_credential_retry(self, service: str, callback=None, reason: str = ""):
        """调度鉴权获取重试（发送失败 / 响应超时 / 空凭证）"""
        state = self._credential_retry_state.setdefault(service, {
            "count": 0,
            "callback": callback,
            "retry_timer": None,
            "response_timers": {},
        })
        if callback is not None:
            state["callback"] = callback

        # 已有重试定时器在排队，避免并发重复请求
        if state.get("retry_timer") is not None:
            self.logger.info(
                f"credential 重试已在排队中，跳过重复调度: service={service}, reason={reason}"
            )
            return

        count = int(state.get("count", 0))
        if count >= self._credential_retry_max:
            self.logger.warning(
                f"credential_get 重试达到最大次数 ({self._credential_retry_max})，停止重试: "
                f"service={service}, reason={reason}"
            )
            return

        state["count"] = count + 1
        attempt = state["count"]
        interval = float(self._credential_retry_interval_sec)

        self.logger.warning(
            f"credential_get 获取异常，将在 {interval:.1f}s 后重试 "
            f"({attempt}/{self._credential_retry_max}): service={service}, reason={reason}"
        )

        timer = self.create_timer(
            interval,
            lambda svc=service: self._retry_credential_request(svc),
        )
        state["retry_timer"] = timer

    def _retry_credential_request(self, service: str):
        """执行一次鉴权重试（create_timer 为周期定时器，入口先 cancel）"""
        state = self._credential_retry_state.get(service) or {}
        timer = state.get("retry_timer")
        if timer is not None:
            try:
                timer.cancel()
            except Exception:
                pass
            state["retry_timer"] = None

        callback = state.get("callback")
        attempt = int(state.get("count", 0))
        self.logger.info(
            f"重试 credential_get ({attempt}/{self._credential_retry_max}): service={service}"
        )

        # force=True 跳过可能存在的半残缓存；失败时 request 内部会再次 schedule
        success = self.request_service_credential_async(
            service,
            callback=callback,
            force=True,
            enable_retry=True,
            _from_retry=True,
        )
        if success:
            self.logger.info(f"credential_get 重试请求已发送: service={service}")

    def _clear_credential_retry(self, service: str):
        """鉴权成功后清理重试/超时状态"""
        state = self._credential_retry_state.pop(service, None)
        if not state:
            return

        retry_timer = state.get("retry_timer")
        if retry_timer is not None:
            try:
                retry_timer.cancel()
            except Exception:
                pass

        for event_id, timer in list((state.get("response_timers") or {}).items()):
            try:
                timer.cancel()
            except Exception:
                pass

        self.logger.info(f"已清理 credential 重试状态: service={service}")

    def _hermes_config_path(self) -> Path:
        """解析 Hermes config.yaml 路径。"""
        hermes_home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
        return Path(hermes_home) / "config.yaml"

    def _resolve_hermes_provider(
        self, providers: List[Dict[str, Any]]
    ) -> Tuple[Optional[int], str]:
        """按 name 优先、index 回退解析 custom_providers 目标。

        Returns:
            (index, label) — index 为 None 表示未解析到
        """
        name = str(self._hermes_provider_name or "").strip()
        if name:
            for idx, item in enumerate(providers):
                if isinstance(item, dict) and str(item.get("name", "")).strip() == name:
                    return idx, f"name={name} index={idx}"

        idx = self._hermes_provider_index
        if isinstance(idx, int) and idx >= 0 and idx < len(providers):
            item = providers[idx] if isinstance(providers[idx], dict) else {}
            item_name = item.get("name", "")
            return idx, f"index={idx} name={item_name or '?'}"

        return None, f"name={name or '-'} index={idx}"

    def _update_hermes_config_yaml(self, api_key: str, base_url: str) -> bool:
        """直接写 ~/.hermes/config.yaml（避免 hermes CLI 冷启动超时）。"""
        if yaml is None:
            raise RuntimeError("PyYAML 不可用")

        config_path = self._hermes_config_path()
        if not config_path.exists():
            raise FileNotFoundError(f"Hermes 配置不存在: {config_path}")

        with self._hermes_config_lock:
            with open(config_path, "r", encoding="utf-8") as f:
                user_config = yaml.safe_load(f) or {}

            providers = user_config.get("custom_providers")
            if not isinstance(providers, list):
                providers = []
                user_config["custom_providers"] = providers

            index, label = self._resolve_hermes_provider(providers)
            if index is None:
                # name 未命中且无合法 index：按 name 新建条目
                name = str(self._hermes_provider_name or "openclaw").strip() or "openclaw"
                providers.append({
                    "name": name,
                    "base_url": base_url,
                    "api_key": api_key,
                })
                index = len(providers) - 1
                label = f"created name={name} index={index}"
                self.logger.warning(f"未找到已有 provider，新建: {label}")
            else:
                entry = providers[index]
                if not isinstance(entry, dict):
                    entry = {}
                    providers[index] = entry
                entry["base_url"] = base_url
                entry["api_key"] = api_key
                # 有直写 api_key 时去掉 key_env，避免被 env 覆盖
                entry.pop("key_env", None)

            tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(user_config, f, allow_unicode=True, sort_keys=False)
            os.replace(tmp_path, config_path)
            try:
                os.chmod(config_path, 0o600)
            except OSError:
                pass

        self.logger.info(f"✅ Hermes 配置已写入: {config_path} ({label})")
        return True

    def _run_hermes_config_set(self, key: str, value: str, hide_value: bool = False) -> bool:
        """调用 hermes config set；失败返回 False。"""
        cmd = ["hermes", "config", "set", "--force", key, value]
        shown = f"hermes config set --force {key} {'[HIDDEN]' if hide_value else value}"
        self.logger.info(f"执行命令: {shown}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._hermes_config_timeout_sec,
                stdin=subprocess.DEVNULL,
                env={
                    **os.environ,
                    # 与 /usr/local/bin/hermes wrapper 一致，避免 ROS PYTHONPATH 干扰
                    "HERMES_DISABLE_LAZY_INSTALLS": "1",
                },
            )
        except subprocess.TimeoutExpired:
            self.logger.error(
                f"❌ Hermes 配置更新超时 ({self._hermes_config_timeout_sec}s): {key}"
            )
            return False
        except FileNotFoundError:
            self.logger.error("❌ 未找到 hermes 命令，请确认 Hermes 已安装并在 PATH 中")
            return False

        if result.returncode == 0:
            self.logger.info(f"✅ 已设置 {key}")
            if result.stdout:
                self.logger.debug(f"命令输出: {result.stdout.strip()}")
            return True

        err = (result.stderr or result.stdout or "").strip()
        self.logger.error(f"❌ 设置 {key} 失败 (rc={result.returncode}): {err}")
        return False

    def _update_hermes_config_cli(self, api_key: str, base_url: str) -> bool:
        """回退：通过 hermes CLI 写入。"""
        providers: List[Dict[str, Any]] = []
        config_path = self._hermes_config_path()
        if yaml is not None and config_path.exists():
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                raw = cfg.get("custom_providers") or []
                if isinstance(raw, list):
                    providers = [p for p in raw if isinstance(p, dict)]
            except Exception as e:
                self.logger.warning(f"读取 Hermes 配置失败，将使用 index 回退: {e}")

        index, label = self._resolve_hermes_provider(providers)
        if index is None:
            index = max(0, int(self._hermes_provider_index))
            label = f"fallback index={index}"

        self.logger.info(f"CLI 更新目标: {label}")
        ok_key = self._run_hermes_config_set(
            f"custom_providers.{index}.api_key", api_key, hide_value=True
        )
        if not ok_key:
            return False
        return self._run_hermes_config_set(
            f"custom_providers.{index}.base_url", base_url, hide_value=False
        )

    def _update_hermes_config_sync(self, credential_info: Dict[str, Any]) -> bool:
        """同步更新 Hermes 配置（API Key + Base URL）。"""
        api_key = credential_info.get("credential", "")
        base_url = credential_info.get("endpoint", "")

        if not api_key or not base_url:
            self.logger.warning("凭证信息不完整，跳过 Hermes 配置更新")
            return False

        self.logger.info("=" * 80)
        self.logger.info("🔧 [更新 Hermes 配置]")
        self.logger.info(f"Provider name: {self._hermes_provider_name}")
        self.logger.info(f"Provider index fallback: {self._hermes_provider_index}")
        self.logger.info(f"Base URL: {base_url}")
        self.logger.info(
            f"API Key: {api_key[:20]}..." if len(api_key) > 20 else f"API Key: {api_key}"
        )
        self.logger.info("=" * 80)

        # 优先直写 config.yaml（快、不依赖 hermes 冷启动）
        try:
            if self._update_hermes_config_yaml(api_key, base_url):
                return True
        except Exception as e:
            self.logger.warning(f"直写 Hermes config.yaml 失败，回退 CLI: {e}")

        try:
            return self._update_hermes_config_cli(api_key, base_url)
        except Exception as e:
            self.logger.error(f"❌ Hermes 配置更新异常: {e}")
            return False

    def _update_hermes_config(self, credential_info: Dict[str, Any]) -> bool:
        """
        更新 Hermes 配置（API Key 和 Base URL）。

        默认异步执行，避免阻塞 ROS 回调线程；失败仅打日志，不影响鉴权缓存。

        Returns:
            bool: 同步模式下 True/False；异步模式下提交成功即 True
        """
        if not self._hermes_config_async:
            ok = self._update_hermes_config_sync(credential_info)
            if not ok:
                self.logger.warning(
                    "Hermes 配置更新失败，鉴权缓存已生效，请检查 hermes 命令/配置"
                )
            return ok

        def _worker():
            try:
                ok = self._update_hermes_config_sync(credential_info)
                if not ok:
                    self.logger.warning(
                        "Hermes 配置更新失败，鉴权缓存已生效，请检查 hermes 命令/配置"
                    )
            except Exception as e:
                self.logger.error(f"❌ Hermes 配置更新线程异常: {e}")

        threading.Thread(
            target=_worker,
            name="hermes-config-update",
            daemon=True,
        ).start()
        self.logger.info("已提交 Hermes 配置异步更新任务")
        return True

    def _schedule_credential_refresh(self, service: str, credential_info: Dict[str, Any]):
        """
        根据凭证过期时间，设置自动刷新定时器

        Args:
            service: 服务名称
            credential_info: 凭证信息（包含 expireAt 字段）
        """
        expire_at = credential_info.get("expireAt", 0)
        if expire_at <= 0:
            self.logger.warning(f"凭证无有效期，跳过自动刷新设置: service={service}")
            return

        # 计算刷新时间点（提前 N 秒刷新）
        current_time_ms = int(time.time() * 1000)
        expire_time_ms = expire_at
        refresh_time_ms = expire_time_ms - (self._credential_refresh_advance_sec * 1000)

        # 如果已经过了刷新时间，立即刷新
        if refresh_time_ms <= current_time_ms:
            self.logger.warning(f"凭证即将过期或已过期，立即刷新: service={service}")
            self.request_service_credential_async(service)
            return

        # 计算延迟时间（秒）
        delay_sec = (refresh_time_ms - current_time_ms) / 1000.0

        # 取消之前的定时器（如果存在）
        cache_key = f"{service}:{self.device_id}"
        if cache_key in self._credential_refresh_timers:
            old_timer = self._credential_refresh_timers[cache_key]
            old_timer.cancel()
            self.logger.debug(f"取消旧的刷新定时器: service={service}")

        # 创建新的定时器
        refresh_timer = self.create_timer(
            delay_sec,
            lambda: self._refresh_credential_callback(service)
        )
        self._credential_refresh_timers[cache_key] = refresh_timer

        # 格式化时间用于日志
        from datetime import datetime
        expire_datetime = datetime.fromtimestamp(expire_time_ms / 1000.0)
        refresh_datetime = datetime.fromtimestamp(refresh_time_ms / 1000.0)

        self.logger.info("=" * 80)
        self.logger.info("⏰ [设置凭证自动刷新定时器]")
        self.logger.info(f"服务: {service}")
        self.logger.info(f"过期时间: {expire_datetime.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info(f"刷新时间: {refresh_datetime.strftime('%Y-%m-%d %H:%M:%S')} (提前 {self._credential_refresh_advance_sec} 秒)")
        self.logger.info(f"延迟时间: {delay_sec:.1f} 秒 ({delay_sec/60:.1f} 分钟)")
        self.logger.info("=" * 80)

    def _refresh_credential_callback(self, service: str):
        """
        定时器触发的凭证刷新回调

        Args:
            service: 服务名称
        """
        self.logger.info("=" * 80)
        self.logger.info("🔄 [自动刷新凭证]")
        self.logger.info(f"服务: {service}")
        self.logger.info("=" * 80)

        # 异步请求新的凭证（响应到达后会自动更新缓存并设置下一次定时器）
        success = self.request_service_credential_async(service)

        if not success:
            self.logger.error(f"凭证自动刷新失败: service={service}")
            # 失败后延迟重试（30秒后）
            cache_key = f"{service}:{self.device_id}"
            retry_timer = self.create_timer(
                30.0,
                lambda: self._refresh_credential_callback(service)
            )
            self._credential_refresh_timers[cache_key] = retry_timer
            self.logger.info(f"将在 30 秒后重试刷新: service={service}")

    # ──────────────────────────────────────────────────────────────────
    # 语音助手模式设置
    # ──────────────────────────────────────────────────────────────────

    def _setup_assistant_mode(self):
        """设置语音助手启动模式（通过配置文件）"""
        mode = self._assistant_startup_mode

        mode_names = {
            0: "关闭（唤醒后不触发对话）",
            1: "正常模式（ASR+NLP+TTS）",
            2: "仅ASR模式"
        }
        mode_name = mode_names.get(mode, f"未知模式({mode})")

        self.logger.info(f"配置的语音助手启动模式: {mode_name}")

        # 创建服务客户端
        self._assistant_mode_client = self.create_client(
            AssistantQuiet,
            '/homi_speech/assistant_enable_service'
        )

        # 启动定时器，等待服务就绪后设置模式
        self._assistant_retry_count = 0
        self._assistant_retry_max = 10
        self._assistant_mode_timer = self.create_timer(1.0, self._try_set_assistant_mode)

    def _try_set_assistant_mode(self):
        """尝试设置语音助手模式（定时器回调）"""
        self._assistant_retry_count += 1

        if not self._assistant_mode_client.service_is_ready():
            if self._assistant_retry_count >= self._assistant_retry_max:
                self.logger.warning(
                    f'语音助手服务 /homi_speech/assistant_enable_service 在 {self._assistant_retry_max} 秒后仍未就绪，'
                    f'跳过模式设置'
                )
                self._assistant_mode_timer.cancel()
                return

            self.logger.debug(
                f'等待语音助手服务就绪... ({self._assistant_retry_count}/{self._assistant_retry_max})'
            )
            return

        # 服务就绪，取消定时器
        self._assistant_mode_timer.cancel()

        # 调用服务设置模式
        self._call_assistant_mode_service(self._assistant_startup_mode)

    def _call_assistant_mode_service(self, mode: int):
        """
        调用服务设置语音助手模式

        Args:
            mode: 0=关闭, 1=正常模式, 2=仅ASR模式
        """
        request = AssistantQuiet.Request()
        request.mode = mode

        mode_names = {
            0: "关闭（唤醒后不触发对话）",
            1: "正常模式（ASR+NLP+TTS）",
            2: "仅ASR模式"
        }
        mode_name = mode_names.get(mode, f"未知模式({mode})")

        self.logger.info("=" * 80)
        self.logger.info("🎤 [设置语音助手模式 - 发送请求]")
        self.logger.info(f'服务: /homi_speech/assistant_enable_service')
        self.logger.info(f'模式: {mode_name} (mode={mode})')
        self.logger.info(f'发送数据: {{mode: {mode}}}')
        self.logger.info("=" * 80)

        # 异步调用服务
        future = self._assistant_mode_client.call_async(request)
        future.add_done_callback(self._assistant_mode_callback)

    def _assistant_mode_callback(self, future):
        """语音助手模式设置服务调用回调"""
        try:
            response = future.result()
            self.logger.info("=" * 80)
            self.logger.info("🎤 [语音助手模式设置 - 接收响应]")
            self.logger.info(f'服务: /homi_speech/assistant_enable_service')
            self.logger.info(f'接收数据: {{error_code: {response.error_code}}}')

            if response.error_code == 0:
                self.logger.info('状态: ✅ 设置成功')
            else:
                self.logger.warning(f'状态: ⚠️ 设置失败')

            self.logger.info("=" * 80)
        except Exception as e:
            self.logger.error("=" * 80)
            self.logger.error("🎤 [语音助手模式设置 - 调用失败]")
            self.logger.error(f'服务: /homi_speech/assistant_enable_service')
            self.logger.error(f'异常信息: {e}')
            self.logger.error("=" * 80)


    def destroy_node(self):
        """清理节点资源，包括取消所有凭证刷新定时器"""
        # 取消所有凭证刷新定时器
        for cache_key, timer in self._credential_refresh_timers.items():
            try:
                timer.cancel()
                self.logger.debug(f"取消凭证刷新定时器: {cache_key}")
            except Exception as e:
                self.logger.error(f"取消定时器失败 {cache_key}: {e}")

        self._credential_refresh_timers.clear()
        self.logger.info("已清理所有凭证刷新定时器")

        # 取消鉴权重试/响应超时定时器
        for service in list(self._credential_retry_state.keys()):
            self._clear_credential_retry(service)

        # 调用父类的销毁方法
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = SpeechCoreClientNode()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        node.get_logger().info("检测到 KeyboardInterrupt，正在关闭节点...")
    finally:
        try:
            node.destroy_node()
        except Exception as e:
            print(f"清理节点资源时发生错误: {e}")
        finally:
            rclpy.shutdown()


if __name__ == "__main__":
    main()
