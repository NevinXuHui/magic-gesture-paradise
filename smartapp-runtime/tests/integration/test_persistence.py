import json
import os
import stat
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session
from smartapp_runtime.domain.state import RuntimeState
from smartapp_runtime.infrastructure.persistence.paths import RuntimePaths
from smartapp_runtime.infrastructure.persistence.pointers import AtomicPointers
from smartapp_runtime.infrastructure.persistence.state_repository import FileStateRepository
from smartapp_runtime.infrastructure.persistence.lock import SingleInstanceLock
from smartapp_runtime.ports.repository import BackendProcessIdentity, PersistedRuntimeState


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = RuntimePaths.from_root(Path(self.temp.name) / "runtime")
        self.paths.ensure_layout()
        self.v1 = self.paths.apps_root / "demo" / "v1"
        self.v2 = self.paths.apps_root / "demo" / "v2"
        (self.v1 / "web").mkdir(parents=True)
        (self.v2 / "web").mkdir(parents=True)
        self.pointers = AtomicPointers(self.paths)
        self.repo = FileStateRepository(self.paths.state_file)

    def assertRecovery(self, action):
        with self.assertRaises(SmartAppError) as caught:
            action()
        self.assertEqual(caught.exception.code, ErrorCode.RECOVERY_FAILED)

    def test_layout_modes_and_rejects_symlink_directory(self):
        for name in ("root", "apps_root", "downloads_root", "tmp_root", "state_root", "logs_root"):
            self.assertEqual(stat.S_IMODE(getattr(self.paths, name).stat().st_mode), 0o750)
        self.paths.logs_root.rmdir()
        self.paths.logs_root.symlink_to(self.v1, target_is_directory=True)
        self.assertRecovery(self.paths.ensure_layout)

    def test_pointer_replacement_is_relative_atomic_and_restores_raw_snapshot(self):
        current = self.v1.parent / "current"
        current.symlink_to("./v1")
        snapshot = self.pointers.snapshot("demo")
        with patch("os.replace", wraps=os.replace) as replacing:
            self.pointers.set_current("demo", self.v2)
            self.pointers.set_previous("demo", self.v1)
            self.pointers.set_current_web(self.v2 / "web")
        self.assertEqual(replacing.call_count, 3)
        for call in replacing.call_args_list:
            self.assertEqual(Path(call.args[0]).parent, Path(call.args[1]).parent)
        self.assertEqual(os.readlink(current), "v2")
        self.assertEqual(self.pointers.current_target("demo"), self.v2)
        self.assertEqual(self.pointers.previous_target("demo"), self.v1)
        self.assertEqual(self.pointers.current_web_target(), self.v2 / "web")
        self.pointers.restore(snapshot)
        self.assertEqual(os.readlink(current), "./v1")
        self.assertFalse(os.path.lexists(self.v1.parent / "previous"))
        self.assertFalse(os.path.lexists(self.paths.current_web))

    def test_pointer_rejects_escape_wrong_depth_app_and_unexpected_objects(self):
        foreign = self.paths.apps_root / "other" / "v1"
        foreign.mkdir(parents=True)
        for target in (Path(self.temp.name), self.paths.apps_root, self.v1.parent, self.v1 / "web", foreign):
            self.assertRecovery(lambda target=target: self.pointers.set_current("demo", target))
        self.assertRecovery(lambda: self.pointers.set_current_web(self.v1))
        current = self.v1.parent / "current"
        current.write_text("keep")
        self.assertRecovery(lambda: self.pointers.set_current("demo", self.v1))
        self.assertEqual(current.read_text(), "keep")

    def test_pointer_failure_cleans_owned_temporary_link(self):
        self.pointers.set_current("demo", self.v1)
        with patch("os.replace", side_effect=OSError("failure")):
            self.assertRecovery(lambda: self.pointers.set_current("demo", self.v2))
        self.assertEqual(self.pointers.current_target("demo"), self.v1)
        self.assertEqual(sorted(p.name for p in self.v1.parent.iterdir()), ["current", "v1", "v2"])

    def test_validation_removes_only_exact_invalid_names_without_following(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "keep").write_text("safe")
        (self.v1.parent / "current").symlink_to(outside)
        (self.v1.parent / "previous").mkdir()
        (self.v1.parent / "previous" / "junk").write_text("owned")
        self.paths.current_web.symlink_to(self.v1 / "web")
        (self.paths.apps_root / "linked_app").symlink_to(outside)
        (self.v1.parent / "unrelated").write_text("safe")
        removed = self.pointers.validate_and_remove_invalid()
        self.assertEqual(set(removed), {self.v1.parent / "current", self.v1.parent / "previous"})
        self.assertTrue((outside / "keep").exists())
        self.assertTrue((self.v1.parent / "unrelated").exists())
        self.assertEqual(self.pointers.current_web_target(), self.v1 / "web")

    def test_state_roundtrip_atomic_modes_and_identity_updates_preserve_fields(self):
        state = PersistedRuntimeState(state=RuntimeState.RUNNING,
            active_session=Session("s1", "demo", "v1", 7), generation=7,
            pointer_snapshot=self.pointers.snapshot("demo"),
            last_error={"code": "START_TIMEOUT", "message": "timeout"})
        with patch("os.fsync", wraps=os.fsync) as syncing:
            for number in range(3):
                self.repo.save(replace(state, generation=number))
                self.assertEqual(self.repo.load().generation, number)
        self.assertGreaterEqual(syncing.call_count, 6)
        self.assertEqual(stat.S_IMODE(self.paths.state_file.stat().st_mode), 0o640)
        identity = BackendProcessIdentity(123, "9876", str(self.v1))
        self.repo.set_backend_process(identity)
        self.assertEqual(self.repo.load().backend_process, identity)
        self.assertEqual(self.repo.load().active_session, state.active_session)
        self.repo.clear_backend_process()
        self.assertIsNone(self.repo.load().backend_process)
        self.assertEqual(self.repo.load().pointer_snapshot, state.pointer_snapshot)

    def test_non_linux_identity_sentinel_roundtrips_but_other_words_are_rejected(self):
        identity = BackendProcessIdentity(123, "non-linux", str(self.v1 / "backend" / "main.py"))
        self.repo.set_backend_process(identity)
        self.assertEqual(self.repo.load().backend_process, identity)
        for start_time in ("", "unknown", "non-linux-other", "-1", "1.2"):
            with self.assertRaises(SmartAppError):
                self.repo.set_backend_process(replace(identity, start_time=start_time))
        self.repo.clear_backend_process()
        self.assertIsNone(self.repo.load().backend_process)

    def test_missing_state_only_returns_default_and_corruption_is_rejected(self):
        self.assertEqual(self.repo.load(), PersistedRuntimeState())
        self.repo.save(PersistedRuntimeState())
        good = json.loads(self.paths.state_file.read_text())
        invalid = [b"", b"\xff", b"{", b'{"schemaVersion":1,"schemaVersion":1}']
        for key, value in (("schemaVersion", True), ("schemaVersion", 2), ("generation", True),
                           ("generation", -1), ("state", "invalid"), ("activeSession", {}),
                           ("backendProcess", {"pid": True, "startTime": "1", "commandMarker": str(self.v1)}),
                           ("backendProcess", {"pid": 1, "startTime": "1", "commandMarker": str(self.v1)}),
                           ("pointerSnapshot", {}), ("lastError", []), ("lastError", {"code": "invalid", "message": "x"})):
            invalid.append(json.dumps(dict(good, **{key: value})).encode())
        invalid.extend([json.dumps(dict(good, extra=1)).encode(),
                        json.dumps({k: v for k, v in good.items() if k != "generation"}).encode()])
        for raw in invalid:
            with self.subTest(raw=raw):
                self.paths.state_file.write_bytes(raw)
                self.assertRecovery(self.repo.load)

    def test_failed_state_replace_preserves_previous_json_and_cleans_temp(self):
        self.repo.save(PersistedRuntimeState(generation=3))
        with patch("os.replace", side_effect=OSError("failure")):
            self.assertRecovery(lambda: self.repo.save(PersistedRuntimeState(generation=4)))
        self.assertEqual(self.repo.load().generation, 3)
        self.assertEqual(list(self.paths.state_root.iterdir()), [self.paths.state_file])

    def test_lock_contention_release_context_and_never_unlinks(self):
        first = SingleInstanceLock(self.paths.lock_file)
        second = SingleInstanceLock(self.paths.lock_file)
        with first:
            self.assertRecovery(second.acquire)
            self.assertEqual(stat.S_IMODE(self.paths.lock_file.stat().st_mode), 0o600)
        first.release()
        with second:
            self.assertTrue(self.paths.lock_file.exists())
        self.assertTrue(self.paths.lock_file.exists())

    def test_lock_acquire_creates_only_missing_root_and_state_parent(self):
        root = Path(self.temp.name) / "early-runtime"
        paths = RuntimePaths.from_root(root)
        lock = SingleInstanceLock(paths.lock_file)

        lock.acquire()
        self.addCleanup(lock.release)

        self.assertTrue(paths.root.is_dir())
        self.assertTrue(paths.state_root.is_dir())
        self.assertTrue(paths.lock_file.is_file())
        self.assertFalse(paths.apps_root.exists())
        self.assertFalse(paths.logs_root.exists())

    def test_lock_acquire_creates_missing_runtime_parent(self):
        root = Path(self.temp.name) / "runtime-data" / "smartapp-runtime"
        paths = RuntimePaths.from_root(root)
        lock = SingleInstanceLock(paths.lock_file)

        lock.acquire()
        self.addCleanup(lock.release)

        self.assertTrue(root.parent.is_dir())
        self.assertTrue(paths.state_root.is_dir())
        self.assertTrue(paths.lock_file.is_file())

    def test_early_lock_rejects_symlink_root_or_state_parent(self):
        outside = Path(self.temp.name) / "early-outside"
        outside.mkdir()
        linked_root = Path(self.temp.name) / "linked-early-root"
        linked_root.symlink_to(outside, target_is_directory=True)
        self.assertRecovery(
            SingleInstanceLock(RuntimePaths.from_root(linked_root).lock_file).acquire
        )

        root = Path(self.temp.name) / "real-early-root"
        root.mkdir()
        (root / "state").symlink_to(outside, target_is_directory=True)
        self.assertRecovery(
            SingleInstanceLock(RuntimePaths.from_root(root).lock_file).acquire
        )

    def test_lock_open_uses_trusted_state_fd_when_path_is_replaced(self):
        original_state = self.paths.root / "state-original"
        outside = Path(self.temp.name) / "lock-outside"
        outside.mkdir()
        real_open = os.open
        swapped = []

        def racing_open(path, flags, *args, **kwargs):
            if (
                path == "runtime.lock"
                and kwargs.get("dir_fd") is not None
                and not swapped
            ):
                self.paths.state_root.rename(original_state)
                self.paths.state_root.symlink_to(outside, target_is_directory=True)
                swapped.append(True)
            return real_open(path, flags, *args, **kwargs)

        lock = SingleInstanceLock(self.paths.lock_file)
        with patch("smartapp_runtime.infrastructure.persistence.lock.os.open", side_effect=racing_open):
            lock.acquire()
        lock.release()

        self.assertTrue(swapped)
        self.assertFalse((outside / "runtime.lock").exists())
        self.assertTrue((original_state / "runtime.lock").is_file())

    def test_nested_state_shapes_and_invalid_save_preserve_last_good_state(self):
        state = PersistedRuntimeState(generation=8)
        self.repo.save(state)
        good = json.loads(self.paths.state_file.read_text())
        cases = [
            ("activeSession", {"sessionId": "s", "appId": "demo", "version": "v1", "generation": True}),
            ("activeSession", {"sessionId": "../s", "appId": "demo", "version": "v1", "generation": 1}),
            ("activeSession", {"sessionId": "s", "appId": "../demo", "version": "v1", "generation": 1}),
            ("activeSession", {"sessionId": "s", "appId": "demo", "version": "../v1", "generation": 1}),
            ("activeSession", {"sessionId": "s", "appId": "demo", "version": "v1", "generation": 1, "extra": 1}),
            ("backendProcess", {"pid": 123, "startTime": 123, "commandMarker": str(self.v1)}),
            ("backendProcess", {"pid": 123, "startTime": "-1", "commandMarker": str(self.v1)}),
            ("backendProcess", {"pid": 123, "startTime": "1", "commandMarker": "relative"}),
            ("backendProcess", {"pid": 123, "startTime": "1", "commandMarker": "/tmp/../bad"}),
            ("pointerSnapshot", {"appId": "demo", "current": False, "previous": None, "currentWeb": None}),
            ("pointerSnapshot", {"appId": "demo", "current": "", "previous": None, "currentWeb": None}),
            ("lastError", {"code": "RECOVERY_FAILED", "message": "bad", "extra": 1}),
            ("lastError", {"code": "RECOVERY_FAILED", "message": "bad", "details": []}),
            ("lastError", {"code": "RECOVERY_FAILED", "message": "bad", "details": {"n": float("nan")}}),
        ]
        for key, value in cases:
            with self.subTest(key=key, value=value):
                self.paths.state_file.write_text(json.dumps(dict(good, **{key: value})))
                self.assertRecovery(self.repo.load)
        self.paths.state_file.write_text(json.dumps(good).replace('"lastError": null',
            '"lastError": {"code":"RECOVERY_FAILED","message":"a","message":"b"}'))
        self.assertRecovery(self.repo.load)
        self.repo.save(state)
        for invalid in (replace(state, generation=True), replace(state, state="IDLE"),
                        replace(state, backend_process=BackendProcessIdentity(0, "1", str(self.v1)))):
            self.assertRecovery(lambda invalid=invalid: self.repo.save(invalid))
            self.assertEqual(self.repo.load(), state)

    def test_state_and_lock_refuse_symlink_files_and_symlink_parent(self):
        outside = Path(self.temp.name) / "external"
        outside.write_text("unchanged")
        self.paths.state_file.symlink_to(outside)
        self.paths.lock_file.symlink_to(outside)
        self.assertRecovery(self.repo.load)
        self.assertRecovery(lambda: self.repo.save(PersistedRuntimeState()))
        self.assertRecovery(SingleInstanceLock(self.paths.lock_file).acquire)
        self.assertEqual(outside.read_text(), "unchanged")
        linked = self.paths.root / "linked"
        linked.symlink_to(self.paths.state_root)
        self.assertRecovery(lambda: FileStateRepository(linked / "state.json").save(PersistedRuntimeState()))

    def test_state_fsync_failure_keeps_valid_destination_and_removes_temp(self):
        self.repo.save(PersistedRuntimeState(generation=3))
        with patch("os.fsync", side_effect=OSError("failure")):
            self.assertRecovery(lambda: self.repo.save(PersistedRuntimeState(generation=4)))
        self.assertEqual(self.repo.load().generation, 3)
        self.assertEqual(list(self.paths.state_root.iterdir()), [self.paths.state_file])

    def test_pointer_rejects_invalid_version_directory_name(self):
        invalid = self.v1.parent / "bad version"
        (invalid / "web").mkdir(parents=True)
        self.assertRecovery(lambda: self.pointers.set_current("demo", invalid))
        self.assertRecovery(lambda: self.pointers.set_current_web(invalid / "web"))

    def test_restore_prevalidates_all_targets_before_replacing_any_link(self):
        self.pointers.set_current("demo", self.v1)
        snapshot = replace(self.pointers.snapshot("demo"), current="v2", current_web="/missing")
        self.assertRecovery(lambda: self.pointers.restore(snapshot))
        self.assertEqual(self.pointers.current_target("demo"), self.v1)

    def test_layout_rejects_root_symlink_without_chmod_target(self):
        target = Path(self.temp.name) / "external_root"
        target.mkdir(mode=0o700)
        linked = Path(self.temp.name) / "linked_root"
        linked.symlink_to(target)
        self.assertRecovery(RuntimePaths.from_root(linked).ensure_layout)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)

    def test_pointer_failure_does_not_remove_replaced_temp_even_with_same_text(self):
        foreign_temporary = []

        def replace_temp(source, destination):
            source = Path(source)
            raw = os.readlink(source)
            moved = source.with_name("retained-original")
            os.rename(source, moved)
            source.symlink_to(raw)
            foreign_temporary.append(source)
            raise OSError("replace failed")

        with patch("os.replace", side_effect=replace_temp):
            self.assertRecovery(lambda: self.pointers.set_current("demo", self.v1))
        self.assertTrue(foreign_temporary[0].is_symlink())

    def test_temp_collision_preserves_preexisting_artifacts(self):
        class FixedUuid:
            hex = "existing"

        pointer_temp = self.v1.parent / ".current.existing"
        pointer_temp.symlink_to("v2")
        state_temp = self.paths.state_root / ".runtime-state.json.existing"
        state_temp.write_text("keep")
        with patch("uuid.uuid4", return_value=FixedUuid()):
            self.assertRecovery(lambda: self.pointers.set_current("demo", self.v1))
            self.assertRecovery(lambda: self.repo.save(PersistedRuntimeState()))
        self.assertEqual(os.readlink(pointer_temp), "v2")
        self.assertEqual(state_temp.read_text(), "keep")

    def test_failed_temp_cleanup_is_reported_as_recovery_error(self):
        with patch("os.replace", side_effect=OSError("secret replace failure")):
            with patch.object(Path, "unlink", side_effect=PermissionError("secret cleanup failure")):
                for action in (lambda: self.pointers.set_current("demo", self.v1),
                               lambda: self.repo.save(PersistedRuntimeState())):
                    with self.subTest(action=action):
                        self.assertRecovery(action)


if __name__ == "__main__":
    unittest.main()
