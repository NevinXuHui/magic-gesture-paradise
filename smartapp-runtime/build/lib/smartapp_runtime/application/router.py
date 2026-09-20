import asyncio
import copy
import inspect
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple, Union

from smartapp_runtime.domain.commands import (
    CloudData,
    MessageTarget,
    _expect_json_object,
)
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest
from smartapp_runtime.domain.models import Session
from smartapp_runtime.ports.process import BackendEvent
from smartapp_runtime.ports.renderer import RendererPort


class BackendSender(Protocol):
    async def send(self, handle: Any, message: Dict[str, Any]) -> None:
        ...


AgentEventSink = Callable[
    [Dict[str, Any]], Union[bool, Awaitable[bool]]
]


@dataclass
class _DownstreamItem:
    targets: Tuple[str, ...]
    message: Dict[str, Any]
    size: int


@dataclass
class _UpstreamItem:
    event: Dict[str, Any]
    size: int


def _error(code: ErrorCode, message: str) -> SmartAppError:
    return SmartAppError(code, message)


class DeliveryFailure(Exception):
    """Marks a validated cloud command whose component delivery failed."""

    def __init__(self, error: SmartAppError) -> None:
        self.error = error
        super().__init__(error.message)


def _delivery_failure(error: Exception) -> DeliveryFailure:
    if isinstance(error, SmartAppError):
        return DeliveryFailure(error)
    return DeliveryFailure(
        _error(ErrorCode.INTERNAL_ERROR, "cloud data delivery failed")
    )


