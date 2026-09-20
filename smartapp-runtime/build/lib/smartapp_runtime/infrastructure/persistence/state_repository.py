import json
import os
import re
import stat
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Optional

from smartapp_runtime.domain.commands import (
    _expect_app_id, _expect_session_id, _expect_version, _validate_json_value,
)
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths, fsync_directory, recovery_error
from smartapp_runtime.ports.repository import BackendProcessIdentity, PersistedRuntimeState, PointerSnapshot


def _object(value: Any, keys: set, optional: Optional[set] = None) -> Dict[str, Any]:
    if type(value) is not dict or not keys <= value.keys() or value.keys() - keys - (optional or set()):
        raise ValueError()
    return value


def _integer(value: Any, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError()
    return value


def _text(value: Any) -> str:
    if type(value) is not str or not value or any(ord(char) < 32 for char in value):
        raise ValueError()
    value.encode("utf-8")
    return value


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def _decode(data: Any) -> PersistedRuntimeState:
    data = _object(data, {"schemaVersion", "state", "activeSession", "generation",
                          "backendProcess", "pointerSnapshot", "lastError"})
    if _integer(data["schemaVersion"]) != 1:
        raise ValueError()
    state = RuntimeState(_text(data["state"]))
    generation = _integer(data["generation"])
    session = None
    if data["activeSession"] is not None:
        value = _object(data["activeSession"], {"sessionId", "appId", "version", "generation"})
        session = Session(_expect_session_id(value["sessionId"]), _expect_app_id(value["appId"]),
                          _expect_version(value["version"]), _integer(value["generation"]))
    identity = None
    if data["backendProcess"] is not None:
        value = _object(data["backendProcess"], {"pid", "startTime", "commandMarker"})
        pid = _integer(value["pid"], 2)
        start = _text(value["startTime"])
        marker = _text(value["commandMarker"])
        if ((re.fullmatch(r"[0-9]+", start) is None and start != "non-linux")
                or not Path(marker).is_absolute() or ".." in Path(marker).parts):
            raise ValueError()
        identity = BackendProcessIdentity(pid, start, marker)
    snapshot = None
    if data["pointerSnapshot"] is not None:
        value = _object(data["pointerSnapshot"], {"appId", "current", "previous", "currentWeb"})
        snapshot = PointerSnapshot(_expect_app_id(value["appId"]),
            *(_text(value[key]) if value[key] is not None else None
              for key in ("current", "previous", "currentWeb")))
    error = data["lastError"]
    if error is not None:
        _object(error, {"code", "message"}, {"details"})
        ErrorCode(_text(error["code"]))
        if type(error["message"]) is not str:
            raise ValueError()
        if "details" in error and type(error["details"]) is not dict:
            raise ValueError()
        _validate_json_value(error, "lastError")
    return PersistedRuntimeState(1, state, session, generation, identity, snapshot, error)


def _encode(state: PersistedRuntimeState) -> dict:
    if not isinstance(state, PersistedRuntimeState) or not isinstance(state.state, RuntimeState):
        raise ValueError()
    identity, snapshot = state.backend_process, state.pointer_snapshot
    return {
        "schemaVersion": state.schema_version, "state": state.state.value,
        "activeSession": None if state.active_session is None else state.active_session.to_dict(),
        "generation": state.generation,
        "backendProcess": None if identity is None else {
            "pid": identity.pid, "startTime": identity.start_time, "commandMarker": identity.command_marker},
        "pointerSnapshot": None if snapshot is None else {
            "appId": snapshot.app_id, "current": snapshot.current,
            "previous": snapshot.previous, "currentWeb": snapshot.current_web},
        "lastError": state.last_error,
    }


class FileStateRepository:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self.paths = RuntimePaths.from_root(self.path.parent.parent)

    def _validate_file(self) -> None:
        self.paths.validate_parent(self.path)
        if os.path.lexists(str(self.path)) and not stat.S_ISREG(self.path.lstat().st_mode):
            raise recovery_error("state path is not a regular file")

    def load(self) -> PersistedRuntimeState:
        descriptor = None
        try:
            self._validate_file()
            try:
                descriptor = os.open(str(self.path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return PersistedRuntimeState()
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError()
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                descriptor = None
                return _decode(json.load(stream, object_pairs_hook=_unique_object))
        except (OSError, ValueError, TypeError, RecursionError, SmartAppError):
            raise recovery_error("cannot load valid runtime state") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def save(self, state: PersistedRuntimeState) -> None:
        temporary = self.path.with_name("." + self.path.name + "." + uuid.uuid4().hex)
        owned = None
        descriptor = None
        try:
            data = _encode(state)
            _decode(data)
            payload = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
            self._validate_file()
            descriptor = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
            info = os.fstat(descriptor)
            owned = (info.st_dev, info.st_ino)
            os.fchmod(descriptor, 0o640)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._validate_file()
            os.replace(str(temporary), str(self.path))
            owned = None
            fsync_directory(self.path.parent)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError, SmartAppError):
            raise recovery_error("cannot save runtime state") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if owned is not None:
                try:
                    self.paths.validate_parent(temporary)
                    info = temporary.lstat()
                    if stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == owned:
                        temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise recovery_error("cannot clean temporary state") from None

    def set_backend_process(self, identity: BackendProcessIdentity) -> None:
        self.save(replace(self.load(), backend_process=identity))

    def clear_backend_process(self) -> None:
        self.save(replace(self.load(), backend_process=None))
