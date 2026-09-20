import asyncio
import copy
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from smartapp_runtime.application.lifecycle import (
    CleanupOutcome,
    LifecycleFailure,
    LifecycleTransaction,
    OwnedResources,
)
from smartapp_runtime.application.router import DeliveryFailure
from smartapp_runtime.domain.commands import CloudData, GetStatus, StartApp, StopApp, StopReason
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.ports.process import BackendEvent, BackendHandle
from smartapp_runtime.ports.repository import PersistedRuntimeState, PointerSnapshot


_TRANSITIONS = {
    RuntimeState.IDLE: {RuntimeState.PREPARING},
    RuntimeState.PREPARING: {
        RuntimeState.DOWNLOADING, RuntimeState.STARTING,
        RuntimeState.STOPPING, RuntimeState.CLEANING,
    },
    RuntimeState.DOWNLOADING: {
        RuntimeState.VERIFYING, RuntimeState.STOPPING, RuntimeState.CLEANING,
    },
    RuntimeState.VERIFYING: {
        RuntimeState.INSTALLING, RuntimeState.STOPPING, RuntimeState.CLEANING,
    },
    RuntimeState.INSTALLING: {
        RuntimeState.STARTING, RuntimeState.STOPPING, RuntimeState.CLEANING,
    },
    RuntimeState.STARTING: {
        RuntimeState.RUNNING, RuntimeState.STOPPING, RuntimeState.CLEANING,
    },
    RuntimeState.RUNNING: {RuntimeState.STOPPING, RuntimeState.CLEANING},
    RuntimeState.STOPPING: {RuntimeState.CLEANING},
    RuntimeState.CLEANING: {RuntimeState.IDLE},
}


@dataclass(frozen=True)
class CommandResult:
    request_id: str
    ok: bool
    state: RuntimeState
    session_id: Optional[str] = None
    error: Optional[SmartAppError] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "event": "command_result",
            "requestId": self.request_id,
            "ok": self.ok,
            "state": self.state.value,
        }
        if self.session_id is not None:
            result["sessionId"] = self.session_id
        if self.error is not None:
            result["error"] = copy.deepcopy(self.error.to_dict())
        return result


@dataclass(frozen=True)
class _ExternalCommand:
    command: Union[StartApp, StopApp, CloudData, GetStatus]
    result: asyncio.Future


@dataclass(frozen=True)
class WorkflowProgress:
    generation: int
    state: RuntimeState
    snapshot: Optional[PointerSnapshot]
    acknowledged: asyncio.Future


@dataclass(frozen=True)
class WorkflowSucceeded:
    generation: int
    resources: OwnedResources


@dataclass(frozen=True)
class WorkflowFailed:
    generation: int
    error: SmartAppError
    resources: Optional[OwnedResources]
    cleanup: CleanupOutcome


@dataclass(frozen=True)
class ComponentData:
    generation: int
    handle: BackendHandle
    event: BackendEvent
    acknowledged: asyncio.Future


@dataclass(frozen=True)
class ComponentExited:
    generation: int
    handle: BackendHandle
    error: SmartAppError
    acknowledged: asyncio.Future


@dataclass(frozen=True)
class CleanupFinished:
    generation: int
    resources: OwnedResources
    error: Optional[SmartAppError]
    outcome: CleanupOutcome


@dataclass(frozen=True)
class _Close:
    result: asyncio.Future


@dataclass
class _PendingStart:
    command: StartApp
    waiters: List[Tuple[str, asyncio.Future]]


