import asyncio
import copy
import inspect
from typing import Any, Dict, List, Optional, Tuple

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.ports.renderer import MessageHandler


def _renderer_error(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.RENDERER_FAILED, message)


class FakeRenderer:
    """Deterministic renderer for tests and hardware-free local runs."""

    def __init__(self, auto_ready: bool = False) -> None:
        if type(auto_ready) is not bool:
            raise TypeError("auto_ready must be a boolean")
        self._auto_ready = auto_ready
        self.load_calls: List[Tuple[str, Session]] = []
        self.sent_messages: List[Dict[str, Any]] = []
        self.stop_calls = 0
        self.restore_calls = 0
        self._handler: Optional[MessageHandler] = None
        self._ready = asyncio.Event()
        self._ready_error: Optional[SmartAppError] = None
        self._load_error: Optional[SmartAppError] = None
        self._send_error: Optional[SmartAppError] = None
        self._stop_error: Optional[SmartAppError] = None
        self._restore_error: Optional[SmartAppError] = None
        self._stopped = False
        self._restored = False

    def set_message_handler(self, handler: Optional[MessageHandler]) -> None:
        self._handler = handler

    def set_load_error(self, error: Optional[SmartAppError] = None) -> None:
        self._load_error = error or _renderer_error("renderer load failed")

    def set_send_error(self, error: Optional[SmartAppError] = None) -> None:
        self._send_error = error or _renderer_error("renderer send failed")

    def set_stop_error(self, error: Optional[SmartAppError] = None) -> None:
        self._stop_error = error or _renderer_error("renderer stop failed")

    def set_restore_error(self, error: Optional[SmartAppError] = None) -> None:
        self._restore_error = error or _renderer_error("renderer restore failed")

    def mark_ready(self) -> None:
        self._ready_error = None
        self._ready.set()

    def fail_ready(self, error: Optional[SmartAppError] = None) -> None:
        self._ready_error = error or _renderer_error("renderer readiness failed")
        self._ready.set()

    async def load(self, url: str, session: Session) -> None:
        if self._load_error is not None:
            raise self._load_error
        self.load_calls.append((url, session))
        self._ready = asyncio.Event()
        self._ready_error = None
        self._stopped = False
        self._restored = False
        if self._auto_ready:
            self._ready.set()

    async def wait_ready(self, timeout: float) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except (asyncio.TimeoutError, ValueError):
            raise _renderer_error("renderer readiness timed out") from None
        if self._ready_error is not None:
            raise self._ready_error

    async def send(self, message: Dict[str, Any]) -> None:
        if self._send_error is not None:
            raise self._send_error
        self.sent_messages.append(copy.deepcopy(message))

    async def stop(self) -> None:
        if self._stopped:
            return
        if self._stop_error is not None:
            raise self._stop_error
        self.stop_calls += 1
        self._stopped = True

    async def restore_default(self) -> None:
        if self._restored:
            return
        if self._restore_error is not None:
            raise self._restore_error
        self.restore_calls += 1
        self._restored = True

    async def emit_message(self, message: Dict[str, Any]) -> None:
        handler = self._handler
        if handler is None:
            raise _renderer_error("renderer message handler is not set")
        try:
            result = handler(copy.deepcopy(message))
            if inspect.isawaitable(result):
                await result
        except SmartAppError:
            raise
        except Exception:
            raise _renderer_error("renderer message callback failed") from None
