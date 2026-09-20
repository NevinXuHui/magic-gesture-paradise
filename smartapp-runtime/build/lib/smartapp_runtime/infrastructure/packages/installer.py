import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from smartapp_runtime.config import RuntimeConfig
from smartapp_runtime.domain.commands import StartApp
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest, load_manifest
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.packages.archive import SafeArchiveExtractor
from smartapp_runtime.infrastructure.packages.downloader import await_worker
from smartapp_runtime.ports.downloader import Downloader, DownloadRequest


@dataclass(frozen=True)
class InstalledApp:
    root: Path
    manifest: Manifest
    sha256: str
    package_size: int
    cache_hit: bool


def _error(code, message):
    return SmartAppError(code, message)


def _exists(path):
    return os.path.lexists(str(path))


def _regular(path):
    return not path.is_symlink() and path.is_file()


def _fsync_directory(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate metadata field")
        value[key] = item
    return value


class PackageInstaller:
    def __init__(self, config: RuntimeConfig, downloader: Downloader, extractor=None) -> None:
        self.config = config
        self.downloader = downloader
        self.extractor = extractor or SafeArchiveExtractor(config.limits)

    async def ensure_installed(
        self,
        command: StartApp,
        progress: Optional[Callable[[RuntimeState], Awaitable[None]]] = None,
    ) -> InstalledApp:
        # Reuse the domain boundary even for programmatically constructed commands.
        command = StartApp.from_dict(command.to_dict())
        if command.package_size > self.config.limits.max_package_bytes:
            raise _error(ErrorCode.PACKAGE_TOO_LARGE, "package exceeds configured limit")
        loop = asyncio.get_running_loop()
        cancelled = threading.Event()
        transaction = {}

        async def run(function, *args):
            return await await_worker(loop.run_in_executor(None, function, *args), cancelled)

        try:
            cached = await run(self._prepare, command, transaction)
            if cached is not None:
                return cached
            if progress is not None:
                await progress(RuntimeState.DOWNLOADING)
            request = DownloadRequest(command.package_url, command.package_size,
                                      command.sha256, self.config.limits.max_package_bytes,
                                      self.config.timeouts.download)
            observed = await self.downloader.download(request, transaction["part"])
            if progress is not None:
                await progress(RuntimeState.VERIFYING)
            await run(self._verify_download, command, transaction, observed, cancelled)
            if progress is not None:
                await progress(RuntimeState.INSTALLING)
            return await run(self._install, command, transaction, cancelled)
        except SmartAppError:
            raise
        except OSError:
            raise _error(ErrorCode.INTERNAL_ERROR, "package installation filesystem operation failed") from None
        finally:
            # A cancelled await cannot relinquish ownership while an executor is writing.
            await await_worker(loop.run_in_executor(None, self._cleanup, transaction))

    def _prepare(self, command, transaction):
        root = self.config.paths.root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        for name in ("apps", "downloads", "tmp"):
            path = root / name
            if path.is_symlink():
                raise _error(ErrorCode.INSTALL_CONFLICT, "package directory is not trusted")
            path.mkdir(exist_ok=True)
            path.resolve().relative_to(root)
        parent = root / "apps" / command.app_id
        if parent.is_symlink():
            raise _error(ErrorCode.INSTALL_CONFLICT, "application directory is not trusted")
        parent.mkdir(exist_ok=True)
        parent.resolve().relative_to(root / "apps")
        final = parent / command.version
        cached = self._cache(final, command)
        if cached is not None:
            return cached
        token = uuid.uuid4().hex
        staging = root / "tmp" / (token + ".staging")
        part = root / "downloads" / (token + ".part")
        staging.mkdir(mode=0o700)
        transaction.update(root=root, final=final, staging=staging)
        if _exists(part):
            raise _error(ErrorCode.INSTALL_CONFLICT, "random download path already exists")
        transaction["part"] = part
        return None

    def _validate_entries(self, root, manifest):
        for directory, component in (("web", manifest.web), ("backend", manifest.backend)):
            if not component.enabled:
                continue
            path = root / directory / component.entry
            try:
                path.resolve(strict=True).relative_to(root.resolve())
                cursor = path
                while cursor != root:
                    if cursor.is_symlink():
                        raise ValueError("symlink")
                    cursor = cursor.parent
                if not _regular(path):
                    raise ValueError("not a regular file")
            except (OSError, ValueError, RuntimeError):
                raise _error(ErrorCode.MANIFEST_INVALID, "enabled manifest entry is not a contained regular file") from None

    def _cache(self, final, command):
        if not _exists(final):
            return None
        try:
            manifest_path = final / "manifest.json"
            metadata_path = final / ".smartapp-install.json"
            if (final.is_symlink() or not final.is_dir() or not _regular(manifest_path)
                    or not _regular(metadata_path) or metadata_path.stat().st_size > 4096):
                raise ValueError("invalid cache nodes")
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
            if (type(metadata) is not dict or set(metadata) != {"sha256", "packageSize", "installedAt"}
                    or type(metadata["packageSize"]) is not int
                    or metadata["packageSize"] != command.package_size
                    or metadata["sha256"] != command.sha256
                    or type(metadata["installedAt"]) is not str):
                raise ValueError("cache identity mismatch")
            stamp = datetime.fromisoformat(metadata["installedAt"])
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                raise ValueError("timestamp must be UTC")
            manifest = load_manifest(manifest_path, command.app_id, command.version)
            self._validate_entries(final, manifest)
            return InstalledApp(final, manifest, command.sha256, command.package_size, True)
        except (OSError, ValueError, TypeError, SmartAppError, RuntimeError):
            raise _error(ErrorCode.INSTALL_CONFLICT, "existing application version cannot prove identical content") from None

    def _verify_download(self, command, transaction, observed, cancelled):
        def checkpoint():
            if cancelled.is_set():
                raise _error(ErrorCode.INTERNAL_ERROR, "package installation cancelled")

        part = transaction["part"]
        if not _regular(part):
            raise _error(ErrorCode.DOWNLOAD_FAILED, "download did not produce a regular package")
        size = 0
        digest = hashlib.sha256()
        with part.open("rb") as source:
            while True:
                checkpoint()
                chunk = source.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.config.limits.max_package_bytes:
                    raise _error(ErrorCode.PACKAGE_TOO_LARGE, "download exceeds configured limit")
                if size > command.package_size:
                    raise _error(ErrorCode.SIZE_MISMATCH, "download size differs from declaration")
                digest.update(chunk)
        if size != command.package_size or observed.size != size:
            raise _error(ErrorCode.SIZE_MISMATCH, "download size differs from declaration")
        if digest.hexdigest() != command.sha256 or observed.sha256.lower() != command.sha256:
            raise _error(ErrorCode.HASH_MISMATCH, "download digest differs from declaration")
        checkpoint()

    def _install(self, command, transaction, cancelled):
        def checkpoint():
            if cancelled.is_set():
                raise _error(ErrorCode.INTERNAL_ERROR, "package installation cancelled")

        part = transaction["part"]
        app_root, manifest = self.extractor.extract(part, transaction["staging"], command.app_id, command.version)
        app_root.resolve().relative_to(transaction["staging"].resolve())
        self._validate_entries(app_root, manifest)
        checkpoint()
        metadata = {"sha256": command.sha256, "packageSize": command.package_size,
                    "installedAt": datetime.now(timezone.utc).isoformat()}
        with (app_root / ".smartapp-install.json").open("x", encoding="utf-8") as output:
            json.dump(metadata, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        _fsync_directory(app_root)
        final = transaction["final"]
        # Lock the owned application directory, avoiding persistent lock files and
        # serializing the final existence check across installer instances/processes.
        fd = os.open(str(final.parent), os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            checkpoint()
            cached = self._cache(final, command)
            if cached is not None:
                return cached
            os.replace(str(app_root), str(final))
            os.fsync(fd)
        finally:
            os.close(fd)
        return InstalledApp(final, manifest, command.sha256, command.package_size, False)

    def _cleanup(self, transaction):
        for key, directory, suffix in (("part", "downloads", ".part"), ("staging", "tmp", ".staging")):
            path = transaction.get(key)
            if path is None:
                continue
            try:
                parent = transaction["root"] / directory
                if path.parent != parent or path.suffix != suffix or path.is_symlink():
                    continue
                path.resolve().relative_to(parent.resolve())
                parent.resolve().relative_to(transaction["root"])
                if key == "part" and path.is_file():
                    path.unlink()
                elif key == "staging" and path.is_dir():
                    shutil.rmtree(str(path))
            except (OSError, ValueError):
                # Cleanup must not replace the primary transport/validation error.
                continue