class RuntimeCoordinator:
    def __init__(
        self,
        installer: Any,
        pointers: Any,
        repository: Any,
        supervisor: Any,
        renderer: Any,
        router: Any,
        port_available: Any,
        web_base_url: str,
        backend_host: str = "127.0.0.1",
        backend_port: int = 18081,
        startup_timeout: float = 15.0,
        renderer_timeout: float = 15.0,
        queue_size: int = 256,
    ) -> None:
        self._repository = repository
        self._lifecycle = LifecycleTransaction(
            installer, pointers, supervisor, renderer, router, port_available,
            web_base_url, backend_host, backend_port, startup_timeout, renderer_timeout,
        )
        self._router = router
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._admission_lock = asyncio.Lock()
        self._accepting = True
        self._actor_task: Optional[asyncio.Task] = None
        self._workflow_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._cancel_event: Optional[asyncio.Event] = None
        self._state = RuntimeState.IDLE
        self._session: Optional[Session] = None
        self._generation = 0
        self._snapshot: Optional[PointerSnapshot] = None
        self._last_error: Optional[SmartAppError] = None
        self._resources: Optional[OwnedResources] = None
        self._active_identity: Optional[Tuple[str, str, str, str]] = None
        self._start_waiters: List[Tuple[str, asyncio.Future]] = []
        self._stop_waiters: List[Tuple[str, asyncio.Future]] = []
        self._pending_start: Optional[_PendingStart] = None
        self._closing = False
        self._close_waiters: List[asyncio.Future] = []
        self._close_future: Optional[asyncio.Future] = None
        self._close_enqueue_task: Optional[asyncio.Task] = None
        self._exit_handled_generation: Optional[int] = None
        self._pending_backend_events: List[Tuple[BackendHandle, BackendEvent]] = []
        self._cleanup_rollback = False

    async def start(self) -> None:
        if self._actor_task is not None:
            return
        recovered = self._repository.load()
        if recovered.state != RuntimeState.IDLE:
            raise SmartAppError(
                ErrorCode.INTERNAL_ERROR,
                "runtime coordinator requires recovered IDLE state",
            )
        self._state = RuntimeState.IDLE
        self._generation = recovered.generation
        self._actor_task = asyncio.create_task(self._run(), name="runtime-coordinator")

    async def submit(
        self, command: Union[StartApp, StopApp, CloudData, GetStatus]
    ) -> CommandResult:
        if not isinstance(command, (StartApp, StopApp, CloudData, GetStatus)):
            return CommandResult(
                getattr(command, "request_id", "unknown"), False, self._state,
                error=SmartAppError(ErrorCode.VALIDATION_ERROR, "unsupported command"),
            )
        async with self._admission_lock:
            if not self._accepting:
                return self._failure(command.request_id, _internal("runtime coordinator is closed"))
            try:
                command = _normalize_command(command)
            except SmartAppError as error:
                return self._failure(command.request_id, error)
            if self._actor_task is None:
                await self.start()
            future = asyncio.get_running_loop().create_future()
            await self._queue.put(_ExternalCommand(command, future))
        return await asyncio.shield(future)

    async def close(self) -> None:
        async with self._admission_lock:
            if self._actor_task is None:
                self._accepting = False
                return
            if self._actor_task.done():
                actor = self._actor_task
                future = None
            else:
                self._accepting = False
                actor = self._actor_task
                if self._close_future is None:
                    self._close_future = asyncio.get_running_loop().create_future()
                    self._close_future.add_done_callback(self._consume_task)
                    enqueue = asyncio.create_task(
                        self._queue.put(_Close(self._close_future)),
                        name="runtime-close-admission",
                    )
                    self._close_enqueue_task = enqueue
                    try:
                        await asyncio.shield(enqueue)
                    except asyncio.CancelledError as cancelled:
                        while not enqueue.done():
                            try:
                                await asyncio.shield(enqueue)
                            except asyncio.CancelledError:
                                continue
                        if not enqueue.cancelled() and enqueue.exception() is not None:
                            raise enqueue.exception()
                        raise cancelled
                    finally:
                        if enqueue.done():
                            self._close_enqueue_task = None
                future = self._close_future
        if future is not None:
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                raise
            except BaseException:
                async with self._admission_lock:
                    if self._close_future is future:
                        self._close_future = None
                raise
        await asyncio.gather(actor, return_exceptions=True)

    async def _run(self) -> None:
        running = True
        while running:
            event = await self._queue.get()
            try:
                running = await self._handle(event)
            except BaseException as error:
                internal = _as_error(error, "runtime actor failed")
                self._last_error = internal
                self._settle_event_failure(event, internal)
            finally:
                self._queue.task_done()

    async def _handle(self, event: Any) -> bool:
        if isinstance(event, _ExternalCommand):
            await self._handle_command(event.command, event.result)
        elif isinstance(event, WorkflowProgress):
            self._handle_progress(event)
        elif isinstance(event, WorkflowSucceeded):
            await self._handle_workflow_success(event)
        elif isinstance(event, WorkflowFailed):
            await self._handle_workflow_failure(event)
        elif isinstance(event, CleanupFinished):
            await self._handle_cleanup_finished(event)
        elif isinstance(event, ComponentData):
            await self._handle_component_data(event)
        elif isinstance(event, ComponentExited):
            await self._handle_component_exit(event)
        elif isinstance(event, _Close):
            self._closing = True
            self._close_waiters.append(event.result)
            await self._handle_close()
        else:
            raise _internal("unknown actor event")
        return not self._ready_to_close()

    async def _handle_command(self, command: Any, result: asyncio.Future) -> None:
        if isinstance(command, StartApp):
            await self._handle_start(command, result)
        elif isinstance(command, StopApp):
            await self._handle_stop(command, result)
        elif isinstance(command, CloudData):
            await self._handle_cloud(command, result)
        else:
            self._resolve(result, CommandResult(
                command.request_id, True, self._state,
                None if self._session is None else self._session.session_id,
            ))

    async def _handle_start(self, command: StartApp, result: asyncio.Future) -> None:
        identity = _identity(command)
        if (self._state in (RuntimeState.STOPPING, RuntimeState.CLEANING)
                and self._active_identity == identity):
            self._resolve(result, self._failure(
                command.request_id,
                SmartAppError(ErrorCode.SESSION_CONFLICT, "session is stopping"),
            ))
            return
        if self._pending_start is not None:
            pending_identity = _identity(self._pending_start.command)
            if identity == pending_identity:
                self._pending_start.waiters.append((command.request_id, result))
                return
            if command.session_id == self._pending_start.command.session_id:
                self._resolve(result, self._failure(
                    command.request_id,
                    SmartAppError(ErrorCode.SESSION_CONFLICT, "session identity conflicts"),
                ))
                return
            self._resolve_waiters(
                self._pending_start.waiters,
                SmartAppError(ErrorCode.SESSION_CONFLICT, "start was superseded"),
            )
            self._pending_start = _PendingStart(command, [(command.request_id, result)])
            return

        if self._session is None and self._state == RuntimeState.IDLE:
            self._begin_start(command, [(command.request_id, result)])
            return
        if self._active_identity == identity:
            if self._state == RuntimeState.RUNNING:
                self._resolve(result, CommandResult(
                    command.request_id, True, self._state, command.session_id,
                ))
            else:
                self._start_waiters.append((command.request_id, result))
            return
        if self._session is not None and command.session_id == self._session.session_id:
            self._resolve(result, self._failure(
                command.request_id,
                SmartAppError(ErrorCode.SESSION_CONFLICT, "session identity conflicts"),
            ))
            return

        self._pending_start = _PendingStart(command, [(command.request_id, result)])
        self._resolve_waiters(
            self._start_waiters,
            SmartAppError(ErrorCode.SESSION_CONFLICT, "start was replaced"),
        )
        self._start_waiters = []
        await self._request_stop(StopReason.REPLACE)

    async def _handle_stop(self, command: StopApp, result: asyncio.Future) -> None:
        if self._pending_start is not None and command.session_id == self._pending_start.command.session_id:
            self._resolve_waiters(
                self._pending_start.waiters, _internal("startup cancelled by stop")
            )
            self._pending_start = None
            self._resolve(result, CommandResult(command.request_id, True, self._state))
            return
        if self._session is None or command.session_id != self._session.session_id:
            self._resolve(result, CommandResult(
                command.request_id, True, self._state,
                None if self._session is None else self._session.session_id,
            ))
            return
        self._stop_waiters.append((command.request_id, result))
        await self._request_stop(command.reason)

    async def _handle_cloud(self, command: CloudData, result: asyncio.Future) -> None:
        if self._session is None or command.session_id != self._session.session_id:
            self._resolve(result, self._failure(
                command.request_id,
                SmartAppError(ErrorCode.SESSION_MISMATCH, "cloud data session does not match"),
            ))
            return
        if self._state not in (RuntimeState.STARTING, RuntimeState.RUNNING):
            self._resolve(result, self._failure(
                command.request_id,
                SmartAppError(ErrorCode.SESSION_MISMATCH, "runtime is not accepting cloud data"),
            ))
            return
        try:
            await self._router.route_cloud_data(command)
            self._resolve(result, CommandResult(
                command.request_id, True, self._state, command.session_id,
            ))
        except DeliveryFailure as delivery:
            failure = delivery.error
            self._resolve(result, self._failure(command.request_id, failure))
            await self._request_stop(StopReason.RUNTIME_ERROR, failure)
        except SmartAppError as failure:
            self._resolve(result, self._failure(command.request_id, failure))
        except BaseException as error:
            failure = _as_error(error, "cloud data delivery failed")
            self._resolve(result, self._failure(command.request_id, failure))
            await self._request_stop(StopReason.RUNTIME_ERROR, failure)

    def _begin_start(
        self, command: StartApp, waiters: List[Tuple[str, asyncio.Future]]
    ) -> None:
        self._generation += 1
        self._session = Session.from_start(command, self._generation)
        self._active_identity = _identity(command)
        self._start_waiters = waiters
        self._resources = None
        self._snapshot = None
        self._last_error = None
        self._exit_handled_generation = None
        self._pending_backend_events = []
        self._cleanup_rollback = False
        self._transition(RuntimeState.PREPARING)
        cancel = asyncio.Event()
        self._cancel_event = cancel
        generation = self._generation
        session = self._session

        async def run() -> None:
            try:
                resources = await self._lifecycle.startup(
                    command, session, generation, cancel,
                    lambda state, snapshot: self._progress_for(
                        generation, state, snapshot
                    ),
                    self._backend_event, self._backend_exit,
                )
                await self._queue.put(WorkflowSucceeded(generation, resources))
            except LifecycleFailure as failure:
                await self._queue.put(WorkflowFailed(
                    generation, failure.error, failure.resources, failure.cleanup,
                ))
            except BaseException as error:
                await self._queue.put(WorkflowFailed(
                    generation, _as_error(error, "startup workflow failed"), None,
                    CleanupOutcome(True, ()),
                ))

        self._workflow_task = asyncio.create_task(run(), name="startup-workflow")
        self._workflow_task.add_done_callback(self._consume_task)

    async def _progress_for(
        self,
        generation: int,
        state: RuntimeState,
        snapshot: Optional[PointerSnapshot],
    ) -> None:
        acknowledged = asyncio.get_running_loop().create_future()
        await self._queue.put(WorkflowProgress(
            generation, state, snapshot, acknowledged,
        ))
        accepted = await asyncio.shield(acknowledged)
        if not accepted:
            raise asyncio.CancelledError()

    def _handle_progress(self, event: WorkflowProgress) -> None:
        if event.generation != self._generation or self._session is None:
            self._resolve(event.acknowledged, False)
            return
        if self._state in (RuntimeState.STOPPING, RuntimeState.CLEANING):
            self._resolve(event.acknowledged, False)
            return
        try:
            if event.snapshot is not None:
                self._snapshot = event.snapshot
            self._transition(event.state)
            self._resolve(event.acknowledged, True)
        except BaseException as error:
            self._resolve_exception(event.acknowledged, _as_error(error, "invalid workflow progress"))

    async def _handle_workflow_success(self, event: WorkflowSucceeded) -> None:
        if event.generation != self._generation or self._session is None:
            await self._lifecycle.cleanup(
                event.resources, StopReason.RUNTIME_ERROR, rollback=True
            )
            return
        if self._state != RuntimeState.STARTING:
            self._workflow_task = None
            if self._resources is None:
                self._resources = event.resources
                failure = self._last_error or _internal(
                    "startup completed after cancellation"
                )
                self._resolve_waiters(self._start_waiters, failure)
                self._start_waiters = []
                await self._begin_cleanup(
                    StopReason.RUNTIME_ERROR, failure, rollback=True
                )
            elif event.resources is not self._resources:
                await self._lifecycle.cleanup(
                    event.resources, StopReason.RUNTIME_ERROR, rollback=True
                )
            return
        self._workflow_task = None
        self._resources = event.resources
        try:
            await self._lifecycle.activate(event.resources)
            self._snapshot = None
            self._transition(RuntimeState.RUNNING)
            pending = self._pending_backend_events
            self._pending_backend_events = []
            for handle, backend_event in pending:
                if handle == event.resources.backend_handle:
                    await self._router.accept_app_data("python", backend_event)
        except BaseException as error:
            failure = _as_error(error, "application activation failed")
            self._resolve_waiters(self._start_waiters, failure)
            self._start_waiters = []
            await self._begin_cleanup(StopReason.RUNTIME_ERROR, failure, rollback=True)
            return
        self._resolve_waiters(self._start_waiters, None)
        self._start_waiters = []

    async def _handle_workflow_failure(self, event: WorkflowFailed) -> None:
        if event.generation != self._generation:
            return
        self._workflow_task = None
        self._resources = event.resources
        if self._closing and self._last_error is None:
            primary = None
        else:
            primary = (
                self._last_error
                if self._state == RuntimeState.STOPPING and self._last_error is not None
                else event.error
            )
        if not event.cleanup.released and event.cleanup.errors:
            primary = event.cleanup.errors[0]
        self._last_error = primary
        self._cleanup_rollback = True
        if self._state != RuntimeState.CLEANING:
            self._transition(RuntimeState.CLEANING, error=primary)
        if event.cleanup.released:
            self._finish_idle(primary)
        else:
            self._resolve_stop_waiters(primary)
        self._resolve_waiters(self._start_waiters, primary)
        self._start_waiters = []
        if self._closing and not event.cleanup.released:
            self._resolve_close_waiters(primary or event.error)
        await self._launch_pending_if_possible()

    async def _request_stop(
        self, reason: StopReason, error: Optional[SmartAppError] = None
    ) -> None:
        if self._state == RuntimeState.IDLE:
            self._resolve_stop_waiters(None)
            return
        if self._state not in (RuntimeState.STOPPING, RuntimeState.CLEANING):
            self._transition(RuntimeState.STOPPING, error=error)
        if self._workflow_task is not None:
            if self._cancel_event is not None:
                self._cancel_event.set()
            self._workflow_task.cancel()
            return
        if self._cleanup_task is None and self._resources is not None:
            await self._begin_cleanup(
                reason, error, rollback=self._cleanup_rollback
            )
        elif self._resources is None:
            if self._state != RuntimeState.CLEANING:
                self._transition(RuntimeState.CLEANING, error=error)
            self._finish_idle(error)
            await self._launch_pending_if_possible()

    async def _begin_cleanup(
        self, reason: StopReason, error: Optional[SmartAppError], rollback: bool
    ) -> None:
        if self._resources is None:
            if self._state != RuntimeState.CLEANING:
                self._transition(RuntimeState.CLEANING, error=error)
            self._finish_idle(error)
            await self._launch_pending_if_possible()
            return
        self._cleanup_rollback = rollback
        if self._state != RuntimeState.CLEANING:
            self._transition(RuntimeState.CLEANING, error=error)
        resources = self._resources
        generation = self._generation

        async def run() -> None:
            outcome = await self._lifecycle.cleanup(resources, reason, rollback)
            await self._queue.put(CleanupFinished(
                generation, resources, error, outcome,
            ))

        self._cleanup_task = asyncio.create_task(run(), name="lifecycle-cleanup")
        self._cleanup_task.add_done_callback(self._consume_task)

    async def _handle_cleanup_finished(self, event: CleanupFinished) -> None:
        if event.generation != self._generation or event.resources is not self._resources:
            return
        self._cleanup_task = None
        if event.outcome.released:
            self._finish_idle(event.error)
            await self._launch_pending_if_possible()
        else:
            failure = event.error or event.outcome.errors[0]
            self._last_error = failure
            self._persist()
            self._resolve_stop_waiters(failure)
            if self._closing:
                self._resolve_close_waiters(failure)

    async def _handle_component_data(self, event: ComponentData) -> None:
        try:
            if (event.generation == self._generation
                    and event.handle.session == self._session
                    and self._state == RuntimeState.STARTING):
                self._pending_backend_events.append((event.handle, event.event))
            elif self._valid_component(event.generation, event.handle) and self._state == RuntimeState.RUNNING:
                await self._router.accept_app_data("python", event.event)
            self._resolve(event.acknowledged, True)
        except BaseException as error:
            failure = _as_error(error, "backend data routing failed")
            self._resolve_exception(event.acknowledged, failure)
            await self._request_stop(StopReason.RUNTIME_ERROR, failure)

    async def _handle_component_exit(self, event: ComponentExited) -> None:
        starting = (
            event.generation == self._generation
            and event.handle.session == self._session
            and self._state == RuntimeState.STARTING
        )
        if ((starting or self._valid_component(event.generation, event.handle))
                and self._exit_handled_generation != event.generation):
            self._exit_handled_generation = event.generation
            await self._request_stop(StopReason.RUNTIME_ERROR, event.error)
        self._resolve(event.acknowledged, True)

    async def _backend_event(
        self, generation: int, handle: BackendHandle, event: BackendEvent
    ) -> None:
        acknowledged = asyncio.get_running_loop().create_future()
        await self._queue.put(ComponentData(generation, handle, event, acknowledged))
        await asyncio.shield(acknowledged)

    async def _backend_exit(
        self, generation: int, handle: BackendHandle, error: SmartAppError
    ) -> None:
        acknowledged = asyncio.get_running_loop().create_future()
        await self._queue.put(ComponentExited(generation, handle, error, acknowledged))
        await asyncio.shield(acknowledged)

    async def _handle_close(self) -> None:
        if self._pending_start is not None:
            self._resolve_waiters(
                self._pending_start.waiters, _internal("runtime coordinator is closing")
            )
            self._pending_start = None
        if self._start_waiters:
            self._resolve_waiters(
                self._start_waiters, _internal("runtime coordinator is closing")
            )
            self._start_waiters = []
        if self._session is not None:
            await self._request_stop(StopReason.OPERATOR)
        elif self._state == RuntimeState.CLEANING and self._resources is not None:
            await self._begin_cleanup(
                StopReason.OPERATOR, self._last_error,
                rollback=self._cleanup_rollback,
            )
        self._finish_close_if_ready()

    def _ready_to_close(self) -> bool:
        ready = (
            self._closing and self._state == RuntimeState.IDLE
            and self._workflow_task is None and self._cleanup_task is None
        )
        if ready:
            self._finish_close_if_ready()
        return ready

    def _finish_close_if_ready(self) -> None:
        if not self._closing or self._state != RuntimeState.IDLE:
            return
        for waiter in self._close_waiters:
            self._resolve(waiter, None)
        self._close_waiters = []

    def _resolve_close_waiters(self, error: SmartAppError) -> None:
        for waiter in self._close_waiters:
            self._resolve_exception(waiter, error)
        self._close_waiters = []

    def _finish_idle(self, error: Optional[SmartAppError]) -> None:
        self._resources = None
        self._snapshot = None
        self._session = None
        self._active_identity = None
        self._pending_backend_events = []
        self._cleanup_rollback = False
        self._cancel_event = None
        self._last_error = error
        self._transition(RuntimeState.IDLE, clear=True, error=error)
        self._resolve_stop_waiters(None)

    async def _launch_pending_if_possible(self) -> None:
        if self._state != RuntimeState.IDLE or self._pending_start is None or self._closing:
            return
        pending = self._pending_start
        self._pending_start = None
        self._begin_start(pending.command, pending.waiters)

    def _transition(
        self,
        state: RuntimeState,
        clear: bool = False,
        error: Optional[SmartAppError] = None,
    ) -> None:
        if state not in _TRANSITIONS.get(self._state, set()):
            raise _internal(
                "invalid runtime transition {0}->{1}".format(self._state.value, state.value)
            )
        self._state = state
        if error is not None:
            self._last_error = error
        if clear and error is None:
            self._snapshot = None
            self._last_error = None
        self._persist()

    def _persist(self) -> None:
        observed = self._repository.load()
        state = PersistedRuntimeState(
            schema_version=observed.schema_version,
            state=self._state,
            active_session=self._session,
            generation=self._generation,
            backend_process=observed.backend_process,
            pointer_snapshot=self._snapshot,
            last_error=None if self._last_error is None else self._last_error.to_dict(),
        )
        self._repository.save(state)

    def _failure(self, request_id: str, error: SmartAppError) -> CommandResult:
        return CommandResult(
            request_id, False, self._state,
            None if self._session is None else self._session.session_id,
            error,
        )

    def _resolve_waiters(
        self,
        waiters: List[Tuple[str, asyncio.Future]],
        error: Optional[SmartAppError],
    ) -> None:
        for request_id, future in waiters:
            if error is None:
                result = CommandResult(
                    request_id, True, self._state,
                    None if self._session is None else self._session.session_id,
                )
            else:
                result = self._failure(request_id, error)
            self._resolve(future, result)

    def _resolve_stop_waiters(self, error: Optional[SmartAppError]) -> None:
        waiters = self._stop_waiters
        self._stop_waiters = []
        self._resolve_waiters(waiters, error)

    def _valid_component(self, generation: int, handle: BackendHandle) -> bool:
        return (
            generation == self._generation
            and self._resources is not None
            and self._resources.backend_handle == handle
            and handle.session == self._session
        )

    def _settle_event_failure(self, event: Any, error: SmartAppError) -> None:
        future = getattr(event, "result", None) or getattr(event, "acknowledged", None)
        if future is not None:
            self._resolve_exception(future, error)

    @staticmethod
    def _resolve(future: asyncio.Future, value: Any) -> None:
        if not future.done():
            future.set_result(value)

    @staticmethod
    def _resolve_exception(future: asyncio.Future, error: BaseException) -> None:
        if not future.done():
            future.set_exception(error)

    @staticmethod
    def _consume_task(task: asyncio.Task) -> None:
        if not task.cancelled():
            task.exception()


def _identity(command: StartApp) -> Tuple[str, str, str, str]:
    return command.session_id, command.app_id, command.version, command.sha256


def _normalize_command(command: Any) -> Any:
    if isinstance(command, StartApp):
        return StartApp.from_dict(command.to_dict())
    if isinstance(command, StopApp):
        return StopApp.from_dict(command.to_dict())
    if isinstance(command, CloudData):
        return CloudData.from_dict(command.to_dict())
    if isinstance(command, GetStatus):
        return GetStatus.from_dict(command.to_dict())
    raise SmartAppError(ErrorCode.VALIDATION_ERROR, "unsupported command")


def _internal(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.INTERNAL_ERROR, message)


def _as_error(error: BaseException, message: str) -> SmartAppError:
    if isinstance(error, SmartAppError):
        return error
    return _internal(message)
