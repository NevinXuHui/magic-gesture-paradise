import copy
import math
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Set

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError


_APP_ID_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
_VERSION_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]{0,63}")
_SESSION_ID_PATTERN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:-]{0,127}")
_SHA256_PATTERN = re.compile(r"[0-9A-Fa-f]{64}")


class StopReason(str, Enum):
    WAKE_WORD = "wake_word"
    CLOUD_STOP = "cloud_stop"
    REPLACE = "replace"
    RUNTIME_ERROR = "runtime_error"
    OPERATOR = "operator"


class MessageTarget(str, Enum):
    AUTO = "auto"
    PYTHON = "python"
    WEB = "web"
    BROADCAST = "broadcast"


def _validation_error(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.VALIDATION_ERROR, message)


def _expect_mapping(
    value: Any, required_keys: Iterable[str], allowed_keys: Iterable[str]
) -> Dict[str, Any]:
    if type(value) is not dict:
        raise _validation_error("command must be an object")

    required = set(required_keys)
    missing = required.difference(value)
    if missing:
        raise _validation_error("missing required field: {0}".format(next(iter(missing))))

    allowed = set(allowed_keys)
    unknown = set(value).difference(allowed)
    if unknown:
        raise _validation_error("unknown field: {0}".format(next(iter(unknown))))
    return value


def _expect_command(value: Any, command: str, required_keys: Set[str], allowed_keys: Set[str]) -> Dict[str, Any]:
    payload = _expect_mapping(value, required_keys, allowed_keys)
    if type(payload["command"]) is not str or payload["command"] != command:
        raise _validation_error("command must be {0}".format(command))
    return payload


def _expect_string(value: Any, field: str, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value):
        raise _validation_error("{0} must be a non-empty string".format(field))
    return value


def _expect_pattern(value: Any, field: str, pattern: re.Pattern) -> str:
    value = _expect_string(value, field)
    if pattern.fullmatch(value) is None:
        raise _validation_error("invalid {0}".format(field))
    return value


def _expect_request_id(value: Any) -> str:
    value = _expect_string(value, "requestId")
    if not 1 <= len(value) <= 128 or any(not 0x20 <= ord(char) <= 0x7E for char in value):
        raise _validation_error("invalid requestId")
    return value


def _expect_app_id(value: Any) -> str:
    return _expect_pattern(value, "appId", _APP_ID_PATTERN)


def _expect_version(value: Any) -> str:
    return _expect_pattern(value, "version", _VERSION_PATTERN)


def _expect_session_id(value: Any) -> str:
    return _expect_pattern(value, "sessionId", _SESSION_ID_PATTERN)


def _expect_json_object(value: Any, field: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise _validation_error("{0} must be an object".format(field))
    _validate_json_value(value, field)
    return copy.deepcopy(value)


def _validate_json_value(value: Any, field: str) -> None:
    value_type = type(value)
    if value is None or value_type in (bool, int, str):
        return
    if value_type is float:
        if math.isfinite(value):
            return
        raise _validation_error("{0} must contain only finite JSON values".format(field))
    if value_type is list:
        for item in value:
            _validate_json_value(item, field)
        return
    if value_type is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _validation_error("{0} object keys must be strings".format(field))
            _validate_json_value(item, field)
        return
    raise _validation_error("{0} must contain only JSON values".format(field))


def _expect_enum(value: Any, field: str, enum_type: Any) -> Any:
    _expect_string(value, field)
    try:
        return enum_type(value)
    except ValueError:
        raise _validation_error("invalid {0}".format(field))


@dataclass(frozen=True)
class StartApp:
    request_id: str
    session_id: str
    app_id: str
    version: str
    package_url: str
    package_size: int
    sha256: str
    init_data: Dict[str, Any]

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "StartApp":
        required = {
            "requestId", "command", "sessionId", "appId", "version", "packageUrl",
            "packageSize", "sha256", "initData",
        }
        payload = _expect_command(value, "start_app", required, required)
        package_size = payload["packageSize"]
        if type(package_size) is not int or package_size <= 0:
            raise _validation_error("packageSize must be a positive integer")
        sha256 = _expect_string(payload["sha256"], "sha256")
        if _SHA256_PATTERN.fullmatch(sha256) is None:
            raise _validation_error("invalid sha256")
        return cls(
            request_id=_expect_request_id(payload["requestId"]),
            session_id=_expect_session_id(payload["sessionId"]),
            app_id=_expect_app_id(payload["appId"]),
            version=_expect_version(payload["version"]),
            package_url=_expect_string(payload["packageUrl"], "packageUrl"),
            package_size=package_size,
            sha256=sha256.lower(),
            init_data=_expect_json_object(payload["initData"], "initData"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requestId": self.request_id,
            "command": "start_app",
            "sessionId": self.session_id,
            "appId": self.app_id,
            "version": self.version,
            "packageUrl": self.package_url,
            "packageSize": self.package_size,
            "sha256": self.sha256,
            "initData": copy.deepcopy(self.init_data),
        }


@dataclass(frozen=True)
class StopApp:
    request_id: str
    session_id: str
    reason: StopReason

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "StopApp":
        required = {"requestId", "command", "sessionId", "reason"}
        payload = _expect_command(value, "stop_app", required, required)
        return cls(
            request_id=_expect_request_id(payload["requestId"]),
            session_id=_expect_session_id(payload["sessionId"]),
            reason=_expect_enum(payload["reason"], "reason", StopReason),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requestId": self.request_id,
            "command": "stop_app",
            "sessionId": self.session_id,
            "reason": self.reason.value,
        }


@dataclass(frozen=True)
class CloudData:
    request_id: str
    session_id: str
    seq: int
    target: MessageTarget
    data_type: Optional[str]
    trigger: Optional[str]
    data: Dict[str, Any]

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "CloudData":
        required = {"requestId", "command", "sessionId", "seq", "data"}
        allowed = required.union({"target", "dataType", "trigger"})
        payload = _expect_command(value, "cloud_data", required, allowed)
        seq = payload["seq"]
        if type(seq) is not int or not 0 <= seq <= (2 ** 63 - 1):
            raise _validation_error("seq must be an integer in range")
        target = MessageTarget.AUTO
        if "target" in payload:
            target = _expect_enum(payload["target"], "target", MessageTarget)
        data_type = None
        if "dataType" in payload:
            data_type = _expect_string(payload["dataType"], "dataType")
        trigger = None
        if "trigger" in payload:
            trigger = _expect_string(payload["trigger"], "trigger")
        return cls(
            request_id=_expect_request_id(payload["requestId"]),
            session_id=_expect_session_id(payload["sessionId"]),
            seq=seq,
            target=target,
            data_type=data_type,
            trigger=trigger,
            data=_expect_json_object(payload["data"], "data"),
        )

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "requestId": self.request_id,
            "command": "cloud_data",
            "sessionId": self.session_id,
            "seq": self.seq,
            "target": self.target.value,
            "data": copy.deepcopy(self.data),
        }
        if self.data_type is not None:
            result["dataType"] = self.data_type
        if self.trigger is not None:
            result["trigger"] = self.trigger
        return result


@dataclass(frozen=True)
class GetStatus:
    request_id: str

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "GetStatus":
        required = {"requestId", "command"}
        payload = _expect_command(value, "get_status", required, required)
        return cls(request_id=_expect_request_id(payload["requestId"]))

    def to_dict(self) -> Dict[str, Any]:
        return {"requestId": self.request_id, "command": "get_status"}
