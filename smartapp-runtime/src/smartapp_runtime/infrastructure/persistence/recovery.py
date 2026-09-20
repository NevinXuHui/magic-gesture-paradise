import asyncio
import logging
import math
import os
import shutil
import signal
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Protocol, Tuple

from smartapp_runtime.domain.commands import _expect_app_id, _expect_version
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths, fsync_directory, recovery_error
from smartapp_runtime.infrastructure.persistence.pointers import AtomicPointers
from smartapp_runtime.ports.repository import BackendProcessIdentity, PersistedRuntimeState, StateRepository


class _RendererRestorer(Protocol):
    async def restore_default(self) -> None:
        ...


@dataclass(frozen=True)
class ObservedProcessIdentity:
    start_time: str
    cmdline: Tuple[str, ...]


class ProcessIdentityReader(Protocol):
    def read(self, pid: int) -> Optional[ObservedProcessIdentity]:
        ...


class SignalSender(Protocol):
    def send_group(self, pgid: int, signal_number: int) -> None:
        ...


class LinuxProcessIdentityReader:
    def __init__(self, proc_root: Path = Path("/proc")) -> None:
        self.proc_root = proc_root

    def _start_time(self, pid: int) -> str:
        raw = (self.proc_root / str(pid) / "stat").read_text(encoding="utf-8")
        prefix, separator, fields = raw.rpartition(") ")
        if not separator or not prefix.startswith(str(pid) + " ("):
            raise ValueError()
        fields = fields.split()
        if fields[2] != str(pid):
            raise ValueError()
        start = fields[19]
        if not start.isascii() or not start.isdigit():
            raise ValueError()
        return start

    def read(self, pid: int) -> Optional[ObservedProcessIdentity]:
        if type(pid) is not int or pid <= 1:
            return None
        try:
            start = self._start_time(pid)
            raw = (self.proc_root / str(pid) / "cmdline").read_bytes()
            cmdline = tuple(os.fsdecode(argument) for argument in raw.rstrip(b"\0").split(b"\0"))
            if self._start_time(pid) != start:
                return None
            return ObservedProcessIdentity(start, cmdline)
        except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
            return None


class OsSignalSender:
    def send_group(self, pgid: int, signal_number: int) -> None:
        if type(pgid) is not int or pgid <= 1:
            raise recovery_error("invalid process group identity")
        os.killpg(pgid, signal_number)


class RecoveryService:
    def __init__(self, paths: RuntimePaths, repository: StateRepository, pointers: AtomicPointers,
                 renderer: _RendererRestorer, identity_reader: Optional[ProcessIdentityReader] = None,
                 signal_sender: Optional[SignalSender] = None, stale_after_seconds: float = 300.0,
                 term_grace_seconds: float = 1.0, platform_name: str = sys.platform,
                 wall_clock: Callable[[], float] = time.time) -> None:
        for interval in (stale_after_seconds, term_grace_seconds):
            if type(interval) not in (int, float) or not math.isfinite(interval) or interval < 0:
                raise recovery_error("invalid recovery interval")
        self.paths, self.repository, self.pointers, self.renderer = paths, repository, pointers, renderer
        self.identity_reader = identity_reader if identity_reader is not None else LinuxProcessIdentityReader()
        self.signal_sender = signal_sender if signal_sender is not None else OsSignalSender()
        self.stale_after_seconds, self.term_grace_seconds = stale_after_seconds, term_grace_seconds
        self.platform_name, self.wall_clock = platform_name, wall_clock

    def _remove_artifact(self, path: Path, root: Path, suffix: str) -> None:
        if root not in (self.paths.downloads_root, self.paths.tmp_root) or path.parent != root or not path.name.endswith(suffix):
            raise recovery_error("unowned transaction artifact")
        self.paths.validate_parent(path)
        info = path.lstat()
        if self.wall_clock() - info.st_mtime <= self.stale_after_seconds:
            return
        if stat.S_ISLNK(info.st_mode):
            path.unlink()
        else:
            resolved = path.resolve(strict=True)
            if resolved.parent != root or resolved == root:
                raise recovery_error("transaction artifact containment failed")
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(str(path))
            else:
                path.unlink()
        fsync_directory(root)

    def _clean_artifacts(self, failures: List[str]) -> None:
        for root, suffix in ((self.paths.downloads_root, ".part"), (self.paths.tmp_root, ".staging")):
            try:
                self.paths.validate_parent(root / "sentinel")
                children = list(root.iterdir())
            except Exception:
                failures.append("artifact scan")
                continue
            for path in children:
                if path.name.endswith(suffix):
                    try:
                        self._remove_artifact(path, root, suffix)
                    except Exception:
                        failures.append("artifact cleanup")

    def _matches(self, identity: BackendProcessIdentity) -> bool:
        observed = self.identity_reader.read(identity.pid)
        return (observed is not None and observed.start_time == identity.start_time
                and identity.command_marker in observed.cmdline)

    async def _recover_process(self, identity: Optional[BackendProcessIdentity]) -> None:
        if not self.platform_name.startswith("linux"):
            logging.getLogger(__name__).warning("PID recovery is unavailable on this platform")
            return
        if identity is None:
            return
        marker = Path(identity.command_marker)
        relative = marker.relative_to(self.paths.apps_root)
        if (len(relative.parts) < 4 or relative.parts[2] != "backend"
                or marker.resolve(strict=True) != marker or marker.is_symlink() or not marker.is_file()):
            raise recovery_error("invalid installed process marker")
        _expect_app_id(relative.parts[0])
        _expect_version(relative.parts[1])
        self.paths.validate_parent(marker)
        try:
            if not self._matches(identity):
                return
            self.signal_sender.send_group(identity.pid, signal.SIGTERM)
            await asyncio.sleep(self.term_grace_seconds)
            if self._matches(identity):
                self.signal_sender.send_group(identity.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    async def recover(self) -> None:
        failures: List[str] = []
        state = None
        preserve_evidence = False
        try:
            try:
                state = self.repository.load()
            except Exception:
                failures.append("state load")
            self._clean_artifacts(failures)
            try:
                self.pointers.validate_and_remove_invalid()
            except Exception:
                failures.append("pointer cleanup")
            if state is not None:
                try:
                    if state.state == RuntimeState.RUNNING:
                        self.pointers.set_current_web(None)
                    elif state.pointer_snapshot is not None:
                        self.pointers.restore(state.pointer_snapshot)
                    elif state.state != RuntimeState.IDLE:
                        self.pointers.set_current_web(None)
                except Exception:
                    failures.append("pointer rollback")
                    preserve_evidence = True
                try:
                    await self._recover_process(state.backend_process)
                except Exception:
                    failures.append("process cleanup")
        finally:
            try:
                await self.renderer.restore_default()
            except Exception:
                failures.append("renderer restore")
            finally:
                if not preserve_evidence:
                    try:
                        self.repository.save(PersistedRuntimeState(state=RuntimeState.IDLE))
                    except Exception:
                        failures.append("idle persistence")
        if failures:
            raise recovery_error("recovery failed: " + ", ".join(failures))
