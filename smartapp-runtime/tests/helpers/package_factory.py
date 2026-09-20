import gzip
import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Mapping, Optional

from smartapp_runtime.ports.downloader import DownloadRequest, DownloadResult


FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures" / "apps"


@dataclass(frozen=True)
class PackageBytes:
    data: bytes
    size: int
    sha256: str


def _manifest_bytes(source: bytes, app_id: str, version: str) -> bytes:
    manifest = json.loads(source.decode("utf-8"))
    manifest["appId"] = app_id
    manifest["version"] = version
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def fixture_files(name: str, app_id: Optional[str] = None,
                  version: str = "1") -> Dict[str, bytes]:
    root = FIXTURE_ROOT / name
    if not root.is_dir():
        raise ValueError("unknown app fixture: {0}".format(name))
    selected_app_id = app_id or name
    files = {}
    for path in sorted(root.rglob("*")):
        relative_path = path.relative_to(root)
        if (not path.is_file() or "__pycache__" in relative_path.parts
                or path.suffix in (".pyc", ".pyo")):
            continue
        relative = relative_path.as_posix()
        data = path.read_bytes()
        if relative == "manifest.json":
            data = _manifest_bytes(data, selected_app_id, version)
        files[relative] = data
    return files


def _encode_members(members: Mapping[str, bytes]) -> PackageBytes:
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(members):
            data = bytes(members[name])
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.mode = 0o644
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", compresslevel=9,
                       fileobj=compressed, mtime=0) as output:
        output.write(tar_stream.getvalue())
    data = compressed.getvalue()
    return PackageBytes(data, len(data), hashlib.sha256(data).hexdigest())


def build_archive(app_id: str, files: Mapping[str, bytes]) -> PackageBytes:
    members = {}
    for relative_name, data in files.items():
        relative = PurePosixPath(relative_name)
        if (relative.is_absolute() or not relative.parts
                or any(part in ("", ".", "..") for part in relative.parts)):
            raise ValueError("fixture member path must be safe and relative")
        members["{0}/{1}".format(app_id, relative.as_posix())] = data
    return _encode_members(members)


def build_fixture(name: str, app_id: Optional[str] = None,
                  version: str = "1",
                  replacements: Optional[Mapping[str, bytes]] = None) -> PackageBytes:
    selected_app_id = app_id or name
    files = fixture_files(name, selected_app_id, version)
    files.update(replacements or {})
    return build_archive(selected_app_id, files)


def build_unsafe_path_archive(app_id: str, version: str = "1") -> PackageBytes:
    files = fixture_files("web_only", app_id, version)
    members = {
        "{0}/{1}".format(app_id, relative): data
        for relative, data in files.items()
    }
    members["{0}/../../escaped.txt".format(app_id)] = b"must-not-escape"
    return _encode_members(members)


class MemoryDownloader:
    def __init__(self, packages: Mapping[str, bytes]) -> None:
        self.packages = dict(packages)
        self.calls = []

    async def download(self, request: DownloadRequest, destination: Path) -> DownloadResult:
        self.calls.append(request)
        try:
            data = self.packages[request.url]
        except KeyError:
            raise AssertionError("unexpected package URL: {0}".format(request.url))
        with destination.open("xb") as output:
            output.write(data)
        return DownloadResult(len(data), hashlib.sha256(data).hexdigest())
