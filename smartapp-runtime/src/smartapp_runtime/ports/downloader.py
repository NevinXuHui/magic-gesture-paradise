from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    expected_size: int
    expected_sha256: str
    max_bytes: int
    timeout: float
    expected_md5: Optional[str] = None


@dataclass(frozen=True)
class DownloadResult:
    size: int
    sha256: str
    md5: Optional[str] = None


class Downloader(Protocol):
    async def download(self, request: DownloadRequest, destination: Path) -> DownloadResult:
        ...
