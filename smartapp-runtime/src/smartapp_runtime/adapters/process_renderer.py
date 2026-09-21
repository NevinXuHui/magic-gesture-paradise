import asyncio
import inspect
import json
import logging
import math
import os
import re
import signal
from typing import Any, Dict, Optional, Sequence, Tuple

from smartapp_runtime.config import RendererConfig
from smartapp_runtime.domain.commands import _expect_json_object
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.ports.renderer import MessageHandler


_PLACEHOLDERS = frozenset(("{url}", "{session_id}", "{app_id}", "{version}"))
_PLACEHOLDER_PATTERN = re.compile(r"\{[^{}]+\}")


def _renderer_error(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.RENDERER_FAILED, message)


def _validation_error(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.VALIDATION_ERROR, message)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


class ProcessRenderer:
    """Owns one persistent renderer process using a strict JSONL bridge."""

    def __init__(
        self,
        config: RendererConfig,
        max_message_bytes: int = 1048576,
        command_timeout: float = 15.0,
    ) -> None:
        if not isinstance(config, RendererConfig) or config.kind != "process":
            raise _validation_error("renderer config is invalid")
        if type(max_message_bytes) is not int or max_message_bytes <= 0:
            raise _validation_error("renderer message limit must be positive")
        if (type(command_timeout) not in (int, float)
                or not math.isfinite(command_timeout) or command_timeout <= 0):
            raise _validation_error("renderer command timeout must be finite and positive")
        self._validate_template(config.process_argv, placeholders=True)
        self._validate_template(config.restore_argv, placeholders=False)
        self._process_argv = tuple(config.process_argv)
        self._restore_argv = tuple(config.restore_argv)
        self._max_message_bytes = max_message_bytes
        self._command_timeout = float(command_timeout)
        self._handler: Optional[MessageHandler] = None
        self._process = None
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._ready = None
        self._failure: Optional[SmartAppError] = None
        self._write_lock = asyncio.Lock()
        self._stopping = False
        self._restored = False
        self._use_process_groups = os.name == "posix"

    @staticmethod
    def _validate_template(template: Sequence[str], placeholders: bool) -> None:
        if not isinstance(template, tuple) or not template:
            raise _validation_error("renderer process argv must be a non-empty tuple")
        for item in template:
            if type(item) is not str or not item:
                raise _validation_error("renderer process argv must contain strings")
            matches = _PLACEHOLDER_PATTERN.findall(item)
            if matches and (
                not placeholders or len(matches) != 1 or item not in _PLACEHOLDERS
            ):
                raise _validation_error(
                    "renderer placeholder must occupy a whole argv value"
                )

    def set_message_handler(self, handler: Optional[MessageHandler]) -> None:
        self._handler = handler

    @staticmethod
    def _substitute(template: Tuple[str, ...], values: Dict[str, str]) -> Tuple[str, ...]:
        return tuple(values.get(item, item) for item in template)

    def _encode(self, message: Dict[str, Any]) -> bytes:
        try:
            encoded = (json.dumps(
                _expect_json_object(message, "message"),
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise _validation_error("renderer message is not valid JSON") from None
        if len(encoded) > self._max_message_bytes:
            raise _validation_error("renderer message exceeds byte limit")
        return encoded

    async def load(self, url: str, session: Session) -> None:
        if type(url) is not str or not url or not isinstance(session, Session):
            raise _validation_error("renderer load arguments are invalid")
        if self._process is not None:
            raise _renderer_error("renderer process is already active")
        values = {
            "{url}": url,
            "{session_id}": session.session_id,
            "{app_id}": session.app_id,
            "{version}": session.version,
        }
        argv = self._substitute(self._process_argv, values)
        options = {"limit": min(self._max_message_bytes + 1, 2 ** 20)}
        if self._use_process_groups:
            options["start_new_session"] = True
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **options
            )
        except (OSError, TypeError, ValueError):
            self._process = None
            raise _renderer_error("cannot spawn renderer process") from None
        self._ready = asyncio.get_running_loop().create_future()
        self._failure = None
        self._stopping = False
        self._restored = False
        self._reader_task = asyncio.create_task(self._read_stdout(), name="renderer-jsonl")
        self._stderr_task = asyncio.create_task(self._read_stderr(), name="renderer-stderr")

    async def wait_ready(self, timeout: float) -> None:
        if self._ready is None:
            raise _renderer_error("renderer load has not started")
        try:
            failure = await asyncio.wait_for(asyncio.shield(self._ready), timeout)
        except (asyncio.TimeoutError, ValueError):
            raise _renderer_error("renderer readiness timed out") from None
        if failure is not None:
            raise failure

    async def send(self, message: Dict[str, Any]) -> None:
        if (self._process is None or self._process.returncode is not None
                or self._ready is None or not self._ready.done() or self._stopping):
            raise self._failure or _renderer_error("renderer is not ready")
        if self._failure is not None:
            raise self._failure
        await self._write(self._encode(message))

    async def _write(self, encoded: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise _renderer_error("renderer stdin is unavailable")
        try:
            async with self._write_lock:
                process.stdin.write(encoded)
                await process.stdin.drain()
        except (BrokenPipeError, ConnectionError, OSError):
            self._failure = _renderer_error("renderer process exited")
            raise self._failure from None

    @staticmethod
    async def _wait_for_returncode(process) -> int:
        while process.returncode is None:
            await asyncio.sleep(0.01)
        return process.returncode

    async def _read_stdout(self) -> None:
        process = self._process
        ready = False
        try:
            while process is not None and process.stdout is not None:
                raw = await process.stdout.readline()
                if not raw:
                    break
                if len(raw) > self._max_message_bytes or not raw.endswith(b"\n"):
                    raise ValueError("oversized or incomplete renderer frame")
                message = json.loads(
                    raw.decode("utf-8"), object_pairs_hook=_unique_object,
                    parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
                )
                if not ready:
                    if message != {"event": "renderer_ready"}:
                        raise ValueError("first renderer frame is not ready")
                    ready = True
                    if self._ready is not None and not self._ready.done():
                        self._ready.set_result(None)
                    continue
                if (type(message) is not dict
                        or set(message) != {"event", "dataType", "data"}
                        or message["event"] != "app_data"
                        or type(message["dataType"]) is not str
                        or not message["dataType"]
                        or type(message["data"]) is not dict):
                    raise ValueError("invalid renderer app_data")
                handler = self._handler
                if handler is not None and not self._stopping:
                    result = handler(message)
                    if inspect.isawaitable(result):
                        await result
        except (ValueError, UnicodeError, json.JSONDecodeError, RecursionError):
            self._failure = _renderer_error("renderer protocol violation")
        except Exception:
            self._failure = _renderer_error("renderer message callback failed")
        finally:
            if not self._stopping and self._failure is None:
                self._failure = _renderer_error("renderer process exited")
            if (self._ready is not None and not self._ready.done()
                    and self._failure is not None):
                self._ready.set_result(self._failure)

    async def _read_stderr(self) -> None:
        process = self._process
        try:
            while process is not None and process.stderr is not None:
                raw = await process.stderr.readline()
                if not raw:
                    return
                logging.getLogger(__name__).info(
                    "renderer stderr: %s", raw.decode("utf-8", errors="replace").rstrip()
                )
        except Exception:
            return

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        self._stopping = True
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    self._write(self._encode({"event": "renderer_stop"})),
                    self._command_timeout,
                )
            except (asyncio.TimeoutError, SmartAppError):
                pass
            try:
                await asyncio.wait_for(
                    self._wait_for_returncode(process), self._command_timeout
                )
            except asyncio.TimeoutError:
                await self._terminate(process)
        await self._finish_tasks()
        self._process = None
        self._ready = None
        self._failure = None

    async def _terminate(self, process) -> None:
        try:
            if self._use_process_groups:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(
                self._wait_for_returncode(process), min(1.0, self._command_timeout)
            )
            return
        except asyncio.TimeoutError:
            pass
        try:
            if self._use_process_groups:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            await asyncio.wait_for(
                self._wait_for_returncode(process), min(1.0, self._command_timeout)
            )
        except asyncio.TimeoutError:
            pass

    async def _finish_tasks(self) -> None:
        tasks = [task for task in (self._reader_task, self._stderr_task) if task]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None

    async def restore_default(self) -> None:
        if self._restored:
            return
        options = {}
        if self._use_process_groups:
            options["start_new_session"] = True
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *self._restore_argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **options
            )
            returncode = await asyncio.wait_for(
                self._wait_for_returncode(process), self._command_timeout
            )
            if returncode != 0:
                raise _renderer_error("default display restore failed")
        except asyncio.TimeoutError:
            if process is not None:
                await self._terminate(process)
            raise _renderer_error("default display restore timed out") from None
        except SmartAppError:
            raise
        except (OSError, TypeError, ValueError):
            raise _renderer_error("default display restore failed") from None
        self._restored = True
