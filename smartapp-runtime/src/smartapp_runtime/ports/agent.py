from typing import Any, Dict, Protocol


class AgentEventPublisher(Protocol):
    @property
    def connected(self) -> bool:
        ...

    def publish(self, event: Dict[str, Any]) -> bool:
        ...
