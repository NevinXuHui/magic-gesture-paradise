import io
import gzip
import json
import os
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.config import LimitConfig
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.infrastructure.packages.archive import SafeArchiveExtractor


def manifest_bytes(**changes):
    value = {"schemaVersion": 1, "appId": "demo", "version": "1",
             "web": {"enabled": True, "entry": "index.html"},
             "backend": {"enabled": False, "entry": "main.py", "dynamicService": False},
             "routing": {"defaultTarget": "web"}}
    value.update(changes)
    return json.dumps(value).encode()


def archive_bytes(extra=(), manifest=None, mode="w:gz"):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode=mode) as archive:
        for name, data, kind, permissions in [
            ("demo/manifest.json", manifest or manifest_bytes(), tarfile.REGTYPE, 0o644),
            ("demo/web/index.html", b"hello", tarfile.REGTYPE, 0o644),
        ] + list(extra):
            item = tarfile.TarInfo(name)
            item.type, item.mode = kind, permissions
            item.linkname = "../../escape"
            item.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(item, io.BytesIO(data))
    return output.getvalue()


class ArchiveTests(unittest.TestCase):
    def extract(self, payload, limits=None):
        archive = self.root / "input.tar.gz"
        archive.write_bytes(payload)
        return SafeArchiveExtractor(limits or LimitConfig()).extract(
            archive, self.staging, "demo", "1")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.staging = self.root / "stage"
        self.staging.mkdir()

    def assert_unsafe(self, payload, limits=None, code=ErrorCode.ARCHIVE_UNSAFE):
        with self.assertRaises(SmartAppError) as raised:
            self.extract(payload, limits)
        self.assertEqual(raised.exception.code, code)
        self.assertFalse((self.root / "escape").exists())

    def test_valid_archive_creates_regular_files_with_safe_modes(self):
        root, manifest = self.extract(archive_bytes())
        self.assertEqual(root, self.staging.resolve() / "demo")
        self.assertEqual(manifest.app_id, "demo")
        self.assertEqual((root / "web/index.html").read_bytes(), b"hello")
        self.assertEqual((root / "web").stat().st_mode & 0o7777, 0o755)
        self.assertEqual((root / "web/index.html").stat().st_mode & 0o7777, 0o644)

    def test_traversal_absolute_dot_empty_and_wrong_root_never_escape(self):
        for name in ("../escape", str(self.root / "escape"), "demo/../escape",
                     "demo/./escape", "demo//escape", "other/file", "demo/\x00evil"):
            with self.subTest(name=name):
                # Separate staging roots also prove no pre-existing files are reused.
                self.staging = self.root / ("stage" + str(len(list(self.root.iterdir()))))
                self.staging.mkdir()
                self.assert_unsafe(archive_bytes([(name, b"x", tarfile.REGTYPE, 0o644)]))

    def test_links_devices_fifo_and_setid_are_rejected(self):
        for kind, mode in ((tarfile.SYMTYPE, 0o644), (tarfile.LNKTYPE, 0o644),
                           (tarfile.FIFOTYPE, 0o644), (tarfile.CHRTYPE, 0o644),
                           (tarfile.BLKTYPE, 0o644), (b"s", 0o644),
                           (tarfile.REGTYPE, 0o4755), (tarfile.REGTYPE, 0o2755)):
            with self.subTest(kind=kind, mode=mode):
                self.staging = self.root / ("stage" + str(len(list(self.root.iterdir()))))
                self.staging.mkdir()
                self.assert_unsafe(archive_bytes([("demo/evil", b"x", kind, mode)]))

    def test_duplicate_and_file_directory_collision_are_rejected(self):
        self.assert_unsafe(archive_bytes([("demo/web/index.html", b"replace", tarfile.REGTYPE, 0o644)]))

    def test_member_count_includes_directories(self):
        self.assert_unsafe(archive_bytes([("demo/empty", b"", tarfile.DIRTYPE, 0o755)]),
                           replace(LimitConfig(), max_files=2))

    def test_single_file_total_and_utf8_path_limits(self):
        for limits, extra in ((replace(LimitConfig(), max_file_bytes=4), []),
                              (replace(LimitConfig(), max_unpacked_bytes=4), []),
                              (replace(LimitConfig(), max_path_length=20),
                               [("demo/" + "中" * 6, b"x", tarfile.REGTYPE, 0o644)])):
            with self.subTest(limits=limits):
                self.staging = self.root / ("stage" + str(len(list(self.root.iterdir()))))
                self.staging.mkdir()
                self.assert_unsafe(archive_bytes(extra), limits)

    def test_non_gzip_and_truncated_archive_are_rejected(self):
        self.assert_unsafe(archive_bytes(mode="w"))
        self.assert_unsafe(archive_bytes()[:60])

    def test_bad_gzip_footer_and_short_declared_file_are_rejected(self):
        payload = bytearray(archive_bytes())
        payload[-8] ^= 0xFF
        self.assert_unsafe(bytes(payload))
        self.staging = self.root / "second"
        self.staging.mkdir()
        member = tarfile.TarInfo("demo/manifest.json")
        member.size = 100
        self.assert_unsafe(gzip.compress(member.tobuf() + b"abc"))

    def test_sparse_archive_member_is_rejected(self):
        self.assert_unsafe(archive_bytes([("demo/sparse", b"", tarfile.GNUTYPE_SPARSE, 0o644)]))

    def test_oversized_extended_headers_cannot_bypass_resource_limits(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz", pax_headers={"comment": "x" * 70000}) as archive:
            for name, data in (("demo/manifest.json", manifest_bytes()), ("demo/web/index.html", b"ok")):
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        self.assert_unsafe(stream.getvalue())

    def test_invalid_deflate_is_mapped_to_archive_unsafe(self):
        payload = bytearray(archive_bytes())
        payload[10] = 7
        self.assert_unsafe(bytes(payload))

    def test_unicode_pax_path_and_explicit_directories_are_supported(self):
        root, _ = self.extract(archive_bytes([
            ("demo/assets/", b"", tarfile.DIRTYPE, 0o700),
            ("demo/assets/中文.txt", b"text", tarfile.REGTYPE, 0o777)]))
        self.assertEqual((root / "assets/中文.txt").read_bytes(), b"text")
        self.assertEqual((root / "assets").stat().st_mode & 0o7777, 0o755)

    def test_pax_sparse_10_is_rejected_before_reading_attacker_map(self):
        stream = io.BytesIO()
        sparse_map = b"20000\n" + b"0\n0\n" * 20000
        with tarfile.open(fileobj=stream, mode="w:gz") as archive:
            item = tarfile.TarInfo("demo/sparse")
            item.pax_headers = {"GNU.sparse.major": "1", "GNU.sparse.minor": "0",
                                "GNU.sparse.realsize": "1"}
            item.size = len(sparse_map)
            archive.addfile(item, io.BytesIO(sparse_map))
        reads = []
        original = gzip.GzipFile.read
        def measured(source, size=-1):
            data = original(source, size)
            reads.append(len(data))
            return data
        with patch.object(gzip.GzipFile, "read", measured):
            self.assert_unsafe(stream.getvalue(), replace(LimitConfig(), max_unpacked_bytes=1024))
        self.assertLessEqual(sum(reads), 2048, "sparse map was read before rejection")

    def test_consumed_bad_header_and_missing_double_zero_end_fail_closed(self):
        raw = gzip.decompress(archive_bytes())
        end = 0
        for unused in range(2):
            item = tarfile.TarInfo.frombuf(raw[end:end + 512], "utf-8", "strict")
            end += 512 + ((item.size + 511) // 512) * 512
        bad = bytearray(512)
        bad[:4] = b"evil"
        cases = [raw[:end] + bytes(bad) + bytes(1024),
                 raw[:end] + bytes(512), raw[:end],
                 raw[:end] + bytes(512) + bytes(bad)]
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                self.staging = self.root / ("termination" + str(index))
                self.staging.mkdir()
                self.assert_unsafe(gzip.compress(data))

    def test_pax_sparse_variants_and_cumulative_extension_budget_are_rejected(self):
        variants = ({"GNU.sparse.map": "0,0"}, {"GNU.sparse.size": "1"},
                    {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"})
        for index, headers in enumerate(variants):
            stream = io.BytesIO()
            with tarfile.open(fileobj=stream, mode="w:gz", pax_headers=headers) as archive:
                item = tarfile.TarInfo("demo/map")
                archive.addfile(item, io.BytesIO())
            self.staging = self.root / ("variant" + str(index))
            self.staging.mkdir()
            self.assert_unsafe(stream.getvalue())
        # Each individual header fits; the cumulative metadata exceeds the budget.
        blocks = []
        for unused in range(3):
            value = b"x" * 400
            record = b"413 comment=" + value + b"\n"
            item = tarfile.TarInfo("pax")
            item.type, item.size = tarfile.XGLTYPE, len(record)
            blocks.append(item.tobuf(format=tarfile.USTAR_FORMAT) + record + bytes(512 - len(record)))
        self.staging = self.root / "cumulative"
        self.staging.mkdir()
        self.assert_unsafe(gzip.compress(b"".join(blocks) + gzip.decompress(archive_bytes())),
                           replace(LimitConfig(), max_unpacked_bytes=1024))

    def test_manifest_mismatch_is_not_installable(self):
        self.assert_unsafe(archive_bytes(manifest=manifest_bytes(version="2")),
                           code=ErrorCode.MANIFEST_INVALID)

    def test_existing_staging_content_is_never_overwritten(self):
        sentinel = self.staging / "sentinel"
        sentinel.write_bytes(b"keep")
        self.assert_unsafe(archive_bytes())
        self.assertEqual(sentinel.read_bytes(), b"keep")

    def test_default_path_limit_blocks_241_byte_member(self):
        self.assert_unsafe(archive_bytes([("demo/" + "x" * 236, b"x", tarfile.REGTYPE, 0o644)]))


if __name__ == "__main__":
    unittest.main()
