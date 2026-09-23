import asyncio
import ipaddress
import signal
import socket
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from smartapp_runtime.adapters.command_renderer import CommandRenderer
from smartapp_runtime.adapters.fake_renderer import FakeRenderer
from smartapp_runtime.adapters.process_renderer import ProcessRenderer
from smartapp_runtime.application.coordinator import RuntimeCoordinator
from smartapp_runtime.application.router import MessageRouter
from smartapp_runtime.config import RuntimeConfig
from smartapp_runtime.infrastructure.ipc.server import AgentServer
from smartapp_runtime.infrastructure.packages.downloader import HttpsDownloader
from smartapp_runtime.infrastructure.packages.installer import PackageInstaller
from smartapp_runtime.infrastructure.persistence.lock import SingleInstanceLock
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.infrastructure.persistence.pointers import AtomicPointers
from smartapp_runtime.infrastructure.persistence.recovery import RecoveryService
from smartapp_runtime.infrastructure.persistence.state_repository import FileStateRepository
from smartapp_runtime.infrastructure.processes.supervisor import ProcessSupervisor
from smartapp_runtime.infrastructure.web.server import StaticWebServer
from smartapp_runtime.logging import configure_logging


class _AgentPublisher:
    """Instance-owned late binding between the router and Agent transport."""

    def __init__(self) -> None:
        self._server: Optional[AgentServer] = None

    @property
    def connected(self) -> bool:
        return self._server is not None and self._server.connected

    def publish(self, event: Dict[str, Any]) -> bool:
        if self._server is None:
            return False
        return self._server.publish(event)

    def bind(self, server: AgentServer) -> None:
        if self._server is not None:
            raise RuntimeError("Agent publisher is already bound")
        self._server = server


def _port_available(host: str, port: int) -> bool:
    try:
        address = ipaddress.ip_address(host)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((host, port))
            return True
        finally:
            probe.close()
    except (OSError, TypeError, ValueError):
        return False


def _static_base_url(host: str, port: int) -> str:
    address = ipaddress.ip_address(host)
    if address.is_unspecified:
        host = "::1" if address.version == 6 else "127.0.0.1"
    rendered_host = "[" + host + "]" if ":" in host else host
    return "http://{0}:{1}".format(rendered_host, port)


@dataclass
class RuntimeAssembly:
    config: RuntimeConfig
    paths: RuntimePaths
    instance_lock: SingleInstanceLock
    repository: FileStateRepository
    pointers: AtomicPointers
    downloader: HttpsDownloader
    installer: PackageInstaller
    static_server: StaticWebServer
    supervisor: ProcessSupervisor
    renderer: Any
    publisher: Any
    router: MessageRouter
    coordinator: RuntimeCoordinator
    recovery: RecoveryService
    agent_server: AgentServer
    _lock_acquired: bool = field(default=False, init=False, repr=False)
    _static_started: bool = field(default=False, init=False, repr=False)
    _coordinator_started: bool = field(default=False, init=False, repr=False)
    _agent_started: bool = field(default=False, init=False, repr=False)

    async def start(self) -> None:
        if self._agent_started:
            return
        try:
            self.instance_lock.acquire()
            self._lock_acquired = True
            self.paths.ensure_layout()
            configure_logging(self.config)
            await self.recovery.recover()
            try:
                self.static_server.start()
            except BaseException:
                self._static_started = bool(
                    getattr(self.static_server, "owns_resources", False)
                )
                raise
            else:
                self._static_started = True
            await self.coordinator.start()
            self._coordinator_started = True
            await self.agent_server.start()
            self._agent_started = True
        except BaseException:
            try:
                await self.stop()
            except BaseException:
                pass
            raise

    async def stop(self) -> None:
        failures = []
        if self._agent_started:
            try:
                await self.agent_server.stop()
            except BaseException as error:
                failures.append(error)
            else:
                self._agent_started = False
        if self._coordinator_started:
            try:
                await self.coordinator.close()
            except BaseException as error:
                failures.append(error)
            else:
                self._coordinator_started = False
        if self._static_started:
            try:
                self.static_server.stop()
            except BaseException as error:
                failures.append(error)
            else:
                self._static_started = False
        if self._lock_acquired:
            try:
                self.instance_lock.release()
            except BaseException as error:
                failures.append(error)
            else:
                self._lock_acquired = False
        if failures:
            raise failures[0]


