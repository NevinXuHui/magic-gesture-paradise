import asyncio
import inspect
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from smartapp_runtime.config import RuntimeConfig
from smartapp_runtime.domain.commands import StopReason, _expect_json_object
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.infrastructure.packages.installer import InstalledApp
from smartapp_runtime.infrastructure.persistence.recovery import LinuxProcessIdentityReader
from smartapp_runtime.ports.process import BackendEvent, BackendHandle, EventCallback, ExitCallback, OwnershipCallback, StderrCallback
from smartapp_runtime.ports.repository import BackendProcessIdentity, ProcessIdentityRepository


def _error(code, message):
    return SmartAppError(code, message)


class _CallbackCancelled(Exception):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


async def _bounded_lines(stream, limit):
    # Retain only the prefix while consuming the entire oversized line.
    buffer = bytearray()
    oversized = False
    while True:
        chunk = await stream.read(min(limit + 1, 4096))
        if not chunk:
            if buffer or oversized:
                yield bytes(buffer), oversized, False
            return
        start = 0
        while start < len(chunk):
            newline = chunk.find(b"\n", start)
            end = len(chunk) if newline == -1 else newline
            part = chunk[start:end]
            available = limit - len(buffer)
            buffer.extend(part[:available])
            oversized = oversized or len(part) > available
            if newline == -1:
                break
            yield bytes(buffer), oversized, True
            buffer.clear()
            oversized = False
            start = newline + 1


class _Active:
    def __init__(self, process, handle, on_event, on_exit, on_stderr):
        self.process, self.handle = process, handle
        self.on_event, self.on_exit, self.on_stderr = on_event, on_exit, on_stderr
        self.ready = asyncio.get_running_loop().create_future()
        self.published = asyncio.Event()
        self.write_lock = asyncio.Lock()
        self.callback_lock = asyncio.Lock()
        self.tasks = []
        self.cleanup = None
        self.exit_callback_task = None
        self.stopping = False
        self.started = False
        self.init_sent = False
        self.failure = None
        self.identity = None
        self.ownership_lost = False


