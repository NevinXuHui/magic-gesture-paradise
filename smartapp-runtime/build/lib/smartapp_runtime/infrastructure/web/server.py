import ipaddress
import math
import mimetypes
import re
import socket
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Optional, Tuple
from urllib.parse import unquote_to_bytes

from smartapp_runtime.infrastructure.persistence.pointers import AtomicPointers

_INVALID_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DRIVE_PATH = re.compile(r"^[A-Za-z]:")


class _LoopbackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, handler, owner):
        self.owner = owner
        self.address_family = (
            socket.AF_INET6 if ipaddress.ip_address(address[0]).version == 6 else socket.AF_INET
        )
        ThreadingHTTPServer.__init__(self, address, handler)


class _StaticRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def handle_one_request(self):
        self.raw_requestline = self.rfile.readline(65537)
        if len(self.raw_requestline) > 65536:
            self.requestline = ""
            self.request_version = ""
            self.command = ""
            self.send_error(414)
            return
        if not self.raw_requestline:
            self.close_connection = True
            return
        self._raw_target = self._request_target_from_raw_requestline()
        if not self.parse_request():
            return
        self._dispatch_request()
        self.wfile.flush()

    def _request_target_from_raw_requestline(self):
        parts = self.raw_requestline.decode("iso-8859-1").split()
        if len(parts) not in (2, 3):
            return None
        return parts[1]

    def _dispatch_request(self):
        raw_path = self._raw_url_path()
        send_body = self.command == "GET"
        if self.command in ("GET", "HEAD") and raw_path == "/healthz":
            self._send_response(200, "text/plain; charset=utf-8", b"ok\n", send_body)
            return
        try:
            root = self.server.owner.current_web_root()
        except Exception:
            root = None
        if self.command not in ("GET", "HEAD"):
            self._method_not_allowed()
            return
        self._handle_request(root, send_body)

    def send_error(self, code, message=None, explain=None):
        self._send_response(code, "text/plain; charset=utf-8", b"", send_body=False)

    def _method_not_allowed(self):
        self._send_response(
            405,
            "text/plain; charset=utf-8",
            b"",
            send_body=False,
            extra_headers=(("Allow", "GET, HEAD"),),
        )

    def _handle_request(self, root, send_body):
        try:
            if root is None:
                raise ValueError("no active web root")
            file_path = self._resolve_file(root)
            with file_path.open("rb") as source:
                content = source.read()
        except Exception:
            self._send_response(404, "text/plain; charset=utf-8", b"", send_body=False)
            return
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        self._send_response(200, content_type, content, send_body)

    def _resolve_file(self, root):
        relative = self._request_relative_path()
        resolved_root = root.resolve(strict=True)
        candidate = resolved_root.joinpath(*relative.parts)
        if candidate.is_dir():
            candidate = candidate / "index.html"
        resolved_candidate = candidate.resolve(strict=False)
        resolved_candidate.relative_to(resolved_root)
        try:
            mode = resolved_candidate.stat().st_mode
        except OSError:
            raise ValueError("not a file")
        if not stat.S_ISREG(mode):
            raise ValueError("not a regular file")
        return resolved_candidate

    def _request_relative_path(self):
        raw_path = self._raw_url_path()
        if not raw_path.startswith("/") or _INVALID_PERCENT_ESCAPE.search(raw_path):
            raise ValueError("invalid URL path")
        decoded = unquote_to_bytes(raw_path).decode("utf-8", "strict")
        if "\x00" in decoded or "\\" in decoded or not decoded.startswith("/"):
            raise ValueError("unsafe URL path")
        relative_raw = decoded[1:]
        if relative_raw.startswith("/") or _DRIVE_PATH.match(relative_raw):
            raise ValueError("unsafe URL path")
        segments = relative_raw.split("/")
        for index, segment in enumerate(segments):
            if segment in (".", ".."):
                raise ValueError("unsafe URL path")
            if not segment and index != len(segments) - 1:
                raise ValueError("unsafe URL path")
        relative = PurePosixPath(relative_raw)
        if relative.is_absolute():
            raise ValueError("unsafe URL path")
        return relative

    def _raw_url_path(self):
        if self._raw_target is None:
            raise ValueError("invalid URL path")
        return self._raw_target.split("?", 1)[0].split("#", 1)[0]

    def _send_response(self, code, content_type, body, send_body, extra_headers=()):
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        if send_body and body:
            self.wfile.write(body)


