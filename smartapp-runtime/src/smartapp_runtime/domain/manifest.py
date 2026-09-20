import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional

from smartapp_runtime.domain.commands import (
    MessageTarget,
    _expect_app_id,
    _expect_version,
)
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError


def _manifest_error(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.MANIFEST_INVALID, message)


def _expect_mapping(
    value: Any, field: str, required_keys: Iterable[str], allowed_keys: Iterable[str]
) -> Dict[str, Any]:
    if type(value) is not dict:
        raise _manifest_error("{0} must be an object".format(field))
    required = set(required_keys)
    missing = required.difference(value)
    if missing:
        raise _manifest_error(
            "{0} missing required field: {1}".format(field, next(iter(missing)))
        )
    allowed = set(allowed_keys)
    unknown = set(value).difference(allowed)
    if unknown:
        raise _manifest_error(
            "{0} unknown field: {1}".format(field, next(iter(unknown)))
        )
    return value


def _expect_manifest_identifier(value: Any, field: str) -> str:
    try:
        if field == "appId":
            return _expect_app_id(value)
        return _expect_version(value)
    except SmartAppError as error:
        raise _manifest_error(error.message)


def _expect_bool(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise _manifest_error("{0} must be a boolean".format(field))
    return value


def _expect_entry(value: Any, field: str) -> str:
    if type(value) is not str or not value:
        raise _manifest_error("{0} must be a non-empty path string".format(field))
    if "\x00" in value or value.startswith("/") or value.endswith("/") or "//" in value:
        raise _manifest_error("{0} must be a relative POSIX path".format(field))
    path = PurePosixPath(value)
    if path.is_absolute() or any(segment in ("", ".", "..") for segment in value.split("/")):
        raise _manifest_error("{0} must be a relative POSIX path".format(field))
    return value


def _expect_default_target(value: Any) -> MessageTarget:
    if type(value) is not str:
        raise _manifest_error("routing.defaultTarget must be a string")
    try:
        target = MessageTarget(value)
    except ValueError:
        raise _manifest_error("invalid routing.defaultTarget")
    return target


@dataclass(frozen=True)
class ComponentConfig:
    enabled: bool
    entry: str


@dataclass(frozen=True)
class BackendConfig(ComponentConfig):
    dynamic_service: bool


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    app_id: str
    version: str
    web: ComponentConfig
    backend: BackendConfig
    default_target: MessageTarget

    @classmethod
    def from_dict(
        cls,
        value: Dict[str, Any],
        expected_app_id: Optional[str] = None,
        expected_version: Optional[str] = None,
    ) -> "Manifest":
        required = {"schemaVersion", "appId", "version", "web", "backend", "routing"}
        payload = _expect_mapping(value, "manifest", required, required)
        schema_version = payload["schemaVersion"]
        if type(schema_version) is not int or schema_version != 1:
            raise SmartAppError(
                ErrorCode.UNSUPPORTED_SCHEMA,
                "schemaVersion must be exactly 1",
            )
        app_id = _expect_manifest_identifier(payload["appId"], "appId")
        version = _expect_manifest_identifier(payload["version"], "version")
        if expected_app_id is not None:
            expected_app_id = _expect_manifest_identifier(expected_app_id, "appId")
            if app_id != expected_app_id:
                raise _manifest_error("manifest appId does not match expected appId")
        if expected_version is not None:
            expected_version = _expect_manifest_identifier(expected_version, "version")
            if version != expected_version:
                raise _manifest_error("manifest version does not match expected version")

        web_payload = _expect_mapping(
            payload["web"], "web", {"enabled", "entry"}, {"enabled", "entry"}
        )
        backend_payload = _expect_mapping(
            payload["backend"],
            "backend",
            {"enabled", "entry", "dynamicService"},
            {"enabled", "entry", "dynamicService"},
        )
        routing_payload = _expect_mapping(
            payload["routing"],
            "routing",
            {"defaultTarget"},
            {"defaultTarget"},
        )
        web = ComponentConfig(
            enabled=_expect_bool(web_payload["enabled"], "web.enabled"),
            entry=_expect_entry(web_payload["entry"], "web.entry"),
        )
        backend = BackendConfig(
            enabled=_expect_bool(backend_payload["enabled"], "backend.enabled"),
            entry=_expect_entry(backend_payload["entry"], "backend.entry"),
            dynamic_service=_expect_bool(
                backend_payload["dynamicService"], "backend.dynamicService"
            ),
        )
        default_target = _expect_default_target(routing_payload["defaultTarget"])

        if not web.enabled and not backend.enabled:
            raise _manifest_error("manifest has no enabled component")
        if web.enabled and not backend.enabled and default_target != MessageTarget.WEB:
            raise _manifest_error("web-only manifest requires defaultTarget=web")
        if backend.enabled and not web.enabled and default_target != MessageTarget.PYTHON:
            raise _manifest_error("Python-only manifest requires defaultTarget=python")
        if web.enabled and backend.enabled and default_target not in (
            MessageTarget.WEB,
            MessageTarget.PYTHON,
        ):
            raise _manifest_error("Hybrid manifest requires an explicit target")
        if backend.dynamic_service and not backend.enabled:
            raise _manifest_error("dynamicService requires an enabled backend")
        return cls(
            schema_version=schema_version,
            app_id=app_id,
            version=version,
            web=web,
            backend=backend,
            default_target=default_target,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "appId": self.app_id,
            "version": self.version,
            "web": {"enabled": self.web.enabled, "entry": self.web.entry},
            "backend": {
                "enabled": self.backend.enabled,
                "entry": self.backend.entry,
                "dynamicService": self.backend.dynamic_service,
            },
            "routing": {"defaultTarget": self.default_target.value},
        }


def load_manifest(
    path: Any,
    expected_app_id: Optional[str] = None,
    expected_version: Optional[str] = None,
) -> Manifest:
    try:
        raw_value = Path(path).read_text(encoding="utf-8")
        value = json.loads(raw_value)
    except (OSError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise _manifest_error("unable to load manifest: {0}".format(error))
    if type(value) is not dict:
        raise _manifest_error("manifest must be an object")
    return Manifest.from_dict(value, expected_app_id, expected_version)
