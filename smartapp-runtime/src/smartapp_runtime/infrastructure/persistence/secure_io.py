import os
from pathlib import Path


_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
)


def _component(value: str) -> str:
    if not isinstance(value, str) or value in ("", ".", "..") or "/" in value:
        raise ValueError("invalid path component")
    return value


def open_child_directory(
    parent_descriptor: int,
    name: str,
    create: bool = False,
    mode: int = 0o750,
) -> int:
    name = _component(name)
    try:
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError:
        if not create:
            raise
        os.mkdir(name, mode=mode, dir_fd=parent_descriptor)
        return os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)


def open_directory(path: Path, create_leaf: bool = False, mode: int = 0o750) -> int:
    path = Path(path)
    if not path.is_absolute() or any(part in (".", "..") for part in path.parts[1:]):
        raise ValueError("directory path must be normalized and absolute")
    descriptor = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        parts = path.parts[1:]
        for index, raw_name in enumerate(parts):
            next_descriptor = open_child_directory(
                descriptor,
                raw_name,
                create=create_leaf and index == len(parts) - 1,
                mode=mode,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def open_file(
    parent_descriptor: int,
    name: str,
    flags: int,
    mode: int,
) -> int:
    return os.open(
        _component(name),
        flags | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=parent_descriptor,
    )
