import asyncio
import hashlib
import http.client
import io
import json
import tarfile
import tempfile
import threading
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.config import PathsConfig, RuntimeConfig
from smartapp_runtime.domain.commands import StartApp
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.packages.downloader import HttpsDownloader
from smartapp_runtime.infrastructure.packages.installer import PackageInstaller
from smartapp_runtime.ports.downloader import DownloadRequest, DownloadResult


def package(version="1", entry=True):
    manifest = {"schemaVersion": 1, "appId": "demo", "version": version,
                "web": {"enabled": True, "entry": "index.html"},
                "backend": {"enabled": False, "entry": "main.py", "dynamicService": False},
                "routing": {"defaultTarget": "web"}}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        files = [("demo/manifest.json", json.dumps(manifest).encode())]
        if entry:
            files.append(("demo/web/index.html", b"hello"))
        for name, data in files:
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return stream.getvalue()


class FakeDownloader:
    def __init__(self, data, result=None):
        self.data, self.result, self.calls = data, result, 0

    async def download(self, request, destination):
        self.calls += 1
        with destination.open("xb") as output:
            output.write(self.data)
        return self.result or DownloadResult(len(self.data), hashlib.sha256(self.data).hexdigest())


class Response(io.BytesIO):
    def __init__(self, data=b"abc", status=200, location=None):
        super().__init__(data)
        self.status = status
        self.headers = {"Location": location} if location else {}


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.destination = Path(self.temp.name) / "download.part"
        self.request = DownloadRequest("https://example.invalid/pkg", 3,
                                       hashlib.sha256(b"abc").hexdigest(), 10, 1.0)

    async def test_streams_observed_bytes_digest_and_exclusive_destination(self):
        downloader = HttpsDownloader(opener=lambda request, timeout: Response())
        result = await downloader.download(self.request, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"abc")
        self.assertEqual(result, DownloadResult(3, self.request.expected_sha256))
        with self.assertRaises(SmartAppError):
            await downloader.download(self.request, self.destination)
        self.assertEqual(self.destination.read_bytes(), b"abc")

    async def test_md5_declaration_is_checked_during_download(self):
        request = replace(self.request, expected_sha256="", expected_md5=hashlib.md5(b"abc").hexdigest())
        downloader = HttpsDownloader(opener=lambda request, timeout: Response())
        result = await downloader.download(request, self.destination)
        self.assertEqual(result.md5, request.expected_md5)
        self.assertEqual(result.sha256, hashlib.sha256(b"abc").hexdigest())
        bad = replace(request, expected_md5="0" * 32)
        with self.assertRaises(SmartAppError) as raised:
            await downloader.download(bad, self.destination.with_name("bad.part"))
        self.assertEqual(raised.exception.code, ErrorCode.HASH_MISMATCH)

    async def test_http_initial_and_redirect_urls_are_rejected_without_credentials(self):
        for url, redirect in (("http://example.invalid/p", False),
                              ("http://user:secret@example.invalid/p?token=secret", False),
                              ("https://example.invalid/p", True)):
            with self.subTest(url=url):
                def opener(request, timeout):
                    self.assertTrue(redirect, "insecure initial URL reached transport")
                    return Response(status=302, location="http://user:secret@example.invalid/p")
                with self.assertRaises(SmartAppError) as raised:
                    await HttpsDownloader(opener=opener).download(replace(self.request, url=url), self.destination)
                self.assertEqual(raised.exception.code, ErrorCode.DOWNLOAD_FAILED)
                self.assertNotIn("secret", str(raised.exception))

    async def test_redirect_limit_stops_fourth_redirect(self):
        calls = []
        def opener(request, timeout):
            calls.append(request.full_url)
            return Response(status=302, location="/next" + str(len(calls)))
        with self.assertRaises(SmartAppError) as raised:
            await HttpsDownloader(opener=opener).download(self.request, self.destination)
        self.assertEqual(raised.exception.code, ErrorCode.DOWNLOAD_FAILED)
        self.assertEqual(len(calls), 4)

    async def test_size_hash_and_absolute_limits_have_distinct_errors(self):
        cases = [(b"ab", self.request, ErrorCode.SIZE_MISMATCH),
                 (b"abcd", self.request, ErrorCode.SIZE_MISMATCH),
                 (b"xyz", self.request, ErrorCode.HASH_MISMATCH),
                 (b"abc", replace(self.request, max_bytes=2), ErrorCode.PACKAGE_TOO_LARGE)]
        for index, (data, request, code) in enumerate(cases):
            with self.subTest(code=code, data=data):
                with self.assertRaises(SmartAppError) as raised:
                    await HttpsDownloader(opener=lambda request, timeout: Response(data)).download(
                        request, self.destination.with_name(str(index) + ".part"))
                self.assertEqual(raised.exception.code, code)

    async def test_deadline_is_total_across_reads(self):
        now = [0.0]
        class SlowResponse(Response):
            def read1(self, count):
                now[0] += 0.6
                return super().read(1)
        with self.assertRaises(SmartAppError) as raised:
            await HttpsDownloader(opener=lambda request, timeout: SlowResponse(),
                                 clock=lambda: now[0]).download(self.request, self.destination)
        self.assertEqual(raised.exception.code, ErrorCode.DOWNLOAD_FAILED)

    async def test_cancellation_waits_for_worker_before_return(self):
        loop_errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: loop_errors.append(context))
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        class BlockingResponse(Response):
            def read1(self, count):
                entered.set()
                release.wait(5)
                return super().read1(count)
            def close(self):
                finished.set()
                super().close()
        task = asyncio.create_task(HttpsDownloader(opener=lambda request, timeout: BlockingResponse()).download(
            self.request, self.destination))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertEqual(self.destination.read_bytes(), b"")
        self.assertEqual(loop_errors, [])

    async def test_incomplete_http_transfer_is_download_failed(self):
        class BrokenResponse(Response):
            def read1(self, count):
                raise http.client.IncompleteRead(b"a", 3)
        with self.assertRaises(SmartAppError) as raised:
            await HttpsDownloader(opener=lambda request, timeout: BrokenResponse()).download(self.request, self.destination)
        self.assertEqual(raised.exception.code, ErrorCode.DOWNLOAD_FAILED)

    async def test_real_http_parser_obeys_deadline_in_extensions_trailers_and_headers(self):
        headers = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        cases = [
            (headers + b"3;" + b"x" * 100 + b"\r\nabc\r\n0\r\n\r\n", len(headers)),
            (headers + b"3\r\nabc\r\n0\r\nX-Trailer: " + b"x" * 100 + b"\r\n\r\n", len(headers) + 11),
            (b"HTTP/1.1 200 OK\r\nX-Header: " + b"x" * 100 +
             b"\r\nContent-Length: 3\r\n\r\nabc", 0),
            (headers + b"3;ok=yes\r\nabc\r\n0\r\nX-Trailer: ok\r\n\r\n", 10000),
        ]
        for index, (wire, slow_at) in enumerate(cases):
            with self.subTest(parser_phase=index):
                now, late_reads = [0.0], []
                class ControlledSocket:
                    timeout = 0.2
                    def settimeout(self, value):
                        self.timeout = value
                    def sendall(self, data):
                        pass
                    def close(self):
                        pass
                    def makefile(self, mode, buffering=None):
                        sock = self
                        class SlowRaw(io.RawIOBase):
                            offset = 0
                            def readable(self):
                                return True
                            def readinto(self, output):
                                if self.offset >= len(wire):
                                    return 0
                                if self.offset >= slow_at:
                                    if now[0] >= 0.2:
                                        late_reads.append(self.offset)
                                    delay = min(0.05, sock.timeout)
                                    now[0] += delay
                                    if delay < 0.05:
                                        raise TimeoutError("controlled transport deadline")
                                output[0] = wire[self.offset]
                                self.offset += 1
                                return 1
                        raw = SlowRaw()
                        return raw if buffering == 0 else io.BufferedReader(raw)
                def controlled_connect(connection):
                    connection.sock = ControlledSocket()
                with patch.object(http.client.HTTPSConnection, "connect", controlled_connect):
                    downloader = HttpsDownloader(clock=lambda: now[0])
                    if index == 3:
                        result = await downloader.download(self.request, self.destination)
                        self.assertEqual(result, DownloadResult(3, self.request.expected_sha256))
                        self.assertEqual(self.destination.read_bytes(), b"abc")
                        continue
                    with self.assertRaises(SmartAppError) as raised:
                        await downloader.download(replace(self.request, timeout=0.2),
                                                  self.destination.with_name(str(index) + ".part"))
                self.assertEqual(raised.exception.code, ErrorCode.DOWNLOAD_FAILED)
                self.assertEqual(late_reads, [], "HTTP parser continued raw I/O past deadline")
                self.assertLessEqual(now[0], 0.200001)


class InstallerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = RuntimeConfig(paths=PathsConfig(root=self.root))
        self.data = package()
        self.command = StartApp("r", "s", "demo", "1", "https://example.invalid/p",
                                len(self.data), hashlib.sha256(self.data).hexdigest(), {})
        self.downloader = FakeDownloader(self.data)
        self.installer = PackageInstaller(self.config, self.downloader)

    def assert_clean(self):
        self.assertEqual(list((self.root / "downloads").glob("*.part")), [])
        self.assertEqual(list((self.root / "tmp").glob("*.staging")), [])

    async def test_install_is_atomic_and_second_identical_request_is_cache_hit(self):
        import os
        original = os.replace
        def checked_replace(source, destination):
            metadata = json.loads((Path(source) / ".smartapp-install.json").read_text())
            self.assertEqual(set(metadata), {"sha256", "packageSize", "installedAt"})
            self.assertEqual(metadata["sha256"], self.command.sha256)
            self.assertFalse(Path(destination).exists())
            return original(source, destination)
        with patch("smartapp_runtime.infrastructure.packages.installer.os.replace", side_effect=checked_replace):
            first = await self.installer.ensure_installed(self.command)
        second = await self.installer.ensure_installed(self.command)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.root, self.root.resolve() / "apps/demo/1")
        self.assertEqual((first.root / "web/index.html").read_bytes(), b"hello")
        self.assertEqual(self.downloader.calls, 1)
        self.assert_clean()

    async def test_md5_install_verifies_and_reuses_same_digest(self):
        command = replace(self.command, sha256="", md5=hashlib.md5(self.data).hexdigest())
        first = await self.installer.ensure_installed(command)
        second = await self.installer.ensure_installed(command)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.sha256, hashlib.sha256(self.data).hexdigest())
        metadata = json.loads((first.root / ".smartapp-install.json").read_text())
        self.assertEqual(metadata["md5"], command.md5)
        with self.assertRaises(SmartAppError) as raised:
            await self.installer.ensure_installed(replace(command, md5="0" * 32))
        self.assertEqual(raised.exception.code, ErrorCode.INSTALL_CONFLICT)

    async def test_progress_callback_reports_only_cold_install_irreversible_phases(self):
        observed = []

        async def progress(state):
            observed.append(state)

        await self.installer.ensure_installed(self.command, progress=progress)
        self.assertEqual(observed, [
            RuntimeState.DOWNLOADING,
            RuntimeState.VERIFYING,
            RuntimeState.INSTALLING,
        ])
        observed.clear()
        await self.installer.ensure_installed(self.command, progress=progress)
        self.assertEqual(observed, [])

    async def test_installing_progress_is_not_reported_until_download_is_verified(self):
        observed = []

        async def progress(state):
            observed.append(state)

        installer = PackageInstaller(self.config, FakeDownloader(b"x" * len(self.data)))
        with self.assertRaises(SmartAppError) as raised:
            await installer.ensure_installed(self.command, progress=progress)
        self.assertEqual(raised.exception.code, ErrorCode.HASH_MISMATCH)
        self.assertEqual(observed, [RuntimeState.DOWNLOADING, RuntimeState.VERIFYING])

    async def test_mismatched_sizes_hashes_and_dishonest_results_never_publish(self):
        cases = [(self.data[:-1], None, ErrorCode.SIZE_MISMATCH),
                 (b"x" * len(self.data), None, ErrorCode.HASH_MISMATCH),
                 (self.data, DownloadResult(1, self.command.sha256), ErrorCode.SIZE_MISMATCH),
                 (self.data, DownloadResult(len(self.data), "0" * 64), ErrorCode.HASH_MISMATCH)]
        for data, result, code in cases:
            with self.subTest(code=code, result=result):
                installer = PackageInstaller(self.config, FakeDownloader(data, result))
                with self.assertRaises(SmartAppError) as raised:
                    await installer.ensure_installed(self.command)
                self.assertEqual(raised.exception.code, code)
                self.assertFalse((self.root / "apps/demo/1").exists())
                self.assert_clean()

    async def test_manifest_mismatch_and_missing_entry_remove_staging(self):
        for data in (package(version="2"), package(entry=False)):
            command = replace(self.command, package_size=len(data), sha256=hashlib.sha256(data).hexdigest())
            with self.assertRaises(SmartAppError) as raised:
                await PackageInstaller(self.config, FakeDownloader(data)).ensure_installed(command)
            self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)
            self.assert_clean()

    async def test_conflicting_version_and_corrupt_cache_are_immutable(self):
        installed = await self.installer.ensure_installed(self.command)
        metadata = installed.root / ".smartapp-install.json"
        original = metadata.read_bytes()
        with self.assertRaises(SmartAppError) as raised:
            await self.installer.ensure_installed(replace(self.command, sha256="0" * 64))
        self.assertEqual(raised.exception.code, ErrorCode.INSTALL_CONFLICT)
        self.assertEqual(metadata.read_bytes(), original)
        metadata.write_text('{"sha256":"x"}')
        with self.assertRaises(SmartAppError) as raised:
            await self.installer.ensure_installed(self.command)
        self.assertEqual(raised.exception.code, ErrorCode.INSTALL_CONFLICT)
        self.assertEqual(self.downloader.calls, 1)

    async def test_cache_symlink_entry_is_conflict(self):
        installed = await self.installer.ensure_installed(self.command)
        entry = installed.root / "web/index.html"
        entry.unlink()
        entry.symlink_to(installed.root / "manifest.json")
        with self.assertRaises(SmartAppError) as raised:
            await self.installer.ensure_installed(self.command)
        self.assertEqual(raised.exception.code, ErrorCode.INSTALL_CONFLICT)

    async def test_oversized_declaration_never_downloads(self):
        with self.assertRaises(SmartAppError) as raised:
            await self.installer.ensure_installed(replace(self.command, package_size=536870913))
        self.assertEqual(raised.exception.code, ErrorCode.PACKAGE_TOO_LARGE)
        self.assertEqual(self.downloader.calls, 0)

    async def test_rename_failure_cleans_only_transaction_paths(self):
        for name in ("downloads", "tmp"):
            (self.root / name).mkdir()
            (self.root / name / "unrelated").write_text("keep")
        with patch("smartapp_runtime.infrastructure.packages.installer.os.replace", side_effect=OSError("disk")):
            with self.assertRaises(SmartAppError):
                await self.installer.ensure_installed(self.command)
        self.assert_clean()
        for name in ("downloads", "tmp"):
            self.assertEqual((self.root / name / "unrelated").read_text(), "keep")

    async def test_concurrent_installers_cannot_overwrite_version(self):
        other = PackageInstaller(self.config, FakeDownloader(self.data))
        first, second = await asyncio.gather(self.installer.ensure_installed(self.command),
                                             other.ensure_installed(self.command))
        self.assertEqual(first.root, second.root)
        self.assertEqual(sum([first.cache_hit, second.cache_hit]), 1)
        self.assert_clean()

    async def test_cancelled_download_cleans_transaction(self):
        started = asyncio.Event()
        class CancelDownloader:
            async def download(self, request, destination):
                destination.write_bytes(b"partial")
                started.set()
                await asyncio.Future()
        task = asyncio.create_task(PackageInstaller(self.config, CancelDownloader()).ensure_installed(self.command))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_clean()

    async def test_cancellation_during_extraction_waits_before_cleanup_and_never_publishes(self):
        from smartapp_runtime.infrastructure.packages.archive import SafeArchiveExtractor
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        real = SafeArchiveExtractor(self.config.limits)
        class BlockingExtractor:
            def extract(self, *args):
                entered.set()
                release.wait(5)
                result = real.extract(*args)
                finished.set()
                return result
        installer = PackageInstaller(self.config, self.downloader, BlockingExtractor())
        task = asyncio.create_task(installer.ensure_installed(self.command))
        while not entered.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        task.cancel()
        await asyncio.sleep(0.01)
        self.assertFalse(task.done())
        self.assertEqual(len(list((self.root / "tmp").glob("*.staging"))), 1)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        self.assertFalse((self.root / "apps/demo/1").exists())
        self.assert_clean()

    async def test_metadata_fsync_failure_never_publishes(self):
        with patch("smartapp_runtime.infrastructure.packages.installer._fsync_directory", side_effect=OSError("disk")):
            with self.assertRaises(SmartAppError):
                await self.installer.ensure_installed(self.command)
        self.assertFalse((self.root / "apps/demo/1").exists())
        self.assert_clean()

    async def test_cache_rejects_duplicate_metadata_and_boolean_size(self):
        installed = await self.installer.ensure_installed(self.command)
        path = installed.root / ".smartapp-install.json"
        original = path.read_text()
        invalid = original[:-1] + ', "sha256": "' + self.command.sha256 + '"}'
        for text in (invalid, original.replace(str(self.command.package_size), "true")):
            path.write_text(text)
            with self.assertRaises(SmartAppError) as raised:
                await self.installer.ensure_installed(self.command)
            self.assertEqual(raised.exception.code, ErrorCode.INSTALL_CONFLICT)

    async def test_cleanup_failure_does_not_hide_hash_mismatch(self):
        with patch("smartapp_runtime.infrastructure.packages.installer.Path.unlink", side_effect=OSError("cleanup")):
            with self.assertRaises(SmartAppError) as raised:
                await self.installer.ensure_installed(replace(self.command, sha256="0" * 64))
        self.assertEqual(raised.exception.code, ErrorCode.HASH_MISMATCH)

    async def test_random_name_collision_never_cleans_preexisting_paths(self):
        token = uuid.UUID("12345678-1234-1234-1234-123456789abc")
        staging = self.root / "tmp" / (token.hex + ".staging")
        staging.mkdir(parents=True)
        (staging / "sentinel").write_text("keep")
        with patch("smartapp_runtime.infrastructure.packages.installer.uuid.uuid4", return_value=token):
            with self.assertRaises(SmartAppError):
                await self.installer.ensure_installed(self.command)
        self.assertEqual((staging / "sentinel").read_text(), "keep")
        token = uuid.UUID("12345678-1234-1234-1234-123456789abd")
        part = self.root / "downloads" / (token.hex + ".part")
        part.write_text("keep")
        with patch("smartapp_runtime.infrastructure.packages.installer.uuid.uuid4", return_value=token):
            with self.assertRaises(SmartAppError):
                await self.installer.ensure_installed(self.command)
        self.assertEqual(part.read_text(), "keep")


if __name__ == "__main__":
    unittest.main()
