import asyncio
import copy
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple
from urllib.parse import quote

from smartapp_runtime.domain.commands import StartApp, StopReason
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.ports.process import BackendEvent, BackendHandle
from smartapp_runtime.ports.repository import PointerSnapshot


class InstalledAppLike(Protocol):
    root: Path
    manifest: Any
    cache_hit: bool


class InstallerLike(Protocol):
    async def ensure_installed(
        self,
        command: StartApp,
        progress: Optional[Callable[[RuntimeState], Awaitable[None]]] = None,
    ) -> InstalledAppLike:
        ...


class PointerLike(Protocol):
    def snapshot(self, app_id: str) -> PointerSnapshot:
        ...

    def current_target(self, app_id: str) -> Optional[Path]:
        ...

    def set_current_web(self, web_root: Optional[Path]) -> None:
        ...

    def set_previous(self, app_id: str, version_root: Optional[Path]) -> None:
        ...

    def set_current(self, app_id: str, version_root: Path) -> None:
        ...

    def restore(self, snapshot: PointerSnapshot) -> None:
        ...


ProgressCallback = Callable[[RuntimeState, Optional[PointerSnapshot]], Awaitable[None]]
BackendEventCallback = Callable[[int, BackendHandle, BackendEvent], Awaitable[None]]
BackendExitCallback = Callable[[int, BackendHandle, SmartAppError], Awaitable[None]]


@dataclass
class OwnedResources:
    session: Session
    installed: InstalledAppLike
    snapshot: PointerSnapshot
    previous_target: Optional[Path]
    backend_handle: Optional[BackendHandle] = None
    router_started: bool = False
    renderer_loaded: bool = False


@dataclass(frozen=True)
class CleanupOutcome:
    released: bool
    errors: Tuple[SmartAppError, ...]


class LifecycleFailure(Exception):
    def __init__(self, error: SmartAppError, resources: Optional[OwnedResources], cleanup: CleanupOutcome) -> None:
        self.error = error
        self.resources = resources
        self.cleanup = cleanup
        super().__init__(error.message)


def _internal(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.INTERNAL_ERROR, message)


def _as_error(error: BaseException, message: str) -> SmartAppError:
    if isinstance(error, SmartAppError):
        return error
    return _internal(message)