class ProcessSupervisor:
    def __init__(self, config: RuntimeConfig, repository: ProcessIdentityRepository,
                 environment: Optional[Mapping[str, str]] = None) -> None:
        self.config, self.repository = config, repository
        self.environment = dict(os.environ if environment is None else environment)
        self._active = None
        self._start_lock = asyncio.Lock()

    def _entry(self, installed):
        if not installed.manifest.backend.enabled:
            raise _error(ErrorCode.VALIDATION_ERROR, "backend is disabled")
        root = Path(installed.root).resolve(strict=True)
        raw = root / "backend" / installed.manifest.backend.entry
        entry = raw.resolve(strict=True)
        if raw != entry or not entry.is_file() or root not in entry.parents or root / "backend" not in entry.parents:
            raise _error(ErrorCode.VALIDATION_ERROR, "invalid backend entry")
        if not self.config.paths.python_executable.is_absolute():
            raise _error(ErrorCode.VALIDATION_ERROR, "Python executable must be absolute")
        return root, entry

    def _environment(self, session):
        result = {key: self.environment[key] for key in self.config.process.env_passthrough
                  if key in self.environment}
        result.update(SMARTAPP_SESSION_ID=session.session_id, SMARTAPP_APP_ID=session.app_id,
                      SMARTAPP_VERSION=session.version,
                      SMARTAPP_DYNAMIC_HOST=self.config.network.backend_host,
                      SMARTAPP_DYNAMIC_PORT=str(self.config.network.backend_port))
        return result

    def _encode(self, message):
        try:
            encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":"),
                                  allow_nan=False) + "\n").encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError):
            raise _error(ErrorCode.VALIDATION_ERROR, "message is not valid JSON") from None
        if len(encoded) > self.config.limits.max_message_bytes:
            raise _error(ErrorCode.VALIDATION_ERROR, "message exceeds byte limit")
        return encoded

    async def start(self, installed: InstalledApp, session: Session, init_data: Dict[str, Any],
                    on_event: EventCallback, on_exit: ExitCallback,
                    on_stderr: Optional[StderrCallback] = None,
                    on_owned: Optional[OwnershipCallback] = None) -> BackendHandle:
        async with self._start_lock:
            if self._active is not None:
                raise _error(ErrorCode.SESSION_CONFLICT, "a backend is already active")
            try:
                root, entry = self._entry(installed)
                initial = self._encode({"event": "runtime_init", "sessionId": session.session_id,
                                        "data": _expect_json_object(init_data, "initData")})
                process = await asyncio.create_subprocess_exec(
                    str(self.config.paths.python_executable), "-u", str(entry), cwd=str(root),
                    env=self._environment(session), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=True, limit=min(self.config.limits.max_message_bytes, 65536))
            except SmartAppError:
                raise
            except (OSError, ValueError, TypeError):
                raise _error(ErrorCode.INTERNAL_ERROR, "cannot spawn backend") from None
            active = _Active(process, BackendHandle(process.pid, session, root), on_event, on_exit, on_stderr)
            self._active = active
            active.tasks = [asyncio.create_task(self._stdout(active), name="backend-stdout"),
                            asyncio.create_task(self._stderr(active), name="backend-stderr"),
                            asyncio.create_task(self._wait(active), name="backend-wait")]
            ownership_error = None
            try:
                if on_owned is not None:
                    on_owned(active.handle)
            except BaseException as error:
                ownership_error = error
        try:
            if ownership_error is not None:
                raise ownership_error
            start_time = "non-linux"
            if sys.platform.startswith("linux"):
                observed = LinuxProcessIdentityReader().read(process.pid)
                if observed is None or not observed.start_time or str(entry) not in observed.cmdline:
                    raise _error(ErrorCode.INTERNAL_ERROR, "cannot establish backend ownership")
                start_time = observed.start_time
            active.identity = BackendProcessIdentity(process.pid, start_time, str(entry))
            self.repository.set_backend_process(active.identity)
            failure = await asyncio.wait_for(self._initialize(active, initial), self.config.timeouts.startup)
            if failure is not None:
                raise failure
            if active.stopping:
                raise active.failure or _error(ErrorCode.BACKEND_EXITED, "backend stopped during startup")
            active.started = True
            active.published.set()
            return active.handle
        except BaseException as error:
            if isinstance(error, asyncio.TimeoutError):
                failure = _error(ErrorCode.START_TIMEOUT, "backend readiness timed out")
            elif isinstance(error, SmartAppError):
                failure = error
            else:
                failure = _error(ErrorCode.INTERNAL_ERROR, "backend startup failed")
            self._request_cleanup(active, StopReason.RUNTIME_ERROR, failure)
            await asyncio.shield(active.cleanup)
            if isinstance(error, asyncio.CancelledError):
                raise
            raise failure from None

    async def _initialize(self, active, initial):
        if active.stopping:
            return active.failure or _error(ErrorCode.BACKEND_EXITED, "backend stopped during startup")
        active.init_sent = True
        await self._write(active, initial)
        return await asyncio.shield(active.ready)

    async def _write(self, active, encoded):
        async with active.write_lock:
            active.process.stdin.write(encoded)
            await active.process.stdin.drain()

    async def send(self, handle: BackendHandle, message: Dict[str, Any]) -> None:
        active = self._active
        if active is None or active.handle != handle or active.stopping or not active.started:
            raise _error(ErrorCode.SESSION_MISMATCH, "backend handle is not active")
        required = {"event", "seq", "data"}
        if (type(message) is not dict or not required <= message.keys()
                or message.keys() - required - {"dataType"} or message["event"] != "cloud_data"
                or type(message["seq"]) is not int or not 0 <= message["seq"] < 2 ** 63
                or ("dataType" in message and (type(message["dataType"]) is not str or not message["dataType"]))):
            raise _error(ErrorCode.VALIDATION_ERROR, "invalid cloud_data message")
        payload = dict(message, data=_expect_json_object(message["data"], "data"))
        encoded = self._encode(payload)
        try:
            async with active.write_lock:
                if active.stopping:
                    raise _error(ErrorCode.SESSION_MISMATCH, "backend is stopping")
                active.process.stdin.write(encoded)
                await active.process.stdin.drain()
        except (BrokenPipeError, ConnectionError):
            failure = _error(ErrorCode.BACKEND_EXITED, "backend stdin is closed")
            self._request_cleanup(active, StopReason.RUNTIME_ERROR, failure)
            raise failure from None

    async def _callback(self, active, callback, *args):
        if callback is None:
            return
        async with active.callback_lock:
            task = asyncio.create_task(self._invoke_callback(callback, args), name="backend-callback")
            if asyncio.current_task() is active.cleanup:
                active.exit_callback_task = task
            try:
                failure = await asyncio.shield(task)
            except asyncio.CancelledError:
                # Reader cancellation belongs to the supervisor/caller. Callback
                # cancellation is translated inside its own task instead.
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise
            finally:
                if active.exit_callback_task is task:
                    active.exit_callback_task = None
            if failure is not None:
                raise failure

    async def _invoke_callback(self, callback, args):
        try:
            result = callback(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            return _CallbackCancelled()
        except Exception as error:
            return error
        return None

    async def _stdout(self, active):
        violations = 0
        ready = False
        try:
            async for raw, oversized, complete in _bounded_lines(active.process.stdout, self.config.limits.max_message_bytes):
                if not raw and not oversized:
                    continue
                try:
                    if oversized or not complete or len(raw) + 1 > self.config.limits.max_message_bytes:
                        raise ValueError()
                    message = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
                    if not ready:
                        if type(message) is not dict or message != {"event": "app_ready"}:
                            raise ValueError()
                        ready = True
                        violations = 0
                        if not active.ready.done():
                            active.ready.set_result(None)
                        await active.published.wait()
                        continue
                    if (type(message) is not dict or set(message) != {"event", "dataType", "data"}
                            or message["event"] != "app_data" or type(message["dataType"]) is not str
                            or not message["dataType"]):
                        raise ValueError()
                    data = _expect_json_object(message["data"], "data")
                    event = BackendEvent("app_data", message["dataType"], data)
                except (ValueError, UnicodeError, SmartAppError, RecursionError):
                    violations += 1
                    if violations >= self.config.process.protocol_violation_limit:
                        self._request_cleanup(active, StopReason.RUNTIME_ERROR,
                            _error(ErrorCode.BACKEND_PROTOCOL_ERROR, "backend protocol violation limit reached"))
                        return
                    continue
                violations = 0
                if active.stopping:
                    return
                try:
                    await self._callback(active, active.on_event, event)
                except Exception:
                    if not active.stopping:
                        self._request_cleanup(active, StopReason.RUNTIME_ERROR,
                            _error(ErrorCode.INTERNAL_ERROR, "backend event callback failed"))
                    return
        except Exception:
            self._request_cleanup(active, StopReason.RUNTIME_ERROR,
                _error(ErrorCode.INTERNAL_ERROR, "backend stdout reader failed"))

    async def _stderr(self, active):
        try:
            async for raw, truncated, complete in _bounded_lines(active.process.stderr, self.config.limits.max_message_bytes):
                text = raw.decode("utf-8", errors="replace")
                try:
                    if active.on_stderr is None:
                        logging.getLogger(__name__).info("backend stderr: %s", text)
                    else:
                        await self._callback(active, active.on_stderr, text, truncated)
                except Exception:
                    pass
        except Exception:
            pass

    async def _wait(self, active):
        try:
            # A descendant can keep pipes open after the leader exits. Detect the
            # leader before waiting for pipe transports to finish.
            while active.process.returncode is None:
                await asyncio.sleep(0.01)
            if not active.stopping:
                self._request_cleanup(active, StopReason.RUNTIME_ERROR,
                    _error(ErrorCode.BACKEND_EXITED, "backend exited unexpectedly"))
            await active.process.wait()
        except Exception:
            self._request_cleanup(active, StopReason.RUNTIME_ERROR,
                _error(ErrorCode.INTERNAL_ERROR, "backend wait failed"))

    def _request_cleanup(self, active, reason, failure=None):
        if active.cleanup is None or active.cleanup.done():
            active.stopping = True
            active.failure = active.failure or failure
            if not active.ready.done():
                active.ready.set_result(failure or _error(ErrorCode.BACKEND_EXITED, "backend stopped during startup"))
            active.cleanup = asyncio.create_task(self._cleanup(active, reason), name="backend-cleanup")
        return active.cleanup

    async def stop(self, handle: BackendHandle, reason: StopReason) -> None:
        if not isinstance(reason, StopReason):
            raise _error(ErrorCode.VALIDATION_ERROR, "invalid stop reason")
        active = self._active
        if active is None or active.handle != handle:
            return
        cleanup = self._request_cleanup(active, reason)
        if asyncio.current_task() in (cleanup, active.exit_callback_task):
            return
        failure = await asyncio.shield(cleanup)
        if failure is not None:
            raise failure

    def _group_alive(self, active):
        if active.ownership_lost:
            return False
        try:
            os.killpg(active.handle.pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # EPERM does not establish that the owned group has disappeared.
            return True

    def _signal_ownership(self, active):
        """True: owned group; False: replacement; None: cannot verify safely."""
        identity = active.identity
        if identity is not None and identity.start_time != "non-linux":
            observed = LinuxProcessIdentityReader().read(identity.pid)
            if observed is not None:
                return (observed.start_time == identity.start_time
                        and identity.command_marker in observed.cmdline)
        elif active.process.returncode is None:
            # The unreaped child still owns its PID on platforms without /proc.
            return True
        try:
            os.getpgid(active.handle.pid)
        except ProcessLookupError:
            # A missing leader does not imply a missing process group: the
            # original descendants may still hold that group ID and its pipes.
            return True if active.process.returncode is not None else None
        # A PID present after our child was reaped belongs to a replacement.
        return False if active.process.returncode is not None else None

    async def _wait_group(self, active, timeout):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline and self._group_alive(active):
            await asyncio.sleep(min(0.01, max(0, deadline - asyncio.get_running_loop().time())))

    async def _discard(self, stream):
        while await stream.read(4096):
            pass

    async def _cancel_readers(self, active):
        for task in active.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*active.tasks, return_exceptions=True)

    async def _stopped(self, active):
        deadline = asyncio.get_running_loop().time() + self.config.timeouts.sigterm
        while True:
            if active.process.returncode is not None:
                if active.ownership_lost:
                    if active.process.stdout.at_eof() and active.process.stderr.at_eof():
                        return True
                elif not self._group_alive(active):
                    return True
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(0.01, remaining))

    async def _cleanup(self, active, reason):
        failed = False
        if active.init_sent and active.process.returncode is None:
            try:
                await asyncio.wait_for(self._write(active, self._encode(
                    {"event": "app_stop", "reason": reason.value})), self.config.timeouts.graceful_stop)
            except (BrokenPipeError, ConnectionError, asyncio.TimeoutError):
                pass
            except Exception:
                failed = True
        try:
            await self._wait_group(active, self.config.timeouts.graceful_stop)
        except Exception:
            failed = True
        try:
            active.process.stdin.close()
        except Exception:
            failed = True
        for signum, interval in ((signal.SIGTERM, self.config.timeouts.sigterm), (signal.SIGKILL, 0)):
            try:
                if self._group_alive(active):
                    ownership = self._signal_ownership(active)
                    if ownership is not True:
                        failed = True
                        active.ownership_lost = ownership is False
                        continue
                    os.killpg(active.handle.pid, signum)
                    await self._wait_group(active, interval)
            except ProcessLookupError:
                pass
            except Exception:
                failed = True
        try:
            stopped = await self._stopped(active)
        except Exception:
            stopped = False
        await self._cancel_readers(active)
        if not stopped:
            # Keep ownership and pipes live for a later stop retry. Never clear
            # persisted identity or admit a new backend while this group lives.
            active.tasks = [asyncio.create_task(self._discard(active.process.stdout), name="backend-stdout"),
                            asyncio.create_task(self._discard(active.process.stderr), name="backend-stderr"),
                            asyncio.create_task(self._wait(active), name="backend-wait")]
            return _error(ErrorCode.INTERNAL_ERROR, "backend process group could not be stopped")
        # Cancel callbacks before draining: a blocked callback may otherwise
        # leave a paused pipe transport alive even after the leader is reaped.
        results = await asyncio.gather(self._discard(active.process.stdout),
            self._discard(active.process.stderr), active.process.wait(), return_exceptions=True)
        failed = failed or any(isinstance(result, BaseException) for result in results)
        try:
            self.repository.clear_backend_process()
        except Exception:
            failed = True
        try:
            if active.started and active.failure is not None:
                try:
                    await self._callback(active, active.on_exit, active.handle, active.failure)
                except (Exception, asyncio.CancelledError):
                    pass
        finally:
            if self._active is active:
                self._active = None
        if failed:
            return _error(ErrorCode.INTERNAL_ERROR, "backend cleanup was incomplete")
        return None
