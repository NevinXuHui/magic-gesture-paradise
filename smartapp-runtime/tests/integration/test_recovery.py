import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.infrastructure.persistence.pointers import AtomicPointers
from smartapp_runtime.infrastructure.persistence.state_repository import FileStateRepository
from smartapp_runtime.infrastructure.persistence.recovery import RecoveryService, ObservedProcessIdentity, LinuxProcessIdentityReader
from smartapp_runtime.ports.repository import BackendProcessIdentity, PersistedRuntimeState


class FakeRenderer:
    def __init__(self, failure=False):
        self.restored = False
        self.failure = failure

    async def restore_default(self):
        self.restored = True
        if self.failure:
            raise RuntimeError("secret renderer contents")


class FakeIdentityReader:
    def __init__(self, observations):
        self.observations = list(observations)
        self.pids = []

    def read(self, pid):
        self.pids.append(pid)
        return self.observations.pop(0)


class FakeSignalSender:
    def __init__(self, gone=False):
        self.calls = []
        self.gone = gone

    def send_group(self, pgid, signal_number):
        self.calls.append((pgid, signal_number))
        if self.gone:
            raise ProcessLookupError()


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths.from_root(Path(self.temp.name) / "runtime")
        self.paths.ensure_layout()
        self.version = self.paths.apps_root / "demo" / "v1"
        (self.version / "web").mkdir(parents=True)
        (self.version / "backend").mkdir()
        self.entry = self.version / "backend" / "main.py"
        self.entry.write_text("pass")
        self.repo = FileStateRepository(self.paths.state_file)
        self.pointers = AtomicPointers(self.paths)
        self.renderer = FakeRenderer()
        self.sender = FakeSignalSender()
        self.identity = BackendProcessIdentity(12345, "123456", str(self.entry))
        self.observed = ObservedProcessIdentity("123456", ("python", str(self.entry)))

    def service(self, observations=(), platform="linux"):
        self.reader = FakeIdentityReader(observations)
        return RecoveryService(self.paths, self.repo, self.pointers, self.renderer,
            self.reader, self.sender, stale_after_seconds=300, term_grace_seconds=0,
            platform_name=platform, wall_clock=lambda: 1000)

    async def test_stale_cleanup_preserves_fresh_unrelated_and_symlink_targets(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep").write_text("safe")
        stale = self.paths.downloads_root / "old.part"
        stale.symlink_to(outside)
        os.utime(stale, (1, 1), follow_symlinks=False)
        staging = self.paths.tmp_root / "old.staging"
        staging.mkdir()
        (staging / "link").symlink_to(outside)
        os.utime(staging, (1, 1))
        fresh = self.paths.downloads_root / "fresh.part"
        fresh.write_text("active")
        os.utime(fresh, (999, 999))
        unrelated = self.paths.tmp_root / "keep"
        unrelated.write_text("safe")
        self.pointers.set_current("demo", self.version)
        self.paths.current_web.symlink_to(outside)
        await self.service().recover()
        self.assertFalse(os.path.lexists(stale))
        self.assertFalse(staging.exists())
        self.assertTrue((outside / "keep").exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(self.pointers.current_target("demo"), self.version)
        self.assertFalse(os.path.lexists(self.paths.current_web))
        self.assertTrue(self.renderer.restored)
        self.assertEqual(self.repo.load(), PersistedRuntimeState(state=RuntimeState.IDLE))

    async def test_starting_recovery_restores_persisted_pointer_snapshot(self):
        replacement = self.paths.apps_root / "demo" / "v2"
        (replacement / "web").mkdir(parents=True)
        self.pointers.set_current("demo", self.version)
        self.pointers.set_current_web(self.version / "web")
        snapshot = self.pointers.snapshot("demo")
        self.pointers.set_current_web(replacement / "web")
        self.repo.save(PersistedRuntimeState(
            state=RuntimeState.STARTING, pointer_snapshot=snapshot, generation=3,
        ))

        await self.service().recover()

        self.assertEqual(self.pointers.current_target("demo"), self.version)
        self.assertEqual(self.pointers.current_web_target(), self.version / "web")
        self.assertEqual(self.repo.load(), PersistedRuntimeState(state=RuntimeState.IDLE))

    async def test_running_recovery_clears_web_without_rolling_back_activation(self):
        replacement = self.paths.apps_root / "demo" / "v2"
        (replacement / "web").mkdir(parents=True)
        self.pointers.set_current("demo", self.version)
        self.pointers.set_current_web(self.version / "web")
        stale_snapshot = self.pointers.snapshot("demo")
        self.pointers.set_current("demo", replacement)
        self.pointers.set_previous("demo", self.version)
        self.pointers.set_current_web(replacement / "web")
        self.repo.save(PersistedRuntimeState(
            state=RuntimeState.RUNNING, generation=4,
            pointer_snapshot=stale_snapshot,
        ))

        await self.service().recover()

        self.assertEqual(self.pointers.current_target("demo"), replacement)
        self.assertEqual(self.pointers.previous_target("demo"), self.version)
        self.assertIsNone(self.pointers.current_web_target())

    async def test_pointer_rollback_failure_preserves_recovery_evidence(self):
        self.pointers.set_current("demo", self.version)
        self.pointers.set_current_web(self.version / "web")
        snapshot = self.pointers.snapshot("demo")
        persisted = PersistedRuntimeState(
            state=RuntimeState.STARTING, pointer_snapshot=snapshot, generation=5,
        )
        self.repo.save(persisted)

        with patch.object(self.pointers, "restore", side_effect=OSError("secret")):
            with self.assertRaises(SmartAppError):
                await self.service().recover()

        self.assertEqual(self.repo.load(), persisted)

    async def test_matching_identity_signals_term_then_kill(self):
        self.repo.save(PersistedRuntimeState(backend_process=self.identity))
        await self.service([self.observed, self.observed]).recover()
        self.assertEqual(self.reader.pids, [12345, 12345])
        self.assertEqual(self.sender.calls, [(12345, signal.SIGTERM), (12345, signal.SIGKILL)])

    async def test_mismatch_start_time_or_partial_marker_never_signals(self):
        for observation in (None, ObservedProcessIdentity("other", self.observed.cmdline),
                            ObservedProcessIdentity("123456", (str(self.entry) + "-other",))):
            self.repo.save(PersistedRuntimeState(backend_process=self.identity))
            await self.service([observation]).recover()
        self.assertEqual(self.sender.calls, [])

    async def test_reused_pid_after_term_is_not_killed(self):
        self.repo.save(PersistedRuntimeState(backend_process=self.identity))
        await self.service([self.observed, ObservedProcessIdentity("new", self.observed.cmdline)]).recover()
        self.assertEqual(self.sender.calls, [(12345, signal.SIGTERM)])

    async def test_non_linux_skips_reader_and_signals(self):
        self.repo.save(PersistedRuntimeState(backend_process=self.identity))
        with self.assertLogs("smartapp_runtime.infrastructure.persistence.recovery", level="WARNING"):
            await self.service(platform="darwin").recover()
        self.assertEqual(self.reader.pids, [])
        self.assertEqual(self.sender.calls, [])

    async def test_already_gone_process_is_success(self):
        self.repo.save(PersistedRuntimeState(backend_process=self.identity))
        self.sender = FakeSignalSender(gone=True)
        await self.service([self.observed]).recover()
        self.assertEqual(self.repo.load().state, RuntimeState.IDLE)

    async def test_load_and_renderer_failures_still_save_idle_and_hide_contents(self):
        self.paths.state_file.write_text("secret broken JSON")
        self.renderer.failure = True
        with self.assertRaises(SmartAppError) as caught:
            await self.service().recover()
        self.assertEqual(caught.exception.code, ErrorCode.RECOVERY_FAILED)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(self.renderer.restored)
        self.assertEqual(self.repo.load(), PersistedRuntimeState(state=RuntimeState.IDLE))

    async def test_failed_artifact_cleanup_continues_to_other_root_and_idle(self):
        blocked = self.paths.downloads_root / "blocked.part"
        blocked.write_text("old")
        removed = self.paths.tmp_root / "other.staging"
        removed.write_text("old")
        for item in (blocked, removed):
            os.utime(item, (1, 1))
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError("secret")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            with self.assertRaises(SmartAppError) as caught:
                await self.service().recover()
        self.assertTrue(blocked.exists())
        self.assertFalse(removed.exists())
        self.assertTrue(self.renderer.restored)
        self.assertEqual(self.repo.load().state, RuntimeState.IDLE)
        self.assertNotIn("secret", str(caught.exception))

    async def test_symlink_cleanup_root_never_touches_external_artifacts(self):
        external = Path(self.temp.name) / "external"
        external.mkdir()
        keep = external / "keep.part"
        keep.write_text("safe")
        os.utime(keep, (1, 1))
        self.paths.downloads_root.rmdir()
        self.paths.downloads_root.symlink_to(external)
        with self.assertRaises(SmartAppError):
            await self.service().recover()
        self.assertEqual(keep.read_text(), "safe")
        self.assertTrue(self.renderer.restored)
        self.assertEqual(self.repo.load().state, RuntimeState.IDLE)

    async def test_pointer_and_signal_failures_still_restore_and_save_idle(self):
        self.repo.save(PersistedRuntimeState(backend_process=self.identity))
        with patch.object(self.pointers, "validate_and_remove_invalid", side_effect=PermissionError("secret")):
            with patch.object(self.sender, "send_group", side_effect=PermissionError("secret")):
                with self.assertRaises(SmartAppError) as caught:
                    await self.service([self.observed]).recover()
        self.assertTrue(self.renderer.restored)
        self.assertEqual(self.repo.load().state, RuntimeState.IDLE)
        self.assertNotIn("secret", str(caught.exception))

    async def test_save_failure_is_reported_after_renderer_restore(self):
        with patch.object(self.repo, "save", side_effect=OSError("secret")):
            with self.assertRaises(SmartAppError) as caught:
                await self.service().recover()
        self.assertTrue(self.renderer.restored)
        self.assertEqual(caught.exception.code, ErrorCode.RECOVERY_FAILED)
        self.assertNotIn("secret", str(caught.exception))

    async def test_external_process_marker_never_signals(self):
        outside = Path(self.temp.name) / "other"
        outside.mkdir()
        self.repo.save(PersistedRuntimeState(backend_process=BackendProcessIdentity(12345, "123456", str(outside))))
        with self.assertRaises(SmartAppError):
            await self.service([ObservedProcessIdentity("123456", (str(outside),))]).recover()
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.repo.load().state, RuntimeState.IDLE)

    def test_proc_reader_handles_parenthesized_comm_and_rejects_nonleader(self):
        proc = Path(self.temp.name) / "proc"
        process = proc / "12345"
        process.mkdir(parents=True)
        fields = ["S", "1", "12345"] + ["0"] * 16 + ["123456"] + ["0"] * 3
        (process / "stat").write_text("12345 (python ) worker) " + " ".join(fields))
        (process / "cmdline").write_bytes(b"python\0" + os.fsencode(self.entry) + b"\0")
        reader = LinuxProcessIdentityReader(proc)
        self.assertEqual(reader.read(12345), self.observed)
        fields[2] = "56789"
        (process / "stat").write_text("12345 (python) " + " ".join(fields))
        self.assertIsNone(reader.read(12345))
        self.assertIsNone(reader.read(99999))

    async def test_directory_web_file_and_symlink_markers_never_signal(self):
        web = self.version / "web" / "main.py"
        web.write_text("pass")
        symlink = self.version / "backend" / "link.py"
        symlink.symlink_to(self.entry)
        for marker in (self.version, self.version / "backend", web, symlink):
            identity = BackendProcessIdentity(12345, "123456", str(marker))
            self.repo.save(PersistedRuntimeState(backend_process=identity))
            with self.assertRaises(SmartAppError):
                await self.service([ObservedProcessIdentity("123456", (str(marker),))]).recover()
        self.assertEqual(self.sender.calls, [])

    def test_negative_or_nonfinite_recovery_intervals_are_rejected(self):
        for stale, grace in ((-1, 0), (float("nan"), 0), (300, -1), (300, float("inf"))):
            with self.subTest(stale=stale, grace=grace):
                with self.assertRaises(SmartAppError):
                    RecoveryService(self.paths, self.repo, self.pointers, self.renderer,
                        FakeIdentityReader([]), self.sender, stale_after_seconds=stale,
                        term_grace_seconds=grace)


if __name__ == "__main__":
    unittest.main()
