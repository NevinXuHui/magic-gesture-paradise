from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Union, TYPE_CHECKING

from smartapp_runtime.domain.commands import StopReason
from smartapp_runtime.domain.errors import SmartAppError
from smartapp_runtime.domain.models import Session

if TYPE_CHECKING:
    from smartapp_runtime.infrastructure.packages.installer import InstalledApp


@dataclass(frozen=True)
class BackendEvent:
    event: str
    data_type: Optional[str]
    data: Dict[str, Any]


@dataclass(frozen=True)
class BackendHandle:
    pid: int
    session: Session
    app_root: Path


EventCallback = Callable[[BackendEvent], Union[None, Awaitable[None]]]
ExitCallback = Callable[[BackendHandle, SmartAppError], Union[None, Awaitable[None]]]
StderrCallback = Callable[[str, bool], Union[None, Awaitable[None]]]
OwnershipCallback = Callable[[BackendHandle], None]


class ProcessSupervisor(Protocol):
    async def start(self, installed: "InstalledApp", session: Session,
                    init_data: Dict[str, Any], on_event: EventCallback,
                    on_exit: ExitCallback, on_stderr: Optional[StderrCallback] = None,
                    on_owned: Optional[OwnershipCallback] = None) -> BackendHandle:
        ...

    async def send(self, handle: BackendHandle, message: Dict[str, Any]) -> None:
        ...

    async def stop(self, handle: BackendHandle, reason: StopReason) -> None:
        ...