class StaticWebServer:
    def __init__(
        self,
        pointers: AtomicPointers,
        host: str,
        port: int,
        shutdown_timeout: float,
    ) -> None:
        self._validate_loopback_host(host)
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("static port must be an integer in 0..65535")
        if (type(shutdown_timeout) not in (int, float)
                or not math.isfinite(shutdown_timeout) or shutdown_timeout <= 0):
            raise ValueError("shutdown timeout must be a finite positive number")
        self._pointers = pointers
        self._host = host
        self._port = port
        self._shutdown_timeout = float(shutdown_timeout)
        self._server = None
        self._thread = None
        self._lock = threading.RLock()

    @staticmethod
    def _validate_loopback_host(host: str) -> None:
        if not isinstance(host, str):
            raise ValueError("static host must be a loopback IP literal")
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError()
        except ValueError:
            raise ValueError("static host must be a loopback IP literal") from None

    @property
    def address(self) -> Tuple[str, int]:
        with self._lock:
            if self._server is None:
                raise RuntimeError("static web server is not started")
            host, port = self._server.server_address[:2]
            return host, port

    @property
    def owns_resources(self) -> bool:
        with self._lock:
            return self._server is not None or self._thread is not None

    def start(self) -> None:
        with self._lock:
            if self._server is not None or self._thread is not None:
                raise RuntimeError("static web server is already started")
            server = _LoopbackHTTPServer((self._host, self._port), _StaticRequestHandler, self)
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05})
            thread.daemon = True
            self._server = server
            self._thread = thread
            try:
                thread.start()
            except BaseException:
                try:
                    alive = self._thread_alive(thread)
                    if alive is True:
                        try:
                            server.shutdown()
                        except BaseException:
                            pass
                        try:
                            thread.join(self._shutdown_timeout)
                        except BaseException:
                            pass
                finally:
                    try:
                        server.server_close()
                    except BaseException:
                        pass
                    self._retain_unreleased(server, thread)
                raise

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            if server is None and thread is None:
                return
            failure = None
            try:
                alive = self._thread_alive(thread)
                if alive is True and server is not None:
                    try:
                        server.shutdown()
                    except BaseException as error:
                        failure = error
                if server is not None:
                    try:
                        server.server_close()
                    except BaseException as error:
                        if failure is None:
                            failure = error
                if self._thread_alive(thread) is True:
                    try:
                        thread.join(self._shutdown_timeout)
                    except BaseException as error:
                        if failure is None:
                            failure = error
            finally:
                self._retain_unreleased(server, thread)
            if failure is None and (self._server is not None or self._thread is not None):
                failure = RuntimeError("static web server cleanup is incomplete")
            if failure is not None:
                raise failure

    @staticmethod
    def _thread_alive(thread) -> Optional[bool]:
        if thread is None:
            return False
        try:
            return thread.is_alive()
        except BaseException:
            return None

    @staticmethod
    def _socket_closed(server) -> bool:
        if server is None:
            return True
        try:
            return server.socket.fileno() < 0
        except BaseException:
            return False

    def _retain_unreleased(self, server, thread) -> None:
        alive = self._thread_alive(thread)
        self._thread = None if alive is False else thread
        closed = self._socket_closed(server)
        self._server = None if closed and self._thread is None else server

    def current_web_root(self) -> Optional[Path]:
        return self._pointers.current_web_target()
