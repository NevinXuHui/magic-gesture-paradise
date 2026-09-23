"""Persistent SmartApp Runtime Agent connection for commands and app events."""

import json
import queue
import socket
import threading
import time


class RuntimeAgentLink:
    def __init__(self, socket_path, on_event, logger):
        self.socket_path = socket_path
        self.on_event = on_event
        self.logger = logger
        self._stop = threading.Event()
        self._commands = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="smartapp-runtime-agent", daemon=True)

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3)

    def send(self, command, timeout):
        if self._stop.is_set():
            return None
        response = queue.Queue(maxsize=1)
        self._commands.put((command, response, time.monotonic() + timeout))
        try:
            return response.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self):
        last_warning = None
        last_logged_at = 0.0
        while not self._stop.is_set():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(1.0)
                    connection.connect(self.socket_path)
                    connection.settimeout(0.2)
                    self._serve(connection)
            except (OSError, ValueError) as exc:
                if not self._stop.is_set():
                    now = time.monotonic()
                    warning = str(exc)
                    if warning != last_warning or now - last_logged_at >= 30.0:
                        self.logger.warning(f"[smartapp_runtime] Agent connection: {exc}")
                        last_warning = warning
                        last_logged_at = now
                    self._stop.wait(1.0)

    def _serve(self, connection):
        pending = {}
        buffer = b""
        try:
            while not self._stop.is_set():
                while True:
                    try:
                        command, response, deadline = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if time.monotonic() >= deadline:
                        response.put_nowait(None)
                        continue
                    request_id = command["requestId"]
                    pending[request_id] = response
                    connection.sendall((json.dumps(command, ensure_ascii=False) + "\n").encode("utf-8"))
                try:
                    chunk = connection.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    raise ConnectionError("Runtime disconnected")
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if len(line) + 1 > 1048576:
                        raise ValueError("Runtime frame is too large")
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        raise ValueError("Runtime frame is not an object")
                    if message.get("event") == "command_result":
                        request_id = message.get("requestId")
                        if request_id == "connection" and message.get("ok") is False:
                            raise ConnectionError("Runtime Agent connection is occupied")
                        response = pending.pop(request_id, None)
                        if response is not None:
                            response.put_nowait(message)
                    elif message.get("event") == "app_data":
                        try:
                            self.on_event(message)
                        except Exception as exc:
                            self.logger.error(f"[smartapp_runtime] app_data handling failed: {exc}")
                if len(buffer) > 1048576:
                    raise ValueError("Runtime frame is too large")
        finally:
            for response in pending.values():
                response.put_nowait(None)
