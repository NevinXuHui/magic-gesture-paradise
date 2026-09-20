from typing import Any, Awaitable, Callable, Dict, Optional, Protocol, Union

from smartapp_runtime.domain.models import Session


MessageHandler = Callable[
    [Dict[str, Any]], Union[None, Awaitable[None]]
]


class RendererPort(Protocol):
    def set_message_handler(self, handler: Optional[MessageHandler]) -> None:
        ...

    async def load(self, url: str, session: Session) -> None:
        ...

    async def wait_ready(self, timeout: float) -> None:
        ...

    async def send(self, message: Dict[str, Any]) -> None:
        ...

    async def stop(self) -> None:
        ...

    async def restore_default(self) -> None:
        ...
