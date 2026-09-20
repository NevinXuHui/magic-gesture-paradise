import asyncio
import json
import socket
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from smartapp_runtime.bootstrap import RuntimeAssembly, build_runtime
from smartapp_runtime.config import (
    LimitConfig,
    LoggingConfig,
    NetworkConfig,
    PathsConfig,
    RuntimeConfig,
    TimeoutConfig,
)
from smartapp_runtime.domain.state import RuntimeState

from helpers.package_factory import MemoryDownloader, PackageBytes


def unused_port() -> int:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


class AgentClient:
    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self.events: List[Dict[str, Any]] = []

    async def close(self) -> None:
        self.writer.close()
        try:
            await asyncio.wait_for(self.writer.wait_closed(), 1.0)
        except (asyncio.TimeoutError, ConnectionError, OSError, RuntimeError):
            pass

    async def send(self, value: Dict[str, Any]) -> None:
        payload = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8") + b"\n"
        if len(payload) > 65536:
            raise AssertionError("test command exceeds client bound")
        self.writer.write(payload)
        await asyncio.wait_for(self.writer.drain(), 1.0)

    async def _read(self) -> Dict[str, Any]:
        line = await asyncio.wait_for(self.reader.readline(), 3.0)
        if not line or len(line) > 65536 or not line.endswith(b"\n"):
            raise AssertionError("invalid or closed Agent stream")
        value = json.loads(line)
        if type(value) is not dict:
            raise AssertionError("Agent event is not an object")
        return value

    async def wait_for(self, predicate: Callable[[Dict[str, Any]], bool]) -> Dict[str, Any]:
        for index, event in enumerate(self.events):
            if predicate(event):
                return self.events.pop(index)
        while True:
            event = await self._read()
            if predicate(event):
                return event
            self.events.append(event)

    async def result(self, request_id: str) -> Dict[str, Any]:
        return await self.wait_for(
            lambda value: value.get("event") == "command_result"
            and value.get("requestId") == request_id
        )

    async def command(self, value: Dict[str, Any]) -> Dict[str, Any]:
        await self.send(value)
        return await self.result(value["requestId"])


class RuntimeHarness:
    def __init__(self, package_map: Dict[str, bytes], *,
                 startup_timeout: float = 0.5,
                 max_queue_messages: int = 16) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "runtime"
        static_port = unused_port()
        backend_port = unused_port()
        while backend_port == static_port:
            backend_port = unused_port()
        self.config = RuntimeConfig(
            paths=PathsConfig(
                root=self.root,
                socket=self.root / "run" / "runtime.sock",
                log=self.root / "logs" / "runtime.jsonl",
                python_executable=Path(sys.executable).resolve(),
            ),
            network=NetworkConfig(
                static_host="127.0.0.1",
                static_port=static_port,
                backend_host="127.0.0.1",
                backend_port=backend_port,
            ),
            timeouts=TimeoutConfig(
                download=1.0,
                startup=startup_timeout,
                graceful_stop=0.15,
                sigterm=0.15,
                renderer=0.3,
            ),
            limits=replace(
                LimitConfig(),
                max_package_bytes=2 * 1024 * 1024,
                max_file_bytes=512 * 1024,
                max_unpacked_bytes=4 * 1024 * 1024,
                max_files=64,
                max_message_bytes=65536,
                max_queue_messages=max_queue_messages,
                max_queue_bytes=2 * 1024 * 1024,
            ),
            logging=LoggingConfig(level="ERROR", target="stderr"),
        )
        self.assembly: RuntimeAssembly = build_runtime(self.config)
        self.downloader = MemoryDownloader(package_map)
        self.assembly.downloader = self.downloader
        self.assembly.installer.downloader = self.downloader
        self.client: Optional[AgentClient] = None
        self.initial_tasks = set()

    async def start(self) -> AgentClient:
        self.initial_tasks = asyncio.all_tasks()
        await asyncio.wait_for(self.assembly.start(), 2.0)
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.config.paths.socket)), 1.0
        )
        self.client = AgentClient(reader, writer)
        return self.client

    async def stop(self) -> None:
        if self.client is not None:
            await self.client.close()
            self.client = None
        issues = []
        try:
            await asyncio.wait_for(self.assembly.stop(), 3.0)
        finally:
            await asyncio.sleep(0)
            if self.config.paths.socket.exists():
                issues.append("Agent socket still exists")
            if self.assembly.supervisor._active is not None:
                issues.append("backend process is still owned")
            probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                try:
                    probe.bind((self.config.network.static_host,
                                self.config.network.static_port))
                except OSError:
                    issues.append("static port cannot be rebound")
            finally:
                probe.close()
            leaked = [
                task for task in asyncio.all_tasks() - self.initial_tasks
                if task is not asyncio.current_task() and not task.done()
                and task.get_name().startswith((
                    "runtime-", "startup-", "lifecycle-", "agent-",
                    "backend-", "renderer-",
                ))
            ]
            if leaked:
                issues.append("runtime left live tasks: {0}".format(leaked))
            if self.root.exists():
                try:
                    if self.assembly.repository.load().state != RuntimeState.IDLE:
                        issues.append("runtime did not persist IDLE")
                except Exception as error:
                    issues.append("runtime state cannot be read: {0}".format(error))
            self.temp.cleanup()
            if issues:
                raise AssertionError("; ".join(issues))

    @staticmethod
    def start_command(request_id: str, session_id: str, app_id: str,
                      version: str, url: str, package: PackageBytes,
                      init_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "requestId": request_id,
            "command": "start_app",
            "sessionId": session_id,
            "appId": app_id,
            "version": version,
            "packageUrl": url,
            "packageSize": package.size,
            "sha256": package.sha256,
            "initData": init_data or {},
        }

    @staticmethod
    def cloud_command(request_id: str, session_id: str, seq: int,
                      data: Dict[str, Any], target: str = "auto",
                      data_type: str = "echo") -> Dict[str, Any]:
        return {
            "requestId": request_id,
            "command": "cloud_data",
            "sessionId": session_id,
            "seq": seq,
            "target": target,
            "dataType": data_type,
            "data": data,
        }

    @staticmethod
    def stop_command(request_id: str, session_id: str,
                     reason: str = "operator") -> Dict[str, Any]:
        return {
            "requestId": request_id,
            "command": "stop_app",
            "sessionId": session_id,
            "reason": reason,
        }

    async def wait_state(self, state: RuntimeState) -> Dict[str, Any]:
        if self.client is None:
            raise AssertionError("client is not connected")
        counter = 0

        async def poll() -> Dict[str, Any]:
            nonlocal counter
            while True:
                counter += 1
                result = await self.client.command({
                    "requestId": "status-{0}".format(counter),
                    "command": "get_status",
                })
                if result["state"] == state.value:
                    return result
                await asyncio.sleep(0)

        return await asyncio.wait_for(poll(), 2.0)
