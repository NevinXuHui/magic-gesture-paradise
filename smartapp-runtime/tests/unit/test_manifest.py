import json
import tempfile
import unittest
from pathlib import Path

from smartapp_runtime.domain.commands import MessageTarget
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.manifest import Manifest, load_manifest


def valid_manifest():
    return {
        "schemaVersion": 1,
        "appId": "demo_app",
        "version": "1.0.0",
        "web": {"enabled": True, "entry": "index.html"},
        "backend": {
            "enabled": False,
            "entry": "main.py",
            "dynamicService": False,
        },
        "routing": {"defaultTarget": "web"},
    }


class ManifestTests(unittest.TestCase):
    def test_manifest_accepts_each_valid_component_configuration(self):
        cases = (
            (True, False, "web"),
            (False, True, "python"),
            (True, True, "python"),
        )

        for web_enabled, backend_enabled, default_target in cases:
            with self.subTest(
                web_enabled=web_enabled,
                backend_enabled=backend_enabled,
                default_target=default_target,
            ):
                payload = valid_manifest()
                payload["web"]["enabled"] = web_enabled
                payload["backend"]["enabled"] = backend_enabled
                payload["routing"]["defaultTarget"] = default_target

                manifest = Manifest.from_dict(payload)

                self.assertEqual(manifest.default_target, MessageTarget(default_target))
                self.assertEqual(manifest.to_dict(), payload)

    def test_manifest_rejects_invalid_component_and_routing_combinations(self):
        cases = (
            (False, False, "web", "no enabled component"),
            (True, False, "python", "web-only"),
            (False, True, "web", "Python-only"),
            (True, True, "auto", "Hybrid"),
        )

        for web_enabled, backend_enabled, default_target, expected_message in cases:
            with self.subTest(
                web_enabled=web_enabled,
                backend_enabled=backend_enabled,
                default_target=default_target,
            ):
                payload = valid_manifest()
                payload["web"]["enabled"] = web_enabled
                payload["backend"]["enabled"] = backend_enabled
                payload["routing"]["defaultTarget"] = default_target

                with self.assertRaisesRegex(SmartAppError, expected_message) as raised:
                    Manifest.from_dict(payload)

                self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)

    def test_manifest_rejects_mismatched_expected_identifiers(self):
        with self.assertRaises(SmartAppError) as raised:
            Manifest.from_dict(valid_manifest(), expected_app_id="other_app")

        self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)
        with self.assertRaises(SmartAppError) as raised:
            Manifest.from_dict(valid_manifest(), expected_version="2.0.0")

        self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)

    def test_manifest_rejects_unknown_keys_at_every_level(self):
        payload = valid_manifest()
        payload["extra"] = True
        with self.assertRaisesRegex(SmartAppError, "unknown field") as raised:
            Manifest.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)
        payload = valid_manifest()
        payload["backend"]["extra"] = True
        with self.assertRaisesRegex(SmartAppError, "unknown field") as raised:
            Manifest.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)

    def test_manifest_rejects_invalid_entry_paths(self):
        for entry in ("", "/index.html", "index.html/", "assets//index.html", "../main.py"):
            with self.subTest(entry=entry):
                payload = valid_manifest()
                payload["web"]["entry"] = entry
                with self.assertRaises(SmartAppError) as raised:
                    Manifest.from_dict(payload)

                self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)

    def test_manifest_rejects_unsupported_schema(self):
        payload = valid_manifest()
        payload["schemaVersion"] = 2

        with self.assertRaises(SmartAppError) as raised:
            Manifest.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.UNSUPPORTED_SCHEMA)

    def test_load_manifest_reads_utf8_json_and_rejects_non_object(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            manifest_path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            manifest = load_manifest(manifest_path, "demo_app", "1.0.0")

            self.assertEqual(manifest.app_id, "demo_app")
            manifest_path.write_text("[]", encoding="utf-8")
            with self.assertRaises(SmartAppError) as raised:
                load_manifest(manifest_path)

        self.assertEqual(raised.exception.code, ErrorCode.MANIFEST_INVALID)


if __name__ == "__main__":
    unittest.main()
