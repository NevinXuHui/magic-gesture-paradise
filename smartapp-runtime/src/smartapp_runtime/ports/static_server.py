from typing import Protocol, Tuple


class StaticWebServer(Protocol):
    def start(self) -> None:
        ...

    def stop(self) -> None:
        ...

    @property
    def address(self) -> Tuple[str, int]:
        ...
