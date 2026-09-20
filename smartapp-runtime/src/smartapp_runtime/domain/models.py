from dataclasses import dataclass
from typing import Any, Dict

from smartapp_runtime.domain.commands import StartApp
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError


@dataclass(frozen=True)
class Session:
    session_id: str
    app_id: str
    version: str
    generation: int

    @classmethod
    def from_start(cls, start: StartApp, generation: int) -> "Session":
        if not isinstance(start, StartApp):
            raise SmartAppError(ErrorCode.VALIDATION_ERROR, "start must be a StartApp")
        if type(generation) is not int or generation < 0:
            raise SmartAppError(
                ErrorCode.VALIDATION_ERROR,
                "generation must be a non-negative integer",
            )
        return cls(
            session_id=start.session_id,
            app_id=start.app_id,
            version=start.version,
            generation=generation,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "appId": self.app_id,
            "version": self.version,
            "generation": self.generation,
        }
