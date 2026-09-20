import errno
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError


def recovery_error(message: str) -> SmartAppError:
    return SmartAppError(ErrorCode.RECOVERY_FAILED, message)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.fsync(descriptor)
        except OSError as error:
            if error.errno not in (errno.EINVAL, errno.ENOTSUP):
                raise
    finally:
        os.close(descriptor)


def require_directory(path: Path) -> None:
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise recovery_error("runtime directory is not an owned directory")


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    apps_root: Path
    downloads_root: Path
    tmp_root: Path
    state_root: Path
    state_file: Path
    logs_root: Path
    current_web: Path
    lock_file: Path

    @classmethod
    def from_root(cls, root: Path) -> "RuntimePaths":
        supplied = Path(root).absolute()
        root = supplied.parent.resolve() / supplied.name
        return cls(root, root / "apps", root / "downloads", root / "tmp",
                   root / "state", root / "state" / "runtime-state.json",
                   root / "logs", root / "current_web", root / "state" / "runtime.lock")

    def ensure_layout(self) -> None:
        try:
            for path in (self.root, self.apps_root, self.downloads_root,
                         self.tmp_root, self.state_root, self.logs_root):
                if not os.path.lexists(str(path)):
                    path.mkdir(mode=0o750)
                    fsync_directory(path.parent)
                require_directory(path)
                path.chmod(0o750)
        except OSError:
            raise recovery_error("cannot create runtime layout") from None

    def validate_parent(self, path: Path) -> None:
        """Validate lexical ownership and every parent without following links."""
        try:
            relative = path.parent.relative_to(self.root)
            if ".." in relative.parts:
                raise ValueError()
            require_directory(self.root)
            current = self.root
            for part in relative.parts:
                current = current / part
                require_directory(current)
            if path.parent.resolve(strict=True) != path.parent:
                raise ValueError()
        except (OSError, ValueError, RuntimeError):
            raise recovery_error("runtime path containment validation failed") from None
