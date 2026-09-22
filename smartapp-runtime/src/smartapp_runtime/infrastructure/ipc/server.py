import asyncio
import copy
import inspect
import json
import os
import socket
import stat
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, Optional, Set, Tuple, Union

from smartapp_runtime.application.coordinator import CommandResult
from smartapp_runtime.domain.commands import CloudData, GetStatus, StartApp, StopApp
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.state import RuntimeState


Command = Union[StartApp, StopApp, CloudData, GetStatus]
Submit = Callable[[Command], Awaitable[CommandResult]]
ConnectedResult = Optional[bool]
ConnectedCallback = Callable[[], Union[ConnectedResult, Awaitable[ConnectedResult]]]
_VALIDATION_MESSAGE = "invalid command message"
_CONNECTION_MESSAGE = "Agent connection already active"
_OVERFLOW_MESSAGE = "result exceeds transport limit"


@dataclass(eq=False)
class _Connection:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


@dataclass(eq=False)
class _Outbound:
    payload: bytes
    size: int
    delivered: Optional[asyncio.Future] = None
    connection: Optional[_Connection] = None


class _DuplicateKey(ValueError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey("duplicate object key")
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError("non-finite JSON number")


def _valid_request_id(value: Any) -> bool:
    return (
        type(value) is str
        and 1 <= len(value) <= 128
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _json_line(value: Dict[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8") + b"\n"


def _error_result(
    request_id: str, state: str, code: ErrorCode, message: str
) -> Dict[str, Any]:
    return {
        "event": "command_result",
        "requestId": request_id,
        "ok": False,
        "state": state,
        "error": {"code": code.value, "message": message},
    }


class AgentServer:
    """Single-client JSON Lines server with a persistent bounded output FIFO."""

    def __init__(
        self,
        socket_path: Path,
        submit: Submit,
        max_input_message_bytes: int,
        max_output_message_bytes: int,
        max_queue_messages: int,
        max_queue_bytes: int,
        on_connected: Optional[ConnectedCallback] = None,
    ) -> None:
        limits = (
            max_input_message_bytes,
            max_output_message_bytes,
            max_queue_messages,
            max_queue_bytes,
        )
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("Agent server limits must be positive integers")
        if max_queue_bytes < max_output_message_bytes:
            raise ValueError("Agent output queue must hold one maximum-size message")
        longest_state = max((state.value for state in RuntimeState), key=len)
        mandatory_results = (
            _json_line(_error_result(
                "\\" * 128, RuntimeState.IDLE.value,
                ErrorCode.VALIDATION_ERROR, _VALIDATION_MESSAGE,
            )),
            _json_line(_error_result(
                "connection", RuntimeState.IDLE.value,
                ErrorCode.QUEUE_FULL, _CONNECTION_MESSAGE,
            )),
            _json_line(_error_result(
                "\\" * 128, longest_state,
                ErrorCode.INTERNAL_ERROR, _OVERFLOW_MESSAGE,
            )),
        )
        if max_output_message_bytes < max(map(len, mandatory_results)):
            raise ValueError("Agent output limit cannot carry a command result")
        if not callable(submit):
            raise TypeError("submit must be callable")
        if on_connected is not None and not callable(on_connected):
            raise TypeError("on_connected must be callable")
        self._socket_path = Path(socket_path)
        self._submit = submit
        self._max_input_message_bytes = max_input_message_bytes
        self._max_output_message_bytes = max_output_message_bytes
        self._max_queue_messages = max_queue_messages
        self._max_queue_bytes = max_queue_bytes
        self._on_connected = on_connected
        self._server: Optional[asyncio.AbstractServer] = None
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._connection: Optional[_Connection] = None
        self._connection_lock = asyncio.Lock()
        self._condition = asyncio.Condition()
        self._outbound: Deque[_Outbound] = deque()
        self._outbound_bytes = 0
        self._outbound_revision = 0
        self._accepted_sequence = 0
        self._writer_task: Optional[asyncio.Task] = None
        self._owned_tasks: Set[asyncio.Task] = set()
        self._running = False

    @property
    def connected(self) -> bool:
        return self._running and self._connection is not None

    async def start(self) -> None:
        if self._running:
            return
        if self._server is not None:
            return
        self._validate_or_create_parent()
        self._remove_stale_socket()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.setblocking(False)
        try:
            listener.bind(str(self._socket_path))
            listener.listen(socket.SOMAXCONN)
            node = self._socket_path.lstat()
            if not stat.S_ISSOCK(node.st_mode):
                raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket node is invalid")
            self._socket_identity = (node.st_dev, node.st_ino)
            server = await asyncio.start_unix_server(
                self._accept, sock=listener, limit=self._max_input_message_bytes
            )
            listener = None
            self._server = server
            self._require_owned_socket_node()
            chmod_options = (
                {"follow_symlinks": False}
                if os.chmod in os.supports_follow_symlinks else {}
            )
            os.chmod(str(self._socket_path), 0o660, **chmod_options)
            mode = stat.S_IMODE(self._require_owned_socket_node().st_mode)
            if mode != 0o660:
                raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket mode is invalid")
            self._running = True
            self._writer_task = asyncio.create_task(
                self._writer_loop(), name="agent-server-writer"
            )
        except BaseException:
            if listener is not None:
                listener.close()
            await self._close_server()
            self._unlink_owned_socket()
            self._socket_identity = None
            raise

    async def stop(self) -> None:
        self._running = False
        server, self._server = self._server, None
        if server is not None:
            server.close()
        connection = await self._detach_connection(None)
        if connection is not None:
            connection.writer.close()

        current = asyncio.current_task()
        tasks = set(self._owned_tasks)
        if self._writer_task is not None:
            tasks.add(self._writer_task)
        tasks.discard(current)
        for task in tasks:
            if not task.done():
                task.cancel()
        async with self._condition:
            self._condition.notify_all()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
        if connection is not None:
            await self._wait_writer_closed(connection.writer)
        self._owned_tasks.clear()
        self._writer_task = None
        async with self._condition:
            while self._outbound:
                item = self._outbound.popleft()
                if item.delivered is not None and not item.delivered.done():
                    item.delivered.cancel()
            self._outbound_revision += 1
            self._outbound_bytes = 0
            self._condition.notify_all()
        self._unlink_owned_socket()
        self._socket_identity = None

    def publish(self, event: Dict[str, Any]) -> bool:
        if not self.connected or type(event) is not dict:
            return False
        try:
            payload = _json_line(copy.deepcopy(event))
        except (TypeError, ValueError, UnicodeError, RecursionError):
            return False
        size = len(payload)
        if size > self._max_output_message_bytes:
            return False
        if (len(self._outbound) >= self._max_queue_messages
                or self._outbound_bytes + size > self._max_queue_bytes):
            return False
        self._outbound.append(_Outbound(payload, size))
        self._outbound_bytes += size
        self._accepted_sequence += 1
        self._outbound_revision += 1
        self._notify_condition()
        return True

    async def _accept(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._owned_tasks.add(task)
        connection = _Connection(reader, writer)
        accepted = False
        try:
            async with self._connection_lock:
                if self._running and self._connection is None:
                    self._connection = connection
                    accepted = True
            if not accepted:
                await self._reject_connection(writer)
                return
            await self._notify_condition_async()
            if not await self._initialize_connection(connection):
                return
            await self._read_connection(connection)
        finally:
            if accepted:
                await self._detach_connection(connection)
            writer.close()
            await self._wait_writer_closed(writer)
            if task is not None:
                self._owned_tasks.discard(task)

    async def _read_connection(self, connection: _Connection) -> None:
        while self._running and self._connection is connection:
            try:
                line = await connection.reader.readline()
            except ValueError:
                await self._send_validation(connection, "invalid", fatal=True)
                return
            if not line:
                return
            if len(line) > self._max_input_message_bytes:
                await self._send_validation(connection, "invalid", fatal=True)
                return
            if not line.endswith(b"\n"):
                request_id = self._extract_request_id_from_complete_json(line)
                await self._send_validation(connection, request_id, fatal=True)
                return
            try:
                command = self._parse_command(line[:-1])
            except SmartAppError as error:
                request_id = error.details.get("requestId", "invalid")
                await self._send_validation(connection, request_id, fatal=False)
                continue
            self._spawn(self._dispatch(command), "agent-command-{0}".format(command.request_id))

    def _parse_command(self, payload: bytes) -> Command:
        request_id = "invalid"
        try:
            text = payload.decode("utf-8")
            value = json.loads(
                text, object_pairs_hook=_unique_object, parse_constant=_reject_constant
            )
            if type(value) is dict and _valid_request_id(value.get("requestId")):
                request_id = value["requestId"]
            if type(value) is not dict:
                raise ValueError("top-level JSON value is not an object")
            discriminator = value.get("command")
            parsers = {
                "start_app": StartApp.from_dict,
                "stop_app": StopApp.from_dict,
                "cloud_data": CloudData.from_dict,
                "get_status": GetStatus.from_dict,
            }
            if type(discriminator) is not str or discriminator not in parsers:
                raise ValueError("invalid command discriminator")
            return parsers[discriminator](value)
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, ValueError,
                SmartAppError, TypeError, RecursionError):
            raise SmartAppError(
                ErrorCode.VALIDATION_ERROR,
                "invalid command message",
                {"requestId": request_id},
            ) from None

    def _extract_request_id_from_complete_json(self, payload: bytes) -> str:
        try:
            value = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, ValueError,
                TypeError, RecursionError):
            return "invalid"
        if type(value) is dict and _valid_request_id(value.get("requestId")):
            return value["requestId"]
        return "invalid"

    async def _dispatch(self, command: Command) -> None:
        try:
            result = await self._submit(command)
            if not isinstance(result, CommandResult):
                raise TypeError("invalid coordinator result")
        except asyncio.CancelledError:
            raise
        except Exception:
            result = CommandResult(
                command.request_id,
                False,
                RuntimeState.IDLE,
                error=SmartAppError(ErrorCode.INTERNAL_ERROR, "command dispatch failed"),
            )
        try:
            payload = _json_line(result.to_dict())
        except (TypeError, ValueError, UnicodeError, RecursionError):
            result = CommandResult(
                command.request_id,
                False,
                RuntimeState.IDLE,
                error=SmartAppError(ErrorCode.INTERNAL_ERROR, "command dispatch failed"),
            )
            payload = _json_line(result.to_dict())
        if len(payload) > self._max_output_message_bytes:
            state = result.state if isinstance(result.state, RuntimeState) else RuntimeState.IDLE
            payload = self._transport_payload(_error_result(
                command.request_id,
                state.value,
                ErrorCode.INTERNAL_ERROR,
                _OVERFLOW_MESSAGE,
            ))
        await self._enqueue_required(payload)

    async def _send_validation(
        self, connection: _Connection, request_id: str, fatal: bool
    ) -> None:
        value = _error_result(
            request_id if _valid_request_id(request_id) else "invalid",
            RuntimeState.IDLE.value,
            ErrorCode.VALIDATION_ERROR,
            _VALIDATION_MESSAGE,
        )
        payload = self._transport_payload(value)
        delivered = await self._enqueue_required(
            payload, wait_for_delivery=fatal, connection=connection
        )
        if fatal and delivered is not None:
            await delivered

    async def _enqueue_required(
        self,
        payload: bytes,
        wait_for_delivery: bool = False,
        connection: Optional[_Connection] = None,
    ) -> Optional[asyncio.Future]:
        if len(payload) > self._max_output_message_bytes:
            raise ValueError("required Agent result exceeds output limit")
        size = len(payload)
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._running
                or (connection is not None and self._connection is not connection)
                or (len(self._outbound) < self._max_queue_messages
                    and self._outbound_bytes + size <= self._max_queue_bytes)
            )
            if (not self._running
                    or (connection is not None and self._connection is not connection)):
                return None
            delivered = (
                asyncio.get_running_loop().create_future()
                if wait_for_delivery else None
            )
            self._outbound.append(_Outbound(payload, size, delivered, connection))
            self._outbound_bytes += size
            self._accepted_sequence += 1
            self._outbound_revision += 1
            self._condition.notify_all()
            return delivered

    async def _writer_loop(self) -> None:
        while self._running:
            async with self._condition:
                await self._condition.wait_for(
                    lambda: not self._running
                    or (self._outbound and self._connection is not None)
                )
                if not self._running:
                    return
                item = self._outbound[0]
                connection = self._connection
            if connection is None:
                continue
            if item.connection is not None and item.connection is not connection:
                async with self._condition:
                    if self._outbound and self._outbound[0] is item:
                        self._outbound.popleft()
                        self._outbound_bytes -= item.size
                        self._outbound_revision += 1
                        self._settle_delivery(item, False)
                        self._condition.notify_all()
                continue
            try:
                connection.writer.write(item.payload)
                await connection.writer.drain()
            except (ConnectionError, OSError, RuntimeError):
                stale = await self._detach_connection(connection)
                if stale is not None:
                    stale.writer.close()
                continue
            async with self._condition:
                if self._outbound and self._outbound[0] is item:
                    self._outbound.popleft()
                    self._outbound_bytes -= item.size
                    self._outbound_revision += 1
                    self._settle_delivery(item, True)
                    self._condition.notify_all()

    async def _initialize_connection(self, connection: _Connection) -> bool:
        if not await self._wait_fifo_empty(connection):
            return False
        if self._on_connected is None:
            return True
        while self._running and self._connection is connection:
            accepted_before = self._accepted_sequence
            completed = await self._invoke_connected()
            if not self._running or self._connection is not connection:
                return False
            if completed is not False:
                return True
            accepted = self._accepted_sequence != accepted_before
            async with self._condition:
                if not self._running or self._connection is not connection:
                    return False
                if not self._outbound:
                    if accepted:
                        continue
                    return False
                revision = self._outbound_revision
                await self._condition.wait_for(
                    lambda: not self._running
                    or self._connection is not connection
                    or self._outbound_revision != revision
                )
                if not self._running or self._connection is not connection:
                    return False
        return False

    async def _wait_fifo_empty(self, connection: _Connection) -> bool:
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._running
                or self._connection is not connection
                or not self._outbound
            )
            return (
                self._running
                and self._connection is connection
                and not self._outbound
            )

    async def _invoke_connected(self) -> ConnectedResult:
        try:
            result = self._on_connected()
            if inspect.isawaitable(result):
                result = await result
        except asyncio.CancelledError:
            raise
        except Exception:
            return True
        if result is None or type(result) is bool:
            return result
        return True

    async def _reject_connection(self, writer: asyncio.StreamWriter) -> None:
        payload = self._transport_payload(_error_result(
            "connection",
            RuntimeState.IDLE.value,
            ErrorCode.QUEUE_FULL,
            _CONNECTION_MESSAGE,
        ))
        try:
            writer.write(payload)
            await writer.drain()
        except (ConnectionError, OSError, RuntimeError):
            return

    def _transport_payload(self, value: Dict[str, Any]) -> bytes:
        payload = _json_line(value)
        if len(payload) > self._max_output_message_bytes:
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent output limit is too small")
        return payload

    async def _detach_connection(
        self, expected: Optional[_Connection]
    ) -> Optional[_Connection]:
        async with self._connection_lock:
            current = self._connection
            if current is None or (expected is not None and current is not expected):
                return None
            self._connection = None
        async with self._condition:
            retained = deque()
            while self._outbound:
                item = self._outbound.popleft()
                if item.connection is current:
                    self._outbound_bytes -= item.size
                    self._outbound_revision += 1
                    self._settle_delivery(item, False)
                else:
                    retained.append(item)
            self._outbound = retained
            self._condition.notify_all()
        return current

    @staticmethod
    def _settle_delivery(item: _Outbound, delivered: bool) -> None:
        if item.delivered is not None and not item.delivered.done():
            item.delivered.set_result(delivered)

    def _spawn(self, awaitable: Awaitable[Any], name: str) -> None:
        task = asyncio.create_task(awaitable, name=name)
        self._owned_tasks.add(task)

        def finished(completed: asyncio.Task) -> None:
            self._owned_tasks.discard(completed)
            if not completed.cancelled():
                completed.exception()

        task.add_done_callback(finished)

    def _notify_condition(self) -> None:
        if not self._running:
            return

        async def notify():
            await self._notify_condition_async()

        self._spawn(notify(), "agent-queue-notify")

    async def _notify_condition_async(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def _close_server(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    def _validate_or_create_parent(self) -> None:
        parent = self._socket_path.parent
        try:
            node = parent.lstat()
        except FileNotFoundError:
            ancestor = parent.parent.lstat()
            if not stat.S_ISDIR(ancestor.st_mode) or stat.S_ISLNK(ancestor.st_mode):
                raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket parent is invalid")
            parent.mkdir()
            return
        if not stat.S_ISDIR(node.st_mode) or stat.S_ISLNK(node.st_mode):
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket parent is invalid")

    def _remove_stale_socket(self) -> None:
        try:
            node = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(node.st_mode):
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket path already exists")

        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(self._socket_path))
        except (ConnectionRefusedError, FileNotFoundError):
            pass
        except OSError as error:
            raise SmartAppError(
                ErrorCode.INTERNAL_ERROR, "Cannot determine whether agent socket is active"
            ) from error
        else:
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket is already in use")
        finally:
            probe.close()

        try:
            current = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (current.st_dev, current.st_ino) != (node.st_dev, node.st_ino):
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket path changed during startup")
        self._socket_path.unlink()

    def _unlink_owned_socket(self) -> None:
        if self._socket_identity is None:
            return
        try:
            node = self._socket_path.lstat()
        except FileNotFoundError:
            return
        if (stat.S_ISSOCK(node.st_mode)
                and (node.st_dev, node.st_ino) == self._socket_identity):
            self._socket_path.unlink()

    def _require_owned_socket_node(self) -> os.stat_result:
        try:
            node = self._socket_path.lstat()
        except FileNotFoundError:
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket node was replaced")
        if (not stat.S_ISSOCK(node.st_mode)
                or (node.st_dev, node.st_ino) != self._socket_identity):
            raise SmartAppError(ErrorCode.INTERNAL_ERROR, "Agent socket node was replaced")
        return node

    @staticmethod
    async def _wait_writer_closed(writer: asyncio.StreamWriter) -> None:
        try:
            await writer.wait_closed()
        except BaseException:
            return
