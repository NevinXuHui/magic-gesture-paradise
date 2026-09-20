from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol

from smartapp_runtime.domain.models import Session
from smartapp_runtime.domain.state import RuntimeState


@dataclass(frozen=True)
class PointerSnapshot:
    app_id: str
    current: Optional[str]
    previous: Optional[str]
    current_web: Optional[str]


@dataclass(frozen=True)
class BackendProcessIdentity:
    pid: int
    start_time: str
    command_marker: str


@dataclass(frozen=True)
class PersistedRuntimeState:
    schema_version: int = 1
    state: RuntimeState = RuntimeState.BOOT_RECOVERY
    active_session: Optional[Session] = None
    generation: int = 0
    backend_process: Optional[BackendProcessIdentity] = None
    pointer_snapshot: Optional[PointerSnapshot] = None
    last_error: Optional[Dict[str, Any]] = None


class StateRepository(Protocol):
    def load(self) -> PersistedRuntimeState:
        ...

    def save(self, state: PersistedRuntimeState) -> None:
        ...


class ProcessIdentityRepository(StateRepository, Protocol):
    def set_backend_process(self, identity: BackendProcessIdentity) -> None:
        ...

    def clear_backend_process(self) -> None:
        ...