def _encoded_size(value: Dict[str, Any]) -> int:
    try:
        return len(json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise _error(ErrorCode.VALIDATION_ERROR, "message is not valid JSON") from None


class MessageRouter:
    """Serializes per-session downstream and upstream logical routing."""

    def __init__(
        self,
        renderer: RendererPort,
        backend_sender: BackendSender,
        agent_sink: AgentEventSink,
        max_queue_messages: int = 128,
        max_queue_bytes: int = 4194304,
    ) -> None:
        if (type(max_queue_messages) is not int or max_queue_messages <= 0
                or type(max_queue_bytes) is not int or max_queue_bytes <= 0):
            raise _error(ErrorCode.VALIDATION_ERROR, "router queue limits must be positive")
        self._renderer = renderer
        self._backend_sender = backend_sender
        self._agent_sink = agent_sink
        self._max_queue_messages = max_queue_messages
        self._max_queue_bytes = max_queue_bytes
        self._lock = asyncio.Lock()
        self._session: Optional[Session] = None
        self._manifest: Optional[Manifest] = None
        self._backend_handle: Any = None
        self._state = "ENDED"
        self._last_seq: Optional[int] = None
        self._downstream: List[_DownstreamItem] = []
        self._downstream_bytes = 0
        self._upstream: List[_UpstreamItem] = []
        self._upstream_bytes = 0

    async def begin_session(
        self, session: Session, manifest: Manifest, backend_handle: Any = None
    ) -> None:
        if not isinstance(session, Session) or not isinstance(manifest, Manifest):
            raise _error(ErrorCode.VALIDATION_ERROR, "invalid router session")
        if session.app_id != manifest.app_id or session.version != manifest.version:
            raise _error(ErrorCode.VALIDATION_ERROR, "session and manifest do not match")
        async with self._lock:
            if self._session is not None:
                if self._session == session and self._manifest == manifest:
                    return
                raise _error(ErrorCode.SESSION_CONFLICT, "another router session is active")
            self._session = session
            self._manifest = manifest
            self._backend_handle = backend_handle
            self._state = "STARTING"
            self._last_seq = None
            self._downstream = []
            self._downstream_bytes = 0
            self._upstream = []
            self._upstream_bytes = 0
            token = session

            async def handle_renderer_message(message, session_token=token):
                await self._handle_renderer_message(session_token, message)

            self._renderer.set_message_handler(handle_renderer_message)

    async def bind_backend(self, session_id: str, backend_handle: Any) -> None:
        async with self._lock:
            self._require_session(session_id)
            if self._state != "STARTING" or backend_handle is None:
                raise _error(ErrorCode.SESSION_MISMATCH, "backend cannot be bound")
            self._backend_handle = backend_handle

    async def mark_running(self, session_id: str) -> None:
        async with self._lock:
            self._require_session(session_id)
            if self._state not in ("STARTING", "RUNNING"):
                raise _error(ErrorCode.SESSION_MISMATCH, "router session is not starting")
            if self._manifest.backend.enabled and self._backend_handle is None:
                raise _error(ErrorCode.SESSION_MISMATCH, "backend is not bound")
            self._state = "RUNNING"
            await self._flush_downstream_locked()

    async def route_cloud_data(self, command: CloudData) -> None:
        if not isinstance(command, CloudData):
            raise _error(ErrorCode.VALIDATION_ERROR, "cloud data is invalid")
        async with self._lock:
            self._require_session(command.session_id)
            if self._state not in ("STARTING", "RUNNING"):
                raise _error(ErrorCode.SESSION_MISMATCH, "router session is not accepting data")
            if (type(command.seq) is not int or not 0 <= command.seq < 2 ** 63
                    or (command.data_type is not None
                        and (type(command.data_type) is not str or not command.data_type))):
                raise _error(ErrorCode.VALIDATION_ERROR, "cloud data is invalid")
            if self._last_seq is not None and command.seq <= self._last_seq:
                raise _error(ErrorCode.SEQ_OUT_OF_ORDER, "cloud data sequence is out of order")
            targets = self._resolve_targets(command.target)
            message = {
                "event": "cloud_data",
                "seq": command.seq,
                "data": _expect_json_object(command.data, "data"),
            }
            if command.data_type is not None:
                message["dataType"] = command.data_type
            if self._state == "STARTING":
                wrapper = {"targets": list(targets), "message": message}
                size = _encoded_size(wrapper)
                if (len(self._downstream) >= self._max_queue_messages
                        or self._downstream_bytes + size > self._max_queue_bytes):
                    raise _error(ErrorCode.QUEUE_FULL, "downstream queue is full")
                self._downstream.append(_DownstreamItem(targets, copy.deepcopy(message), size))
                self._downstream_bytes += size
                self._last_seq = command.seq
                return
            try:
                await self._flush_downstream_locked()
                await self._deliver(targets, message)
            except Exception as error:
                raise _delivery_failure(error) from None
            self._last_seq = command.seq

    async def accept_app_data(self, source: str, message: Any) -> None:
        async with self._lock:
            await self._accept_app_data_locked(source, message)

    async def flush_upstream(self) -> bool:
        async with self._lock:
            return await self._flush_upstream_locked()

    async def end_session(self, session_id: str) -> None:
        async with self._lock:
            if self._session is None or self._session.session_id != session_id:
                return
            self._state = "ENDED"
            self._session = None
            self._manifest = None
            self._backend_handle = None
            self._last_seq = None
            self._downstream = []
            self._downstream_bytes = 0
            self._upstream = []
            self._upstream_bytes = 0
            self._renderer.set_message_handler(None)

    async def _handle_renderer_message(
        self, session_token: Session, message: Dict[str, Any]
    ) -> None:
        async with self._lock:
            if self._session != session_token:
                raise _error(ErrorCode.SESSION_MISMATCH, "renderer session does not match")
            await self._accept_app_data_locked("web", message)

    async def _accept_app_data_locked(self, source: str, message: Any) -> None:
        if self._session is None or self._manifest is None or self._state != "RUNNING":
            raise _error(ErrorCode.SESSION_MISMATCH, "router session is not running")
        data_type, data = self._normalize_app_data(source, message)
        event = {
            "event": "app_data",
            "sessionId": self._session.session_id,
            "appId": self._session.app_id,
            "dataType": data_type,
            "data": data,
        }
        if self._upstream:
            await self._flush_upstream_locked()
        if self._upstream:
            self._enqueue_upstream(event)
            return
        if not await self._emit_upstream(event):
            self._enqueue_upstream(event)

    def _require_session(self, session_id: str) -> None:
        if self._session is None or self._session.session_id != session_id:
            raise _error(ErrorCode.SESSION_MISMATCH, "router session does not match")

    def _resolve_targets(self, target: MessageTarget) -> Tuple[str, ...]:
        if not isinstance(target, MessageTarget):
            raise _error(ErrorCode.VALIDATION_ERROR, "cloud data target is invalid")
        manifest = self._manifest
        resolved = manifest.default_target if target == MessageTarget.AUTO else target
        if resolved == MessageTarget.PYTHON:
            if not manifest.backend.enabled:
                raise _error(ErrorCode.VALIDATION_ERROR, "Python target is disabled")
            return ("python",)
        if resolved == MessageTarget.WEB:
            if not manifest.web.enabled:
                raise _error(ErrorCode.VALIDATION_ERROR, "web target is disabled")
            return ("web",)
        if resolved == MessageTarget.BROADCAST:
            targets = []
            if manifest.backend.enabled:
                targets.append("python")
            if manifest.web.enabled:
                targets.append("web")
            if not targets:
                raise _error(ErrorCode.VALIDATION_ERROR, "broadcast has no enabled target")
            return tuple(targets)
        raise _error(ErrorCode.VALIDATION_ERROR, "cloud data target is invalid")

    async def _deliver(self, targets: Tuple[str, ...], message: Dict[str, Any]) -> None:
        for target in targets:
            payload = copy.deepcopy(message)
            if target == "python":
                if self._backend_handle is None:
                    raise _error(ErrorCode.SESSION_MISMATCH, "backend is not bound")
                await self._backend_sender.send(self._backend_handle, payload)
            else:
                await self._renderer.send(payload)

    async def _flush_downstream_locked(self) -> None:
        while self._downstream:
            item = self._downstream[0]
            await self._deliver(item.targets, item.message)
            self._downstream.pop(0)
            self._downstream_bytes -= item.size

    def _normalize_app_data(self, source: str, message: Any) -> Tuple[str, Dict[str, Any]]:
        if source == "python":
            if not self._manifest.backend.enabled:
                raise _error(ErrorCode.VALIDATION_ERROR, "Python source is disabled")
            if not isinstance(message, BackendEvent):
                raise _error(ErrorCode.VALIDATION_ERROR, "Python app data is invalid")
            event = message.event
            data_type = message.data_type
            data = message.data
        elif source == "web":
            if not self._manifest.web.enabled:
                raise _error(ErrorCode.VALIDATION_ERROR, "web source is disabled")
            if type(message) is not dict or set(message) != {"event", "dataType", "data"}:
                raise _error(ErrorCode.VALIDATION_ERROR, "web app data is invalid")
            event = message["event"]
            data_type = message["dataType"]
            data = message["data"]
        else:
            raise _error(ErrorCode.VALIDATION_ERROR, "app data source is invalid")
        if event != "app_data" or type(data_type) is not str or not data_type:
            raise _error(ErrorCode.VALIDATION_ERROR, "app data is invalid")
        return data_type, _expect_json_object(data, "data")

    def _enqueue_upstream(self, event: Dict[str, Any]) -> None:
        size = _encoded_size(event)
        if (len(self._upstream) >= self._max_queue_messages
                or self._upstream_bytes + size > self._max_queue_bytes):
            raise _error(ErrorCode.UPSTREAM_QUEUE_FULL, "upstream queue is full")
        self._upstream.append(_UpstreamItem(copy.deepcopy(event), size))
        self._upstream_bytes += size

    async def _emit_upstream(self, event: Dict[str, Any]) -> bool:
        try:
            accepted = self._agent_sink(copy.deepcopy(event))
            if inspect.isawaitable(accepted):
                accepted = await accepted
        except Exception:
            raise _error(ErrorCode.INTERNAL_ERROR, "Agent event sink failed") from None
        if type(accepted) is not bool:
            raise _error(ErrorCode.INTERNAL_ERROR, "Agent event sink returned an invalid result")
        return accepted

    async def _flush_upstream_locked(self) -> bool:
        while self._upstream:
            item = self._upstream[0]
            if not await self._emit_upstream(item.event):
                return False
            self._upstream.pop(0)
            self._upstream_bytes -= item.size
        return True
