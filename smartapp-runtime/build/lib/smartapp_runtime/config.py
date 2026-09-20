import ipaddress
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple, Union

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError


_DEFAULT_ROOT = Path("/data/smartapp")
_DEFAULT_ENV_PASSTHROUGH = (
    "PATH",
    "LANG",
    "LC_ALL",
    "ROS_DOMAIN_ID",
    "RMW_IMPLEMENTATION",
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class PathsConfig:
    root: Path = _DEFAULT_ROOT
    socket: Path = Path("/data/smartapp/run/runtime.sock")
    log: Path = Path("/data/smartapp/logs/runtime.jsonl")
    python_executable: Path = Path("/usr/bin/python3")


@dataclass(frozen=True)
class NetworkConfig:
    static_host: str = "127.0.0.1"
    static_port: int = 18080
    backend_host: str = "127.0.0.1"
    backend_port: int = 18081


@dataclass(frozen=True)
class TimeoutConfig:
    download: float = 60.0
    startup: float = 15.0
    graceful_stop: float = 3.0
    sigterm: float = 3.0
    renderer: float = 15.0


@dataclass(frozen=True)
class LimitConfig:
    max_package_bytes: int = 536870912
    max_file_bytes: int = 536870912
    max_unpacked_bytes: int = 1073741824
    max_files: int = 10000
    max_path_length: int = 240
    max_message_bytes: int = 1048576
    max_queue_messages: int = 128
    max_queue_bytes: int = 4194304


@dataclass(frozen=True)
class ProcessConfig:
    env_passthrough: Tuple[str, ...] = _DEFAULT_ENV_PASSTHROUGH
    protocol_violation_limit: int = 5


@dataclass(frozen=True)
class RendererConfig:
    kind: str = "fake"
    load_argv: Tuple[str, ...] = ()
    send_argv: Tuple[str, ...] = ()
    stop_argv: Tuple[str, ...] = ()
    restore_argv: Tuple[str, ...] = ()


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    target: str = "stderr"


@dataclass(frozen=True)
class RuntimeConfig:
    paths: PathsConfig = field(default_factory=PathsConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    limits: LimitConfig = field(default_factory=LimitConfig)
    process: ProcessConfig = field(default_factory=ProcessConfig)
    renderer: RendererConfig = field(default_factory=RendererConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


class ConfigError(SmartAppError):
    def __init__(
        self, message: str, details: Optional[Mapping[str, Any]] = None
    ) -> None:
        super().__init__(ErrorCode.VALIDATION_ERROR, message, details)


_SECTION_FIELDS = {
    "paths": {"root", "socket", "log", "python_executable"},
    "network": {"static_host", "static_port", "backend_host", "backend_port"},
    "timeouts": {"download", "startup", "graceful_stop", "sigterm", "renderer"},
    "limits": {
        "max_package_bytes",
        "max_file_bytes",
        "max_unpacked_bytes",
        "max_files",
        "max_path_length",
        "max_message_bytes",
        "max_queue_messages",
        "max_queue_bytes",
    },
    "process": {"env_passthrough", "protocol_violation_limit"},
    "renderer": {"kind", "load_argv", "send_argv", "stop_argv", "restore_argv"},
    "logging": {"level", "target"},
}


def _require_string(section, key, value):
    if not isinstance(value, str) or not value:
        raise ConfigError("{0}.{1} must be a non-empty string".format(section, key))
    return value


def _require_positive_integer(section, key, value):
    if type(value) is not int or value <= 0:
        raise ConfigError("{0}.{1} must be a positive integer".format(section, key))
    return value


def _require_timeout(key, value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ConfigError("timeouts.{0} must be a finite positive number".format(key))
    return value


def _require_argv(key, value):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ConfigError("renderer.{0} must be an array of non-empty strings".format(key))
    return tuple(value)


def _existing_parent(path):
    parent = path.parent
    while not parent.exists():
        next_parent = parent.parent
        if next_parent == parent:
            return parent
        parent = next_parent
    return parent


def _load_paths(values):
    default = PathsConfig()
    root = Path(_require_string("paths", "root", values.get("root", str(default.root))))
    if root.exists() and not root.is_dir():
        raise ConfigError("paths.root must be a directory")
    if not root.exists() and not os.access(str(_existing_parent(root)), os.W_OK):
        raise ConfigError("paths.root nearest existing parent must be writable")
    socket = Path(
        _require_string("paths", "socket", values["socket"])
        if "socket" in values
        else str(root / "run/runtime.sock")
    )
    log = Path(
        _require_string("paths", "log", values["log"])
        if "log" in values
        else str(root / "logs/runtime.jsonl")
    )
    executable = Path(
        _require_string(
            "paths", "python_executable", values.get("python_executable", str(default.python_executable))
        )
    )
    return PathsConfig(root=root, socket=socket, log=log, python_executable=executable)


def _load_network(values):
    default = NetworkConfig()
    loaded = {}
    for key in ("static_host", "backend_host"):
        host = _require_string("network", key, values.get(key, getattr(default, key)))
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise ConfigError("network.{0} must be a loopback IP literal".format(key))
        loaded[key] = host
    for key in ("static_port", "backend_port"):
        port = values.get(key, getattr(default, key))
        if type(port) is not int or not 1 <= port <= 65535:
            raise ConfigError("network.{0} must be an integer in 1..65535".format(key))
        loaded[key] = port
    return NetworkConfig(**loaded)


def _load_timeouts(values):
    default = TimeoutConfig()
    return TimeoutConfig(
        **{
            key: _require_timeout(key, values.get(key, getattr(default, key)))
            for key in _SECTION_FIELDS["timeouts"]
        }
    )


def _load_limits(values):
    default = LimitConfig()
    return LimitConfig(
        **{
            key: _require_positive_integer("limits", key, values.get(key, getattr(default, key)))
            for key in _SECTION_FIELDS["limits"]
        }
    )


def _load_process(values):
    default = ProcessConfig()
    environment = values.get("env_passthrough", list(default.env_passthrough))
    if not isinstance(environment, list) or any(
        not isinstance(name, str) or not name or not _ENVIRONMENT_NAME.match(name)
        for name in environment
    ) or len(environment) != len(set(environment)):
        raise ConfigError("process.env_passthrough must contain unique environment variable names")
    return ProcessConfig(
        env_passthrough=tuple(environment),
        protocol_violation_limit=_require_positive_integer(
            "process",
            "protocol_violation_limit",
            values.get("protocol_violation_limit", default.protocol_violation_limit),
        ),
    )


def _load_renderer(values):
    default = RendererConfig()
    kind = _require_string("renderer", "kind", values.get("kind", default.kind))
    if kind not in ("fake", "command"):
        raise ConfigError("renderer.kind must be fake or command")
    argv = {
        key: _require_argv(key, values.get(key, list(getattr(default, key))))
        for key in ("load_argv", "send_argv", "stop_argv", "restore_argv")
    }
    if kind == "command" and any(not argv[key] for key in argv):
        raise ConfigError("command renderer requires all argv fields")
    return RendererConfig(kind=kind, **argv)


def _load_logging(values):
    default = LoggingConfig()
    level = _require_string("logging", "level", values.get("level", default.level)).upper()
    if level not in ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"):
        raise ConfigError("logging.level is invalid")
    target = _require_string("logging", "target", values.get("target", default.target))
    if target not in ("stderr", "file"):
        raise ConfigError("logging.target must be stderr or file")
    return LoggingConfig(level=level, target=target)


def _validate_sections(data):
    if not isinstance(data, dict):
        raise ConfigError("configuration must be a TOML table")
    unknown = set(data) - set(_SECTION_FIELDS)
    if unknown:
        raise ConfigError("unknown configuration section: {0}".format(sorted(unknown)[0]))
    for section, values in data.items():
        if not isinstance(values, dict):
            raise ConfigError("configuration section {0} must be a table".format(section))
        unknown_keys = set(values) - _SECTION_FIELDS[section]
        if unknown_keys:
            raise ConfigError(
                "unknown configuration key in {0}: {1}".format(
                    section, sorted(unknown_keys)[0]
                )
            )


def load_config(path: Union[str, Path]) -> RuntimeConfig:
    with open(path, "rb") as config_file:
        data = tomllib.load(config_file)
    _validate_sections(data)
    paths = _load_paths(data.get("paths", {}))
    return RuntimeConfig(
        paths=paths,
        network=_load_network(data.get("network", {})),
        timeouts=_load_timeouts(data.get("timeouts", {})),
        limits=_load_limits(data.get("limits", {})),
        process=_load_process(data.get("process", {})),
        renderer=_load_renderer(data.get("renderer", {})),
        logging=_load_logging(data.get("logging", {})),
    )
