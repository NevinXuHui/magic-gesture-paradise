import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import List, Optional

from smartapp_runtime.domain.commands import _expect_app_id, _expect_version
from smartapp_runtime.domain.errors import SmartAppError
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths, fsync_directory, recovery_error
from smartapp_runtime.ports.repository import PointerSnapshot


class AtomicPointers:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def _app_pointer(self, app_id: str, name: str) -> Path:
        try:
            _expect_app_id(app_id)
        except SmartAppError:
            raise recovery_error("invalid pointer app identity") from None
        return self.paths.apps_root / app_id / name

    def _validate_owned(self, pointer: Path) -> None:
        if pointer != self.paths.current_web:
            if pointer.name not in ("current", "previous") or pointer.parent.parent != self.paths.apps_root:
                raise recovery_error("unowned pointer path")
            self._app_pointer(pointer.parent.name, pointer.name)
        self.paths.validate_parent(pointer)

    def _target(self, pointer: Path, raw: str) -> Path:
        self._validate_owned(pointer)
        try:
            target = (pointer.parent / raw).resolve(strict=True)
            relative = target.relative_to(self.paths.apps_root)
            parts = relative.parts
            if not target.is_dir() or len(parts) != (3 if pointer == self.paths.current_web else 2):
                raise ValueError()
            _expect_app_id(parts[0])
            _expect_version(parts[1])
            if pointer == self.paths.current_web:
                if parts[2] != "web":
                    raise ValueError()
            elif parts[0] != pointer.parent.name:
                raise ValueError()
            self.paths.validate_parent(target)
            return target
        except (OSError, ValueError, RuntimeError, SmartAppError):
            raise recovery_error("invalid pointer target") from None

    def _raw(self, pointer: Path) -> Optional[str]:
        self._validate_owned(pointer)
        if not os.path.lexists(str(pointer)):
            return None
        if not pointer.is_symlink():
            raise recovery_error("unexpected object at pointer")
        raw = os.readlink(str(pointer))
        self._target(pointer, raw)
        return raw

    def _write(self, pointer: Path, raw: Optional[str]) -> None:
        temporary = None
        created = False
        owned = None
        try:
            self._validate_owned(pointer)
            if os.path.lexists(str(pointer)) and not pointer.is_symlink():
                raise recovery_error("unexpected object at pointer")
            if raw is None:
                if pointer.is_symlink():
                    self._validate_owned(pointer)
                    pointer.unlink()
                    fsync_directory(pointer.parent)
                return
            self._target(pointer, raw)
            temporary = pointer.with_name("." + pointer.name + "." + uuid.uuid4().hex)
            temporary.symlink_to(raw)
            created = True
            info = temporary.lstat()
            owned = (info.st_dev, info.st_ino)
            self._validate_owned(pointer)
            if os.path.lexists(str(pointer)) and not pointer.is_symlink():
                raise recovery_error("unexpected object at pointer")
            os.replace(str(temporary), str(pointer))
            created = False
            fsync_directory(pointer.parent)
        except OSError:
            raise recovery_error("pointer replacement failed") from None
        finally:
            if created and temporary is not None:
                try:
                    self._validate_owned(pointer)
                    self.paths.validate_parent(temporary)
                    info = temporary.lstat()
                    if stat.S_ISLNK(info.st_mode) and (info.st_dev, info.st_ino) == owned:
                        temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise recovery_error("cannot clean temporary pointer") from None

    def snapshot(self, app_id: str) -> PointerSnapshot:
        return PointerSnapshot(app_id, self._raw(self._app_pointer(app_id, "current")),
                               self._raw(self._app_pointer(app_id, "previous")),
                               self._raw(self.paths.current_web))

    def set_current(self, app_id: str, version_root: Path) -> None:
        pointer = self._app_pointer(app_id, "current")
        self._write(pointer, os.path.relpath(str(version_root), str(pointer.parent)))

    def set_previous(self, app_id: str, version_root: Optional[Path]) -> None:
        pointer = self._app_pointer(app_id, "previous")
        self._write(pointer, None if version_root is None else os.path.relpath(str(version_root), str(pointer.parent)))

    def set_current_web(self, web_root: Optional[Path]) -> None:
        self._write(self.paths.current_web, None if web_root is None else os.path.relpath(str(web_root), str(self.paths.root)))

    def restore(self, snapshot: PointerSnapshot) -> None:
        entries = ((self._app_pointer(snapshot.app_id, "current"), snapshot.current),
                   (self._app_pointer(snapshot.app_id, "previous"), snapshot.previous),
                   (self.paths.current_web, snapshot.current_web))
        for pointer, raw in entries:
            if raw is not None:
                self._target(pointer, raw)
        for pointer, raw in entries:
            self._write(pointer, raw)

    def current_target(self, app_id: str) -> Optional[Path]:
        return self._read_target(self._app_pointer(app_id, "current"))

    def previous_target(self, app_id: str) -> Optional[Path]:
        return self._read_target(self._app_pointer(app_id, "previous"))

    def current_web_target(self) -> Optional[Path]:
        return self._read_target(self.paths.current_web)

    def _read_target(self, pointer: Path) -> Optional[Path]:
        raw = self._raw(pointer)
        return None if raw is None else self._target(pointer, raw)

    def validate_and_remove_invalid(self) -> List[Path]:
        self.paths.validate_parent(self.paths.apps_root / "sentinel")
        pointers = [self.paths.current_web]
        for app in self.paths.apps_root.iterdir():
            if app.is_symlink() or not app.is_dir():
                continue
            try:
                _expect_app_id(app.name)
            except SmartAppError:
                continue
            pointers.extend((app / "current", app / "previous"))
        removed = []
        for pointer in pointers:
            try:
                self._raw(pointer)
            except SmartAppError:
                self._validate_owned(pointer)
                if not os.path.lexists(str(pointer)):
                    continue
                if pointer.is_symlink() or not pointer.is_dir():
                    pointer.unlink()
                else:
                    shutil.rmtree(str(pointer))
                fsync_directory(pointer.parent)
                removed.append(pointer)
        return removed
