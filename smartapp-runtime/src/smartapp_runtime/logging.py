import datetime
import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit, urlunsplit

from smartapp_runtime.config import RuntimeConfig
from smartapp_runtime.domain.errors import sanitize_message
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.infrastructure.persistence.secure_io import open_directory, open_file


_CORRELATION_FIELDS = ("requestId", "sessionId", "appId", "state", "errorCode")


def sanitize_url(value: str) -> str:
    """Return a useful URL identity without credentials or request secrets."""
    if not isinstance(value, str) or not value or any(
        character.isspace() or ord(character) < 0x20 for character in value
    ):
        return "<invalid-url>"
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return "<invalid-url>"
        host = parsed.hostname
        if ":" in host:
            host = "[" + host + "]"
        port = parsed.port
        authority = host if port is None else "{0}:{1}".format(host, port)
        return urlunsplit((parsed.scheme, authority, parsed.path, "", ""))
    except (TypeError, ValueError, UnicodeError):
        return "<invalid-url>"


def _safe_text(value: Any) -> str:
    try:
        return sanitize_message(str(value))
    except BaseException:
        return "<unprintable>"


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _safe_record_message(record)
        timestamp = datetime.datetime.fromtimestamp(
            record.created, datetime.timezone.utc
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        output: Dict[str, str] = {
            "timestamp": timestamp,
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        for name in _CORRELATION_FIELDS:
            if hasattr(record, name):
                output[name] = _safe_text(getattr(record, name))
        return json.dumps(
            output, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )


def _safe_record_message(record: logging.LogRecord) -> str:
    if type(record.msg) is not str:
        return "<structured message redacted>"
    if not record.args:
        return sanitize_message(record.msg)
    if type(record.args) is tuple and all(
        type(value) in (str, int, float, bool, type(None)) for value in record.args
    ):
        try:
            return sanitize_message(record.msg % record.args)
        except (TypeError, ValueError):
            return "<unprintable message>"
    return sanitize_message(record.msg) + " <structured arguments redacted>"


class _SafeStreamHandler(logging.StreamHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        return


class _OwnedStreamHandler(_SafeStreamHandler):
    def close(self) -> None:
        try:
            if self.stream is not None:
                self.stream.close()
                self.stream = None
        finally:
            super().close()


def _file_handler(path: Path) -> logging.Handler:
    parent = path.parent
    parent_descriptor = open_directory(parent, create_leaf=True)
    descriptor = -1
    try:
        descriptor = open_file(
            parent_descriptor, path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("log target is not a regular file")
        os.fchmod(descriptor, 0o640)
        stream = os.fdopen(descriptor, "a", encoding="utf-8")
        descriptor = -1
        return _OwnedStreamHandler(stream)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)


def _normalized_log_path(config: RuntimeConfig) -> Path:
    root = Path(config.paths.root).absolute()
    path = Path(config.paths.log).absolute()
    try:
        relative = path.relative_to(root)
    except ValueError:
        return path
    return RuntimePaths.from_root(root).root / relative


def configure_logging(config: RuntimeConfig) -> None:
    if not isinstance(config, RuntimeConfig):
        raise TypeError("config must be RuntimeConfig")
    root = logging.getLogger()
    old_handlers = list(root.handlers)
    handler = (
        _SafeStreamHandler(sys.stderr)
        if config.logging.target == "stderr"
        else _file_handler(_normalized_log_path(config))
    )
    handler.setFormatter(_JsonFormatter())
    root.handlers = [handler]
    root.setLevel(getattr(logging, config.logging.level))
    root.propagate = False
    for old_handler in old_handlers:
        old_handler.close()
