import re
import unicodedata
from enum import Enum
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit


class ErrorCode(str, Enum):
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    SESSION_CONFLICT = "SESSION_CONFLICT"
    SESSION_MISMATCH = "SESSION_MISMATCH"
    SEQ_OUT_OF_ORDER = "SEQ_OUT_OF_ORDER"
    QUEUE_FULL = "QUEUE_FULL"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    PACKAGE_TOO_LARGE = "PACKAGE_TOO_LARGE"
    SIZE_MISMATCH = "SIZE_MISMATCH"
    HASH_MISMATCH = "HASH_MISMATCH"
    ARCHIVE_UNSAFE = "ARCHIVE_UNSAFE"
    MANIFEST_INVALID = "MANIFEST_INVALID"
    INSTALL_CONFLICT = "INSTALL_CONFLICT"
    PORT_IN_USE = "PORT_IN_USE"
    START_TIMEOUT = "START_TIMEOUT"
    BACKEND_EXITED = "BACKEND_EXITED"
    BACKEND_PROTOCOL_ERROR = "BACKEND_PROTOCOL_ERROR"
    RENDERER_FAILED = "RENDERER_FAILED"
    UPSTREAM_QUEUE_FULL = "UPSTREAM_QUEUE_FULL"
    RECOVERY_FAILED = "RECOVERY_FAILED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_URL_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")


def _redact_malformed_url(raw_url: str) -> str:
    scheme, _, remainder = raw_url.partition("://")
    without_query_or_fragment = re.split(r"[?#]", remainder, maxsplit=1)[0]
    authority, separator, path = without_query_or_fragment.partition("/")
    safe_authority = authority.rsplit("@", 1)[-1]
    safe_path = separator + path if separator else ""
    return scheme + "://" + safe_authority + safe_path


def _redact_url(match: re.Match) -> str:
    raw_url = match.group(0)
    try:
        parsed = urlsplit(raw_url)
    except ValueError:
        return _redact_malformed_url(raw_url)
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def sanitize_message(value: str) -> str:
    sanitized = "".join(
        " " if unicodedata.category(character) == "Cc" else character
        for character in value
    )
    sanitized = _URL_PATTERN.sub(_redact_url, sanitized).strip()
    return sanitized[:512]


class SmartAppError(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.code: ErrorCode = code
        self.message: str = sanitize_message(message)
        self.details: Dict[str, Any] = {
            key: sanitize_message(value) if isinstance(value, str) else value
            for key, value in (details or {}).items()
        }
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {"code": self.code.value, "message": self.message}
        if self.details:
            result["details"] = self.details.copy()
        return result
