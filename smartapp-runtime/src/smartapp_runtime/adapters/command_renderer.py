import asyncio
import json
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


class CommandRenderer:
    """Local command adapter. It deliberately has no inbound H5 bridge."""

    def __init__(
        self,
        config: RendererConfig,
        max_output_bytes: int = 65536,
        command_timeout: float = 15.0,
    ) -> None:
        if not isinstance(config, RendererConfig):
            raise _validation_error("renderer config is invalid")
        if type(max_output_bytes) is not int or max_output_bytes <= 0:
            raise _validation_error("renderer output limit must be positive")
        if (type(command_timeout) not in (int, float)
                or not math.isfinite(command_timeout) or command_timeout <= 0):
            raise _validation_error("renderer command timeout must be finite and positive")
        templates = (
            config.load_argv,
            config.send_argv,
            config.stop_argv,
            config.restore_argv,
        )
        for template in templates:
            self._validate_template(template)
        for template in (config.stop_argv, config.restore_argv):
            if any(item in _PLACEHOLDERS for item in template):
                raise _validation_error(
                    "stop and restore commands cannot require session placeholders"
                )
        self._load_argv = tuple(config.load_argv)
        self._send_argv = tuple(config.send_argv)
        self._stop_argv = tuple(config.stop_argv)
        self._restore_argv = tuple(config.restore_argv)
        self._max_output_bytes = max_output_bytes
        self._command_timeout = float(command_timeout)
        self._cleanup_timeout = max(0.1, min(self._command_timeout, 1.0))
        self._use_process_groups = os.name == "posix"
        self._handler: Optional[MessageHandler] = None
        self._values: Optional[Dict[str, str]] = None
        self._load_task: Optional[asyncio.Task] = None
        self._ready = False
        self._stopped = False
        self._restored = False

    @staticmethod
    def _validate_template(template: Sequence[str]) -> None:
        if not isinstance(template, tuple) or not template:
            raise _validation_error("renderer command argv must be a non-empty tuple")
        for item in template:
            if type(item) is not str or not item:
                raise _validation_error("renderer command argv must contain strings")
            matches = _PLACEHOLDER_PATTERN.findall(item)
            if matches and (len(matches) != 1 or item not in _PLACEHOLDERS):
                raise _validation_error("renderer placeholder must occupy a whole argv value")

    def set_message_handler(self, handler: Optional[MessageHandler]) -> None:
        self._handler = handler

    def _substitute(self, template: Tuple[str, ...]) -> Tuple[str, ...]:
        values = self._values or {}
        result = []
        for item in template:
            if item in _PLACEHOLDERS:
                if item not in values:
                    raise _validation_error("renderer placeholder is unavailable")
                result.append(values[item])
            else:
                result.append(item)
        return tuple(result)

    async def load(self, url: str, session: Session) -> None:
        if type(url) is not str or not url or not isinstance(session, Session):
            raise _validation_error("renderer load arguments are invalid")
        if self._load_task is not None and not self._load_task.done():
            raise _renderer_error("renderer load is already pending")
        self._values = {
            "{url}": url,
            "{session_id}": session.session_id,
            "{app_id}": session.app_id,
            "{version}": session.version,
        }
        argv = self._substitute(self._load_argv)
        self._ready = False
        self._stopped = False
        self._restored = False
        self._load_task = asyncio.create_task(
            self._run_command(argv, None), name="renderer-load"
        )

    async def wait_ready(self, timeout: float) -> None:
        task = self._load_task
        if task is None:
            raise _renderer_error("renderer load has not started")
        try:
            failure = await asyncio.wait_for(task, timeout)
        except (asyncio.TimeoutError, ValueError):
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise _renderer_error("renderer readiness timed out") from None
        if failure is not None:
            raise failure
        self._ready = True

    async def send(self, message: Dict[str, Any]) -> None:
        if not self._ready or self._stopped:
            raise _renderer_error("renderer is not ready")
        payload = _expect_json_object(message, "message")
        try:
            encoded = (json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise _validation_error("renderer message is not valid JSON") from None
        failure = await self._run_timed_command(
            self._substitute(self._send_argv), encoded
        )
        if failure is not None:
            raise failure

    async def stop(self) -> None:
        if self._stopped:
            return
        task = self._load_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        failure = await self._run_timed_command(
            self._substitute(self._stop_argv), None
        )
        if failure is not None:
            raise failure
        self._ready = False
        self._stopped = True

    async def restore_default(self) -> None:
        if self._restored:
            return
        failure = await self._run_timed_command(
            self._substitute(self._restore_argv), None
        )
        if failure is not None:
            raise failure
        self._restored = True

    async def _drain(self, stream: asyncio.StreamReader) -> bytes:
        retained = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                return bytes(retained)
            remaining = self._max_output_bytes - len(retained)
            if remaining > 0:
                retained.extend(chunk[:remaining])

    @staticmethod
    async def _wait_for_returncode(process) -> int:
        # asyncio's process.wait() may wait for inherited pipe descriptors to
        # close even after the direct child has already been reaped.
        while process.returncode is None:
            await asyncio.sleep(0.01)
        return process.returncode

    async def _run_timed_command(
        self, argv: Tuple[str, ...], stdin_data: Optional[bytes]
    ) -> Optional[SmartAppError]:
        try:
            return await asyncio.wait_for(
                self._run_command(argv, stdin_data), self._command_timeout
            )
        except asyncio.TimeoutError:
            return _renderer_error("renderer command timed out")

    async def _bounded_drain_cleanup(self, drain_tasks) -> None:
        if not drain_tasks:
            return
        for task in drain_tasks:
            if not task.done():
                task.cancel()
        try:
            await asyncio.wait_for(
                asyncio.gather(*drain_tasks, return_exceptions=True),
                self._cleanup_timeout,
            )
        except asyncio.TimeoutError:
            return

    async def _cleanup_process(self, process, drain_tasks, owns_group: bool) -> None:
        if process is not None:
            signalled = False
            if owns_group:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    signalled = True
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if not signalled and process.returncode is None:
                try:
                    process.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), self._cleanup_timeout)
                except asyncio.TimeoutError:
                    pass
        await self._bounded_drain_cleanup(drain_tasks)

    async def _run_command(
        self, argv: Tuple[str, ...], stdin_data: Optional[bytes]
    ) -> Optional[SmartAppError]:
        process = None
        drain_tasks = []
        owns_group = False
        try:
            spawn_options = {}
            if self._use_process_groups:
                spawn_options["start_new_session"] = True
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **spawn_options
            )
            owns_group = self._use_process_groups
            drain_tasks = [
                asyncio.create_task(self._drain(process.stdout), name="renderer-stdout"),
                asyncio.create_task(self._drain(process.stderr), name="renderer-stderr"),
            ]
            if stdin_data is not None:
                process.stdin.write(stdin_data)
                await process.stdin.drain()
            process.stdin.close()
            returncode = await self._wait_for_returncode(process)
            if returncode != 0:
                await self._cleanup_process(process, drain_tasks, owns_group)
                return _renderer_error("renderer command failed")
            await asyncio.gather(*drain_tasks)
            return None
        except asyncio.CancelledError:
            await self._cleanup_process(process, drain_tasks, owns_group)
            raise
        except (BrokenPipeError, ConnectionError, OSError, TypeError, ValueError):
            await self._cleanup_process(process, drain_tasks, owns_group)
            return _renderer_error("renderer command failed")
