import asyncio
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.application.coordinator import CommandResult
from smartapp_runtime.domain.commands import CloudData, GetStatus, StartApp, StopApp
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.ipc.server import AgentServer


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


class FakeCoordinator:
    def __init__(self):
        self.commands = []
        self.entered = asyncio.Queue()
        self.gates = {}
        self.results = {}

    async def submit(self, command):
        self.commands.append(command)
        await self.entered.put(command)
        gate = self.gates.get(command.request_id)
        if gate is not None:
            await gate.wait()
        if command.request_id in self.results:
            return self.results[command.request_id]
        session_id = getattr(command, "session_id", None)
        return CommandResult(command.request_id, True, RuntimeState.RUNNING, session_id)


class ControlledWriter:
    """Defers the transport acceptance boundary until the test releases drain."""

    def __init__(self, delegate):
        self.delegate = delegate
        self.payloads = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.fail = False

    def write(self, payload):
        self.payloads.append(payload)

    async def drain(self):
        self.entered.set()
        await self.release.wait()
        if self.fail:
            raise ConnectionResetError("controlled disconnect")
        for payload in self.payloads:
            self.delegate.write(payload)
        self.payloads = []
        await self.delegate.drain()

    def close(self):
        self.delegate.close()

    async def wait_closed(self):
        try:
            await self.delegate.wait_closed()
        except (ConnectionError, OSError, RuntimeError):
            pass


class AgentServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.temp.name) / "run" / "runtime.sock"
        self.socket_path.parent.mkdir()
        self.coordinator = FakeCoordinator()
        self.initial_tasks = asyncio.all_tasks()
        self.server = self.make_server()
        await self.server.start()

    async def asyncTearDown(self):
        await self.server.stop()
        await asyncio.sleep(0)
        leaked = [
            task for task in asyncio.all_tasks() - self.initial_tasks
            if task is not asyncio.current_task() and not task.done()
        ]
        self.assertEqual(leaked, [], "AgentServer left live tasks")
        self.temp.cleanup()

    def make_server(self, **overrides):
        values = {
            "socket_path": self.socket_path,
            "submit": self.coordinator.submit,
            "max_input_message_bytes": 1024 * 1024,
            "max_output_message_bytes": 1024 * 1024,
            "max_queue_messages": 8,
            "max_queue_bytes": 2 * 1024 * 1024,
        }
        values.update(overrides)
        return AgentServer(**values)

    async def restart_with(self, **overrides):
        await self.server.stop()
        self.server = self.make_server(**overrides)
        await self.server.start()

    async def connect(self):
        return await asyncio.open_unix_connection(str(self.socket_path))

    async def read_event(self, reader):
        line = await asyncio.wait_for(reader.readline(), 1)
        self.assertTrue(line.endswith(b"\n"), line)
        return json.loads(line)

    async def wait_until(self, predicate):
        async def poll():
            while not predicate():
                await asyncio.sleep(0)
        await asyncio.wait_for(poll(), 1)

    async def close_client(self, writer):
        writer.close()
        await writer.wait_closed()

    def control_server_writer(self):
        controlled = ControlledWriter(self.server._connection.writer)
        self.server._connection.writer = controlled
        return controlled

    async def test_socket_has_exact_group_mode_and_lifecycle_is_idempotent(self):
        mode = stat.S_IMODE(self.socket_path.lstat().st_mode)
        self.assertEqual(mode, 0o660)
        await self.server.start()
        await self.server.stop()
        await self.server.stop()
        self.assertFalse(self.socket_path.exists())
        await self.server.start()
        self.assertTrue(stat.S_ISSOCK(self.socket_path.lstat().st_mode))

    async def test_constructor_rejects_limits_that_cannot_carry_a_result(self):
        with self.assertRaises(ValueError):
            self.make_server(max_output_message_bytes=1, max_queue_bytes=1)
        with self.assertRaises(ValueError):
            self.make_server(max_output_message_bytes=409, max_queue_bytes=409)
        with self.assertRaises(ValueError):
            self.make_server(max_output_message_bytes=256, max_queue_bytes=255)

    async def test_maximum_request_id_validation_keeps_code_correlation_and_state(self):
        await self.restart_with(max_output_message_bytes=410, max_queue_bytes=410)
        reader, writer = await self.connect()
        request_id = "\\" * 128
        writer.write(encode({"requestId": request_id, "command": "future"}))
        await writer.drain()
        result = await self.read_event(reader)
        self.assertEqual(result["requestId"], request_id)
        self.assertEqual(result["state"], "IDLE")
        self.assertEqual(result["error"]["code"], "VALIDATION_ERROR")
        await self.close_client(writer)

    async def test_oversized_command_result_keeps_request_state_and_internal_error(self):
        self.coordinator.results["oversized"] = CommandResult(
            "oversized",
            False,
            RuntimeState.PREPARING,
            session_id="private-payload-" * 100,
            error=SmartAppError(
                ErrorCode.SESSION_CONFLICT,
                "private-payload-" * 100,
                {"private": "private-payload-" * 100},
            ),
        )
        await self.restart_with(max_output_message_bytes=420, max_queue_bytes=420)
        reader, writer = await self.connect()
        writer.write(encode({"requestId": "oversized", "command": "get_status"}))
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), 1)
        result = json.loads(raw)
        self.assertEqual(result["requestId"], "oversized")
        self.assertEqual(result["state"], "PREPARING")
        self.assertEqual(result["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn(b"private-payload", raw)
        await self.close_client(writer)

    async def test_all_commands_are_parsed_and_results_keep_request_correlation(self):
        reader, writer = await self.connect()
        commands = [
            {
                "requestId": "start-1", "command": "start_app", "sessionId": "session-1",
                "appId": "demo", "version": "1.0", "packageUrl": "https://example.test/a.tgz",
                "packageSize": 10, "sha256": "a" * 64, "initData": {"mode": "test"},
            },
            {"requestId": "status-1", "command": "get_status"},
            {
                "requestId": "data-1", "command": "cloud_data", "sessionId": "session-1",
                "seq": 1, "target": "auto", "dataType": "question", "data": {"word": "Apple"},
            },
            {
                "requestId": "stop-1", "command": "stop_app", "sessionId": "session-1",
                "reason": "operator",
            },
        ]
        writer.write(b"".join(encode(item) for item in commands))
        await writer.drain()
        results = [await self.read_event(reader) for _ in commands]
        self.assertEqual({item["requestId"] for item in results}, {item["requestId"] for item in commands})
        self.assertTrue(all(item["event"] == "command_result" and item["ok"] for item in results))
        self.assertEqual(
            {type(command) for command in self.coordinator.commands},
            {StartApp, GetStatus, CloudData, StopApp},
        )
        await self.close_client(writer)

    async def test_long_start_does_not_block_later_status_or_stop_input(self):
        gate = asyncio.Event()
        self.coordinator.gates["start-long"] = gate
        reader, writer = await self.connect()
        start = {
            "requestId": "start-long", "command": "start_app", "sessionId": "session-1",
            "appId": "demo", "version": "1.0", "packageUrl": "https://example.test/a.tgz",
            "packageSize": 10, "sha256": "a" * 64, "initData": {},
        }
        writer.write(encode(start) + encode({"requestId": "status-fast", "command": "get_status"})
                     + encode({"requestId": "stop-fast", "command": "stop_app",
                               "sessionId": "session-1", "reason": "operator"}))
        await writer.drain()
        first = await self.read_event(reader)
        second = await self.read_event(reader)
        self.assertEqual({first["requestId"], second["requestId"]}, {"status-fast", "stop-fast"})
        gate.set()
        self.assertEqual((await self.read_event(reader))["requestId"], "start-long")
        await self.close_client(writer)

    async def test_semantic_errors_are_sanitized_and_connection_continues(self):
        reader, writer = await self.connect()
        lines = [
            b'{"requestId":"dup","command":"get_status","command":"stop_app"}\n',
            b'{"requestId":"bad-json","command":}\n',
            b'[]\n',
            b'{"requestId":"unknown","command":"future"}\n',
            b'{"requestId":"wrong-type","command":4}\n',
            b'{"requestId":"bad\\u0001id","command":"get_status"}\n',
            b'\xff\n',
            encode({"requestId": "valid-after-errors", "command": "get_status"}),
        ]
        writer.write(b"".join(lines))
        await writer.drain()
        results = [await self.read_event(reader) for _ in lines]
        self.assertTrue(all(not item["ok"] for item in results[:-1]))
        self.assertTrue(all(item["error"]["code"] == "VALIDATION_ERROR" for item in results[:-1]))
        self.assertTrue(all("\xff" not in item["error"]["message"] for item in results[:-1]))
        self.assertEqual(results[-1]["requestId"], "valid-after-errors")
        self.assertTrue(results[-1]["ok"])
        await self.close_client(writer)

    async def test_exact_input_byte_limit_is_accepted(self):
        base = {"requestId": "x", "command": "get_status"}
        base_size = len(encode(base))
        request_id = "x" * (128 - base_size + len(encode({"requestId": "", "command": "get_status"})))
        message = encode({"requestId": request_id, "command": "get_status"})
        self.assertLessEqual(len(request_id), 128)
        await self.restart_with(max_input_message_bytes=len(message))
        reader, writer = await self.connect()
        writer.write(message)
        await writer.drain()
        result = await self.read_event(reader)
        self.assertEqual(result["requestId"], request_id)
        self.assertTrue(result["ok"])
        await self.close_client(writer)

    async def test_over_limit_and_incomplete_frames_return_one_error_then_close(self):
        await self.restart_with(max_input_message_bytes=80)
        for payload, half_close in ((b"x" * 81 + b"\n", False),
                                    (b'{"requestId":"partial","command":"get_status"}', True)):
            with self.subTest(half_close=half_close):
                reader, writer = await self.connect()
                writer.write(payload)
                await writer.drain()
                if half_close:
                    writer.write_eof()
                result = await self.read_event(reader)
                self.assertEqual(result["error"]["code"], "VALIDATION_ERROR")
                self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
                await self.close_client(writer)

    async def test_failed_framing_error_is_dropped_with_its_connection(self):
        await self.restart_with(max_input_message_bytes=80)
        _, writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        old_handler = next(
            task for task in self.server._owned_tasks
            if getattr(task.get_coro(), "__qualname__", "") == "AgentServer._accept"
        )
        controlled = self.control_server_writer()
        writer.write(b"x" * 81 + b"\n")
        await writer.drain()
        await controlled.entered.wait()
        controlled.fail = True
        controlled.release.set()
        await self.wait_until(lambda: not self.server.connected)
        await self.wait_until(old_handler.done)
        writer.transport.abort()

        new_reader, new_writer = await self.connect()
        new_writer.write(encode({"requestId": "fresh", "command": "get_status"}))
        await new_writer.drain()
        result = await self.read_event(new_reader)
        self.assertEqual(result["requestId"], "fresh")
        self.assertTrue(result["ok"])
        await self.close_client(new_writer)

    async def test_second_client_is_rejected_without_replacing_first(self):
        first_reader, first_writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        second_reader, second_writer = await self.connect()
        rejected = await self.read_event(second_reader)
        self.assertEqual(rejected["requestId"], "connection")
        self.assertEqual(rejected["state"], "IDLE")
        self.assertEqual(rejected["error"]["code"], "QUEUE_FULL")
        self.assertEqual(await second_reader.read(), b"")
        first_writer.write(encode({"requestId": "still-active", "command": "get_status"}))
        await first_writer.drain()
        self.assertEqual((await self.read_event(first_reader))["requestId"], "still-active")
        await self.close_client(second_writer)
        await self.close_client(first_writer)

    async def test_disconnect_does_not_submit_stop_and_reconnect_receives_pending_result(self):
        gate = asyncio.Event()
        self.coordinator.gates["pending"] = gate
        reader, writer = await self.connect()
        writer.write(encode({"requestId": "pending", "command": "get_status"}))
        await writer.drain()
        await asyncio.wait_for(self.coordinator.entered.get(), 1)
        await self.close_client(writer)
        await self.wait_until(lambda: not self.server.connected)
        self.assertFalse(any(isinstance(command, StopApp) for command in self.coordinator.commands))
        gate.set()
        new_reader, new_writer = await self.connect()
        self.assertEqual((await self.read_event(new_reader))["requestId"], "pending")
        await self.close_client(new_writer)

    async def test_on_connected_flush_appends_behind_already_accepted_fifo(self):
        connects = 0

        async def on_connected():
            nonlocal connects
            connects += 1
            if connects == 2:
                self.assertTrue(self.server.publish({"event": "older-router-item", "value": 2}))

        await self.restart_with(on_connected=on_connected)
        _, writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        controlled = self.control_server_writer()
        self.assertTrue(self.server.publish({"event": "accepted-first", "value": 1}))
        await controlled.entered.wait()
        writer.transport.abort()
        controlled.fail = True
        controlled.release.set()
        await self.wait_until(lambda: not self.server.connected)
        reader2, writer2 = await self.connect()
        first = await self.read_event(reader2)
        second = await self.read_event(reader2)
        self.assertEqual([first["event"], second["event"]], ["accepted-first", "older-router-item"])
        await self.close_client(writer2)

    async def test_on_connected_is_barrier_before_new_command_results(self):
        connects = 0
        callback_entered = asyncio.Event()
        callback_release = asyncio.Event()

        async def on_connected():
            nonlocal connects
            connects += 1
            if connects == 2:
                callback_entered.set()
                await callback_release.wait()
                self.assertTrue(self.server.publish({"event": "older-router-item"}))

        await self.restart_with(on_connected=on_connected)
        _, first_writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        controlled = self.control_server_writer()
        self.assertTrue(self.server.publish({"event": "accepted-first"}))
        await controlled.entered.wait()
        first_writer.transport.abort()
        controlled.fail = True
        controlled.release.set()
        await self.wait_until(lambda: not self.server.connected)

        reader, writer = await self.connect()
        await callback_entered.wait()
        writer.write(encode({"requestId": "after-flush", "command": "get_status"}))
        await writer.drain()
        first = await self.read_event(reader)
        self.assertEqual(first["event"], "accepted-first")
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(self.coordinator.commands, [])
        callback_release.set()
        second = await self.read_event(reader)
        third = await self.read_event(reader)
        self.assertEqual(second["event"], "older-router-item")
        self.assertEqual(third["requestId"], "after-flush")
        await self.close_client(writer)

    async def test_reconnect_drains_full_persistent_fifo_before_retrying_router_flush(self):
        connects = 0
        router_backlog = [
            {"event": "older-router-item", "index": 1},
            {"event": "older-router-item", "index": 2},
        ]

        def on_connected():
            nonlocal connects
            connects += 1
            if connects == 1:
                return True
            while router_backlog:
                if not self.server.publish(router_backlog[0]):
                    return False
                router_backlog.pop(0)
            return True

        await self.restart_with(
            on_connected=on_connected,
            max_queue_messages=1,
            max_queue_bytes=512,
            max_output_message_bytes=512,
        )
        _, first_writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        controlled = self.control_server_writer()
        self.assertTrue(self.server.publish({"event": "accepted-first"}))
        await controlled.entered.wait()
        first_writer.transport.abort()
        controlled.fail = True
        controlled.release.set()
        await self.wait_until(lambda: not self.server.connected)

        reader, writer = await self.connect()
        writer.write(encode({"requestId": "after-router", "command": "get_status"}))
        await writer.drain()
        first = await self.read_event(reader)
        second = await self.read_event(reader)
        third = await self.read_event(reader)
        fourth = await self.read_event(reader)
        self.assertEqual(
            [
                first["event"],
                second["index"],
                third["index"],
                fourth["requestId"],
            ],
            ["accepted-first", 1, 2, "after-router"],
        )
        self.assertEqual(router_backlog, [])
        await self.close_client(writer)

    async def test_false_callback_without_fifo_progress_closes_without_reading(self):
        calls = 0

        def on_connected():
            nonlocal calls
            calls += 1
            return False

        await self.restart_with(on_connected=on_connected)
        reader, writer = await self.connect()
        writer.write(encode({"requestId": "must-not-run", "command": "get_status"}))
        await writer.drain()
        self.assertEqual(await asyncio.wait_for(reader.read(), 1), b"")
        self.assertEqual(calls, 1)
        self.assertEqual(self.coordinator.commands, [])
        await self.close_client(writer)

    async def test_stop_and_detach_wake_fifo_progress_barrier(self):
        for action in ("stop", "detach"):
            with self.subTest(action=action):
                controlled_holder = []
                callback_calls = 0

                def on_connected():
                    nonlocal callback_calls
                    callback_calls += 1
                    if callback_calls == 1:
                        controlled_holder.append(self.control_server_writer())
                        self.assertTrue(self.server.publish({"event": "router-batch"}))
                    return False

                await self.restart_with(
                    on_connected=on_connected,
                    max_queue_messages=1,
                    max_queue_bytes=512,
                    max_output_message_bytes=512,
                )
                _, writer = await self.connect()
                await self.wait_until(lambda: bool(controlled_holder))
                controlled = controlled_holder[0]
                await controlled.entered.wait()
                handler = next(
                    task for task in self.server._owned_tasks
                    if getattr(task.get_coro(), "__qualname__", "") == "AgentServer._accept"
                )
                if action == "stop":
                    await self.server.stop()
                else:
                    controlled.fail = True
                    controlled.release.set()
                    await self.wait_until(lambda: not self.server.connected)
                    await self.wait_until(handler.done)
                self.assertTrue(handler.done())
                self.assertEqual(callback_calls, 1)
                writer.transport.abort()

    async def test_stop_cancels_and_joins_blocked_on_connected_callback(self):
        callback_entered = asyncio.Event()
        callback_cancelled = asyncio.Event()

        async def on_connected():
            callback_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                callback_cancelled.set()

        await self.restart_with(on_connected=on_connected)
        _, writer = await self.connect()
        await callback_entered.wait()
        handler = next(
            task for task in self.server._owned_tasks
            if getattr(task.get_coro(), "__qualname__", "") == "AgentServer._accept"
        )
        await self.server.stop()
        self.assertTrue(callback_cancelled.is_set())
        self.assertTrue(handler.done())
        writer.transport.abort()

    async def test_publish_is_nonblocking_bounded_and_copies_input(self):
        await self.restart_with(max_queue_messages=1, max_queue_bytes=512,
                                max_output_message_bytes=512)
        _, writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        controlled = self.control_server_writer()
        event = {"event": "app_data", "data": {"value": "original"}}
        self.assertTrue(self.server.publish(event))
        event["data"]["value"] = "mutated"
        self.assertFalse(self.server.publish({"event": "overflow"}))
        await controlled.entered.wait()
        writer.transport.abort()
        controlled.fail = True
        controlled.release.set()
        await self.wait_until(lambda: not self.server.connected)
        self.assertFalse(self.server.publish({"event": "disconnected"}))
        reader2, writer2 = await self.connect()
        self.assertEqual((await self.read_event(reader2))["data"]["value"], "original")
        await self.close_client(writer2)

    async def test_invalid_or_oversized_publish_does_not_mutate_queue(self):
        await self.restart_with(max_output_message_bytes=512, max_queue_bytes=512)
        _, writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        controlled = self.control_server_writer()
        self.assertFalse(self.server.publish({"event": "bad", "value": float("nan")}))
        self.assertFalse(self.server.publish({"event": "large", "value": "x" * 600}))
        valid = {"event": "valid", "value": "x" * 250}
        self.assertTrue(self.server.publish(valid))
        self.assertFalse(self.server.publish(valid))
        await controlled.entered.wait()
        writer.transport.abort()
        controlled.fail = True
        controlled.release.set()
        await self.wait_until(lambda: not self.server.connected)
        reader2, writer2 = await self.connect()
        self.assertEqual((await self.read_event(reader2))["event"], "valid")
        await self.close_client(writer2)

    async def test_stop_cancels_a_required_result_waiting_for_capacity(self):
        await self.restart_with(max_queue_messages=1)
        _, writer = await self.connect()
        await self.wait_until(lambda: self.server.connected)
        controlled = self.control_server_writer()
        self.server.publish({"event": "occupies-capacity"})
        await controlled.entered.wait()
        task = asyncio.create_task(self.server._enqueue_required(b"y\n"))
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        await self.server.stop()
        self.assertTrue(task.cancelled() or task.done())
        writer.transport.abort()

    async def test_start_refuses_regular_file_symlink_and_symlink_parent(self):
        await self.server.stop()
        self.socket_path.write_text("owned by someone else", encoding="utf-8")
        with self.assertRaises(Exception):
            await self.server.start()
        self.assertEqual(self.socket_path.read_text(encoding="utf-8"), "owned by someone else")
        self.socket_path.unlink()
        target = Path(self.temp.name) / "target"
        target.write_text("target", encoding="utf-8")
        self.socket_path.symlink_to(target)
        with self.assertRaises(Exception):
            await self.server.start()
        self.assertTrue(self.socket_path.is_symlink())
        self.socket_path.unlink()
        self.socket_path.parent.rmdir()
        real_parent = Path(self.temp.name) / "real-parent"
        real_parent.mkdir()
        self.socket_path.parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaises(Exception):
            await self.server.start()
        self.assertTrue(self.socket_path.parent.is_symlink())

    async def test_stop_does_not_unlink_replaced_socket_inode(self):
        original = self.socket_path.lstat()
        self.socket_path.unlink()
        self.socket_path.write_text("replacement", encoding="utf-8")
        await self.server.stop()
        self.assertEqual(self.socket_path.read_text(encoding="utf-8"), "replacement")
        self.assertNotEqual(self.socket_path.lstat().st_ino, original.st_ino)

    async def test_partial_start_removes_only_the_socket_it_created(self):
        await self.server.stop()
        real_chmod = os.chmod

        def fail_after_bind(path, mode, **_kwargs):
            if Path(path) == self.socket_path:
                raise PermissionError("private chmod detail")
            real_chmod(path, mode)

        with patch("smartapp_runtime.infrastructure.ipc.server.os.chmod", side_effect=fail_after_bind):
            with self.assertRaises(Exception):
                await self.server.start()
        self.assertFalse(self.socket_path.exists())

    async def test_start_detects_path_replacement_during_permission_setup(self):
        await self.server.stop()
        real_chmod = os.chmod

        def replace_during_chmod(path, mode, **_kwargs):
            Path(path).unlink()
            Path(path).write_text("replacement", encoding="utf-8")
            real_chmod(path, mode)

        with patch("smartapp_runtime.infrastructure.ipc.server.os.chmod",
                   side_effect=replace_during_chmod):
            with self.assertRaises(Exception):
                await self.server.start()
        self.assertEqual(self.socket_path.read_text(encoding="utf-8"), "replacement")


if __name__ == "__main__":
    unittest.main()