class LifecycleTransaction:
    """Runs component I/O without owning coordinator state."""

    def __init__(
        self,
        installer: InstallerLike,
        pointers: PointerLike,
        supervisor: Any,
        renderer: Any,
        router: Any,
        port_available: Callable[[str, int], Any],
        web_base_url: str,
        backend_host: str = "127.0.0.1",
        backend_port: int = 18081,
        startup_timeout: float = 15.0,
        renderer_timeout: float = 15.0,
    ) -> None:
        self._installer = installer
        self._pointers = pointers
        self._supervisor = supervisor
        self._renderer = renderer
        self._router = router
        self._port_available = port_available
        self._web_base_url = web_base_url.rstrip("/")
        self._backend_host = backend_host
        self._backend_port = backend_port
        self._startup_timeout = startup_timeout
        self._renderer_timeout = renderer_timeout

    async def startup(
        self,
        command: StartApp,
        session: Session,
        generation: int,
        cancelled: asyncio.Event,
        progress: ProgressCallback,
        on_backend_event: BackendEventCallback,
        on_backend_exit: BackendExitCallback,
    ) -> OwnedResources:
        resources: Optional[OwnedResources] = None

        async def install_progress(state: RuntimeState) -> None:
            self._check_cancelled(cancelled)
            await progress(state, None)
            self._check_cancelled(cancelled)

        try:
            installed = await self._installer.ensure_installed(command, progress=install_progress)
            self._check_cancelled(cancelled)
            selected = installed.manifest
            if selected.backend.dynamic_service:
                available = self._port_available(self._backend_host, self._backend_port)
                if inspect.isawaitable(available):
                    available = await available
                if available is not True:
                    raise SmartAppError(ErrorCode.PORT_IN_USE, "dynamic service port is unavailable")

            snapshot = self._pointers.snapshot(command.app_id)
            resources = OwnedResources(
                session, installed, snapshot,
                self._pointers.current_target(command.app_id),
            )
            await progress(RuntimeState.STARTING, snapshot)
            self._check_cancelled(cancelled)

            resources.router_started = True
            await self._router.begin_session(session, selected, backend_handle=None)

            renderer_ready = None
            if selected.web.enabled:
                self._pointers.set_current_web(installed.root / "web")
                entry = "/".join(quote(part, safe="") for part in selected.web.entry.split("/"))
                url = "{0}/{1}?v={2}".format(self._web_base_url, entry, quote(command.version, safe=""))
                resources.renderer_loaded = True
                await self._renderer.load(url, session)
                renderer_ready = asyncio.create_task(
                    self._renderer.wait_ready(self._renderer_timeout), name="renderer-ready"
                )

            backend_ready = None
            handle_box: Dict[str, BackendHandle] = {}
            pending_backend_events: List[BackendEvent] = []
            if selected.backend.enabled:
                async def backend_event(event: BackendEvent) -> None:
                    handle = handle_box.get("handle")
                    if handle is None:
                        pending_backend_events.append(event)
                        return
                    await on_backend_event(generation, handle, event)

                async def backend_exit(handle: BackendHandle, error: SmartAppError) -> None:
                    await on_backend_exit(generation, handle, error)

                def backend_owned(handle: BackendHandle) -> None:
                    handle_box["handle"] = handle
                    resources.backend_handle = handle

                backend_ready = asyncio.create_task(
                    self._supervisor.start(
                        installed, session, command.init_data, backend_event, backend_exit,
                        on_owned=backend_owned,
                    ),
                    name="backend-ready",
                )

            tasks = [task for task in (renderer_ready, backend_ready) if task is not None]
            if tasks:
                joined = asyncio.gather(*tasks)
                try:
                    await asyncio.wait_for(
                        joined, self._startup_timeout
                    )
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    try:
                        await joined
                    except BaseException:
                        pass
                    if (backend_ready is not None and not backend_ready.cancelled()
                            and backend_ready.exception() is None):
                        resources.backend_handle = backend_ready.result()
                    raise
                if backend_ready is not None:
                    handle = backend_ready.result()
                    handle_box["handle"] = handle
                    resources.backend_handle = handle
                    await self._router.bind_backend(session.session_id, handle)
                    for event in pending_backend_events:
                        await on_backend_event(generation, handle, event)
                    pending_backend_events.clear()
            if selected.web.enabled:
                await self._renderer.send({
                    "event": "runtime_init",
                    "sessionId": session.session_id,
                    "appId": session.app_id,
                    "version": session.version,
                    "data": copy.deepcopy(command.init_data),
                })
            self._check_cancelled(cancelled)
            return resources
        except asyncio.CancelledError:
            cleanup = await asyncio.shield(
                self.cleanup(resources, StopReason.RUNTIME_ERROR, rollback=True)
            )
            raise LifecycleFailure(_internal("startup cancelled"), resources, cleanup) from None
        except BaseException as error:
            cleanup = await asyncio.shield(
                self.cleanup(resources, StopReason.RUNTIME_ERROR, rollback=True)
            )
            failure = (
                SmartAppError(ErrorCode.START_TIMEOUT, "application readiness timed out")
                if isinstance(error, asyncio.TimeoutError)
                else _as_error(error, "application startup failed")
            )
            raise LifecycleFailure(failure, resources, cleanup) from None

    async def activate(self, resources: OwnedResources) -> None:
        self._pointers.set_previous(
            resources.session.app_id, resources.previous_target
        )
        self._pointers.set_current(resources.session.app_id, resources.installed.root)
        await self._router.mark_running(resources.session.session_id)

    async def cleanup(
        self,
        resources: Optional[OwnedResources],
        reason: StopReason,
        rollback: bool,
    ) -> CleanupOutcome:
        if resources is None:
            return CleanupOutcome(True, ())
        errors: List[SmartAppError] = []

        async def run_async(operation: Callable[[], Awaitable[None]], message: str) -> None:
            try:
                await operation()
            except BaseException as error:
                errors.append(_as_error(error, message))

        def run_sync(operation: Callable[[], None], message: str) -> None:
            try:
                operation()
            except BaseException as error:
                errors.append(_as_error(error, message))

        if resources.router_started:
            before = len(errors)
            await run_async(
                lambda: self._router.end_session(resources.session.session_id),
                "router cleanup failed",
            )
            if len(errors) == before:
                resources.router_started = False
        if resources.backend_handle is not None:
            await run_async(
                lambda: self._supervisor.stop(resources.backend_handle, reason),
                "backend cleanup failed",
            )
        if resources.renderer_loaded:
            await run_async(self._renderer.stop, "renderer cleanup failed")
        if rollback:
            run_sync(lambda: self._pointers.restore(resources.snapshot), "pointer rollback failed")
        else:
            run_sync(lambda: self._pointers.set_current_web(None), "web pointer cleanup failed")
        await run_async(self._renderer.restore_default, "default display restore failed")
        return CleanupOutcome(not errors, tuple(errors))

    @staticmethod
    def _check_cancelled(cancelled: asyncio.Event) -> None:
        if cancelled.is_set():
            raise asyncio.CancelledError()
