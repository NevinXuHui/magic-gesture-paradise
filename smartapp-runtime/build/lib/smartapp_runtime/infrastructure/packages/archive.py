import os
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Tuple

from smartapp_runtime.config import LimitConfig
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest, load_manifest


def _unsafe() -> SmartAppError:
    return SmartAppError(ErrorCode.ARCHIVE_UNSAFE, "archive violates package safety constraints")


class SafeArchiveExtractor:
    def __init__(self, limits: LimitConfig) -> None:
        self.limits = limits

    def extract(self, archive_path: Path, staging_root: Path,
                expected_app_id: str, expected_version: str) -> Tuple[Path, Manifest]:
        try:
            if staging_root.is_symlink() or not staging_root.is_dir() or any(staging_root.iterdir()):
                raise _unsafe()
            base = staging_root.resolve()
            seen = set()
            declared_total = actual_total = 0
            limits = self.limits

            class BoundedTarInfo(tarfile.TarInfo):
                headers = 0
                extension_chain = 0
                extension_bytes = 0
                ended = False

                @classmethod
                def fromtarfile(cls, archive):
                    return cls._read_header(archive)

                @classmethod
                def _fromtarfile(cls, archive, **kwargs):
                    # Newer Python versions use this entry for recursive PAX reads.
                    return cls._read_header(archive)

                @classmethod
                def _read_header(cls, archive):
                    block = archive.fileobj.read(tarfile.BLOCKSIZE)
                    if block == bytes(tarfile.BLOCKSIZE):
                        if archive.fileobj.read(tarfile.BLOCKSIZE) != bytes(tarfile.BLOCKSIZE):
                            raise _unsafe()
                        cls.ended = True
                        raise tarfile.EOFHeaderError("validated double-zero terminator")
                    try:
                        member = cls.frombuf(block, archive.encoding, archive.errors)
                    except tarfile.HeaderError:
                        # TarFile.next silently treats some invalid/short headers as
                        # EOF. Convert them before its permissive exception handler.
                        raise _unsafe() from None
                    member.offset = archive.fileobj.tell() - tarfile.BLOCKSIZE
                    return member._proc_member(archive)

                def _proc_pax(self, archive):
                    position = archive.fileobj.tell()
                    payload = archive.fileobj.read(self.size)
                    if len(payload) != self.size:
                        raise _unsafe()
                    cursor = 0
                    while cursor < len(payload):
                        space = payload.find(b" ", cursor)
                        if space < 0 or space - cursor > 10:
                            raise _unsafe()
                        length = int(payload[cursor:space])
                        end = cursor + length
                        if length < 5 or end > len(payload) or payload[end - 1:end] != b"\n":
                            raise _unsafe()
                        key, separator, value = payload[space + 1:end - 1].partition(b"=")
                        if not key or not separator or key.startswith(b"GNU.sparse."):
                            # Reject every GNU PAX sparse variant before stdlib map
                            # parsing can allocate attacker-controlled mapping lists.
                            raise _unsafe()
                        cursor = end
                    archive.fileobj.seek(position)
                    return super()._proc_pax(archive)

                def _proc_member(self, archive):
                    # tarfile consumes extension/sparse payloads before yielding a
                    # member. Bound these reads before the stdlib allocates them.
                    cls = type(self)
                    cls.headers += 1
                    if cls.headers > limits.max_files or self.type == tarfile.GNUTYPE_SPARSE:
                        raise _unsafe()
                    if self.type in (tarfile.XHDTYPE, tarfile.XGLTYPE,
                                     tarfile.SOLARIS_XHDTYPE, tarfile.GNUTYPE_LONGNAME,
                                     tarfile.GNUTYPE_LONGLINK):
                        cls.extension_chain += 1
                        cls.extension_bytes += self.size
                        if (self.size < 0 or self.size > min(65536, limits.max_unpacked_bytes)
                                or cls.extension_bytes > limits.max_unpacked_bytes
                                or cls.extension_chain > 16):
                            raise _unsafe()
                    else:
                        cls.extension_chain = 0
                    return super()._proc_member(archive)

            with tarfile.open(str(archive_path), mode="r:gz", tarinfo=BoundedTarInfo) as archive:
                for count, member in enumerate(archive, 1):
                    name = member.name
                    parts = name.split("/")
                    relative = PurePosixPath(name)
                    if (not name or "\x00" in name or relative.is_absolute()
                            or any(part in ("", ".", "..") for part in parts)
                            or parts[0] != expected_app_id
                            or len(name.encode("utf-8")) > self.limits.max_path_length
                            or count > self.limits.max_files
                            or not (member.isdir() or member.type in (tarfile.REGTYPE, tarfile.AREGTYPE))
                            or member.sparse is not None
                            or member.mode & 0o6000
                            or str(relative) in seen
                            or member.size < 0
                            or member.size > self.limits.max_file_bytes):
                        raise _unsafe()
                    seen.add(str(relative))
                    declared_total += member.size
                    if declared_total + BoundedTarInfo.extension_bytes > self.limits.max_unpacked_bytes:
                        raise _unsafe()
                    target = base.joinpath(*relative.parts)
                    target.resolve().relative_to(base)
                    # Only this extractor can create nodes beneath the empty staging root.
                    parent = target if member.isdir() else target.parent
                    pending = []
                    while parent != base and not parent.exists():
                        pending.append(parent)
                        parent = parent.parent
                    for directory in reversed(pending):
                        directory.mkdir(mode=0o755)
                        directory.chmod(0o755)
                    if member.isdir():
                        if not target.is_dir() or member.size:
                            raise _unsafe()
                        continue
                    if target == base / expected_app_id:
                        raise _unsafe()
                    source = archive.extractfile(member)
                    if source is None:
                        raise _unsafe()
                    with source, target.open("xb") as output:
                        os.fchmod(output.fileno(), 0o644)
                        copied = 0
                        while True:
                            chunk = source.read(min(65536, member.size - copied + 1))
                            if not chunk:
                                break
                            copied += len(chunk)
                            actual_total += len(chunk)
                            if (copied > member.size or copied > self.limits.max_file_bytes
                                    or actual_total > self.limits.max_unpacked_bytes):
                                raise _unsafe()
                            output.write(chunk)
                        if copied != member.size:
                            raise _unsafe()
                        output.flush()
                        os.fsync(output.fileno())
                if not BoundedTarInfo.ended:
                    raise _unsafe()
                # The two end blocks have been verified while reading headers.
                # Drain bounded padding to also verify gzip CRC and reject appendages.
                padding = archive.fileobj.read(tarfile.RECORDSIZE + 1)
                if len(padding) > tarfile.RECORDSIZE or any(padding):
                    raise _unsafe()
            app_root = base / expected_app_id
            manifest = load_manifest(app_root / "manifest.json", expected_app_id, expected_version)
            return app_root, manifest
        except SmartAppError:
            raise
        except (OSError, ValueError, EOFError, tarfile.TarError, UnicodeError, zlib.error):
            raise _unsafe() from None
