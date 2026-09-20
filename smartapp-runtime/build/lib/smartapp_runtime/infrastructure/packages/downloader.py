import asyncio
import hashlib
import http.client
import io
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.ports.downloader import DownloadRequest, DownloadResult


async def await_worker(future, cancelled=None):
    """Keep ownership of worker-written paths until the worker has actually stopped."""
    try:
        await asyncio.wait((future,))
        return future.result()
    except asyncio.CancelledError:
        if cancelled is not None:
            cancelled.set()
        while not future.done():
            try:
                await asyncio.wait((future,))
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not future.cancelled():
            future.exception()
        raise


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


class _DeadlineRaw(io.RawIOBase):
    def __init__(self, source, sock, remaining):
        super().__init__()
        self._source, self._sock, self._remaining = source, sock, remaining

    def readable(self):
        return True

    def readinto(self, buffer):
        self._sock.settimeout(self._remaining())
        count = self._source.readinto(buffer)
        self._remaining()
        return count

    def close(self):
        try:
            self._source.close()
        finally:
            super().close()


class _DeadlineSocket:
    def __init__(self, sock, remaining):
        self._sock, self._remaining = sock, remaining

    def makefile(self, mode):
        raw = self._sock.makefile(mode, buffering=0)
        return io.BufferedReader(_DeadlineRaw(raw, self._sock, self._remaining))


class _DeadlineHTTPSHandler(HTTPSHandler):
    def __init__(self, remaining):
        super().__init__()
        self._remaining = remaining

    def https_open(self, request):
        remaining = self._remaining

        class DeadlineResponse(http.client.HTTPResponse):
            def __init__(self, sock, *args, **kwargs):
                # Install the raw I/O guard before begin() parses the status/header
                # lines. It also guards every read inside chunk extensions/trailers.
                super().__init__(_DeadlineSocket(sock, remaining), *args, **kwargs)

        def connection(host, **kwargs):
            kwargs["timeout"] = remaining()
            result = http.client.HTTPSConnection(host, **kwargs)
            result.response_class = DeadlineResponse
            return result

        return self.do_open(connection, request, context=self._context)


def _failed() -> SmartAppError:
    return SmartAppError(ErrorCode.DOWNLOAD_FAILED, "HTTPS package download failed")


class HttpsDownloader:
    def __init__(self, opener=None, clock=time.monotonic) -> None:
        self._opener = opener
        self._clock = clock

    async def download(self, request: DownloadRequest, destination: Path) -> DownloadResult:
        cancelled = threading.Event()
        future = asyncio.get_running_loop().run_in_executor(
            None, self._download, request, destination, cancelled)
        return await await_worker(future, cancelled)

    def _download(self, request, destination, cancelled):
        deadline = self._clock() + request.timeout

        def remaining():
            value = deadline - self._clock()
            if cancelled.is_set() or value <= 0:
                raise _failed()
            return value

        def https(url):
            parsed = urlsplit(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise _failed()

        if request.expected_size > request.max_bytes:
            raise SmartAppError(ErrorCode.PACKAGE_TOO_LARGE, "package exceeds download limit")
        try:
            opener = self._opener or build_opener(_NoRedirect(), _DeadlineHTTPSHandler(remaining)).open
            url = request.url
            for redirects in range(4):
                https(url)
                try:
                    response = opener(Request(url), timeout=remaining())
                except HTTPError as error:
                    response = error
                with response:
                    status = response.status
                    if status in (301, 302, 303, 307, 308):
                        location = response.headers.get("Location")
                        if not location or redirects == 3:
                            raise _failed()
                        url = urljoin(url, location)
                        https(url)
                        continue
                    if status != 200:
                        raise _failed()
                    remaining()
                    size = 0
                    digest = hashlib.sha256()
                    with destination.open("xb") as output:
                        while True:
                            remaining()
                            read = getattr(response, "read1", response.read)
                            chunk = read(min(65536, min(request.expected_size, request.max_bytes) - size + 1))
                            remaining()
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > request.max_bytes:
                                raise SmartAppError(ErrorCode.PACKAGE_TOO_LARGE, "package exceeds download limit")
                            if size > request.expected_size:
                                raise SmartAppError(ErrorCode.SIZE_MISMATCH, "download size does not match declaration")
                            output.write(chunk)
                            digest.update(chunk)
                        output.flush()
                    if size != request.expected_size:
                        raise SmartAppError(ErrorCode.SIZE_MISMATCH, "download size does not match declaration")
                    observed = digest.hexdigest()
                    if observed != request.expected_sha256.lower():
                        raise SmartAppError(ErrorCode.HASH_MISMATCH, "download digest does not match declaration")
                    return DownloadResult(size, observed)
            raise _failed()
        except SmartAppError:
            raise
        except (OSError, URLError, ValueError, EOFError, http.client.HTTPException):
            raise _failed() from None
