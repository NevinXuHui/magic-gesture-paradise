import fcntl
import os
import stat
from pathlib import Path
from typing import Optional

from smartapp_runtime.infrastructure.persistence.paths import recovery_error
from smartapp_runtime.infrastructure.persistence.secure_io import (
    open_child_directory,
    open_directory,
    open_file,
)


class SingleInstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = Path(path).absolute()
        self._descriptor: Optional[int] = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        descriptor = None
        parent_descriptor = None
        try:
            parent_descriptor = self._open_parent()
            descriptor = open_file(
                parent_descriptor,
                self.path.name,
                os.O_RDWR | os.O_CREAT | os.O_NONBLOCK,
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise recovery_error("lock path is not a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.fchmod(descriptor, 0o600)
            self._descriptor = descriptor
            descriptor = None
        except OSError:
            raise recovery_error("cannot acquire runtime instance lock") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if parent_descriptor is not None:
                os.close(parent_descriptor)

    def _open_parent(self) -> int:
        state_path = self.path.parent
        root_path = state_path.parent
        if self.path.name != "runtime.lock" or state_path.name != "state":
            raise recovery_error("runtime lock path is not owned")
        ancestor_descriptor = None
        root_descriptor = None
        try:
            ancestor_descriptor = open_directory(root_path.parent)
            root_descriptor = open_child_directory(
                ancestor_descriptor, root_path.name, create=True
            )
            return open_child_directory(root_descriptor, state_path.name, create=True)
        except (OSError, RuntimeError, ValueError):
            raise recovery_error("cannot create runtime lock parent") from None
        finally:
            if root_descriptor is not None:
                os.close(root_descriptor)
            if ancestor_descriptor is not None:
                os.close(ancestor_descriptor)

    def release(self) -> None:
        if self._descriptor is None:
            return
        descriptor, self._descriptor = self._descriptor, None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.release()