def build_runtime(config: RuntimeConfig) -> RuntimeAssembly:
    if not isinstance(config, RuntimeConfig):
        raise TypeError("config must be RuntimeConfig")
    paths = RuntimePaths.from_root(config.paths.root)
    instance_lock = SingleInstanceLock(paths.lock_file)
    repository = FileStateRepository(paths.state_file)
    pointers = AtomicPointers(paths)
    downloader = HttpsDownloader()
    installer = PackageInstaller(config, downloader)
    static_server = StaticWebServer(
        pointers, config.network.static_host, config.network.static_port,
        config.timeouts.graceful_stop,
        config.network.backend_host, config.network.backend_port,
    )
    supervisor = ProcessSupervisor(config, repository)
    if config.renderer.kind == "command":
        renderer = CommandRenderer(
            config.renderer,
            max_output_bytes=config.limits.max_message_bytes,
            command_timeout=config.timeouts.renderer,
        )
    elif config.renderer.kind == "process":
        renderer = ProcessRenderer(
            config.renderer,
            max_message_bytes=config.limits.max_message_bytes,
            command_timeout=config.timeouts.renderer,
        )
    else:
        renderer = FakeRenderer(auto_ready=True)
    publisher = _AgentPublisher()
    router = MessageRouter(
        renderer, supervisor, publisher.publish,
        max_queue_messages=config.limits.max_queue_messages,
        max_queue_bytes=config.limits.max_queue_bytes,
    )
    coordinator = RuntimeCoordinator(
        installer, pointers, repository, supervisor, renderer, router,
        _port_available,
        _static_base_url(config.network.static_host, config.network.static_port),
        backend_host=config.network.backend_host,
        backend_port=config.network.backend_port,
        startup_timeout=config.timeouts.startup,
        renderer_timeout=config.timeouts.renderer,
    )
    recovery = RecoveryService(
        paths, repository, pointers, renderer,
        term_grace_seconds=config.timeouts.sigterm,
    )
    agent_server = AgentServer(
        config.paths.socket,
        coordinator.submit,
        max_input_message_bytes=config.limits.max_message_bytes,
        max_output_message_bytes=config.limits.max_message_bytes,
        max_queue_messages=config.limits.max_queue_messages,
        max_queue_bytes=config.limits.max_queue_bytes,
        on_connected=router.flush_upstream,
    )
    publisher.bind(agent_server)
    return RuntimeAssembly(
        config, paths, instance_lock, repository, pointers, downloader, installer,
        static_server, supervisor, renderer, publisher, router, coordinator,
        recovery, agent_server,
    )


def _install_signal_handlers(loop: Any, stopped: asyncio.Event) -> Callable[[], None]:
    loop_handlers = []
    synchronous_handlers = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stopped.set)
            loop_handlers.append(signum)
        except (AttributeError, NotImplementedError, RuntimeError):
            try:
                previous = signal.getsignal(signum)
                signal.signal(signum, lambda _signum, _frame: stopped.set())
                synchronous_handlers.append((signum, previous))
            except (AttributeError, OSError, RuntimeError, ValueError):
                continue

    def remove() -> None:
        for signum in loop_handlers:
            try:
                loop.remove_signal_handler(signum)
            except (AttributeError, NotImplementedError, RuntimeError):
                pass
        for signum, previous in synchronous_handlers:
            try:
                signal.signal(signum, previous)
            except (AttributeError, OSError, RuntimeError, ValueError):
                pass

    return remove


async def run_runtime(config: RuntimeConfig) -> None:
    assembly = build_runtime(config)
    stopped = asyncio.Event()
    remove_handlers: Callable[[], None] = lambda: None
    primary: Optional[BaseException] = None
    try:
        await assembly.start()
        remove_handlers = _install_signal_handlers(asyncio.get_running_loop(), stopped)
        await stopped.wait()
    except BaseException as error:
        primary = error
    finally:
        cleanup_task = asyncio.create_task(assembly.stop(), name="runtime-shutdown")
        interrupted: Optional[asyncio.CancelledError] = None
        try:
            while not cleanup_task.done():
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError as error:
                    if interrupted is None:
                        interrupted = error
                except BaseException:
                    break
        finally:
            # Keep SIGINT/SIGTERM mapped to the idempotent stop event until all
            # application and display cleanup has completed. Repeated Ctrl+C
            # must not interrupt asyncio.run() while it restores the display.
            remove_handlers()
        cleanup_error: Optional[BaseException]
        if cleanup_task.cancelled():
            cleanup_error = asyncio.CancelledError()
        else:
            cleanup_error = cleanup_task.exception()
    if primary is not None:
        raise primary
    if interrupted is not None:
        raise interrupted
    if cleanup_error is not None:
        raise cleanup_error
