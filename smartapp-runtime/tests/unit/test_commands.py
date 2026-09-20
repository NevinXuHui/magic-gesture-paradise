import unittest

from smartapp_runtime.domain.commands import (
    CloudData,
    GetStatus,
    MessageTarget,
    StartApp,
    StopApp,
    StopReason,
)
from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.models import Session


def valid_start_payload():
    return {
        "requestId": "request-1",
        "command": "start_app",
        "sessionId": "session:1",
        "appId": "demo_app",
        "version": "1.0.0",
        "packageUrl": "https://example.test/demo.tar.gz",
        "packageSize": 42,
        "sha256": "A" * 64,
        "initData": {"mode": "normal", "items": [1, True, None]},
    }


class CommandParsingTests(unittest.TestCase):
    def test_start_parses_and_serializes_canonical_payload(self):
        start = StartApp.from_dict(valid_start_payload())

        self.assertEqual(start.app_id, "demo_app")
        self.assertEqual(start.sha256, "a" * 64)
        self.assertEqual(
            start.to_dict(),
            {
                "requestId": "request-1",
                "command": "start_app",
                "sessionId": "session:1",
                "appId": "demo_app",
                "version": "1.0.0",
                "packageUrl": "https://example.test/demo.tar.gz",
                "packageSize": 42,
                "sha256": "a" * 64,
                "initData": {"mode": "normal", "items": [1, True, None]},
            },
        )

    def test_start_rejects_invalid_app_id(self):
        payload = valid_start_payload()
        payload["appId"] = "Demo-App"

        with self.assertRaises(SmartAppError) as raised:
            StartApp.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_start_rejects_invalid_sha_length(self):
        payload = valid_start_payload()
        payload["sha256"] = "a" * 63

        with self.assertRaises(SmartAppError) as raised:
            StartApp.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_start_rejects_boolean_package_size(self):
        payload = valid_start_payload()
        payload["packageSize"] = True

        with self.assertRaises(SmartAppError) as raised:
            StartApp.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_start_rejects_string_subclass_command_discriminator(self):
        class StringSubclass(str):
            pass

        payload = valid_start_payload()
        payload["command"] = StringSubclass("start_app")

        with self.assertRaises(SmartAppError) as raised:
            StartApp.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_start_rejects_unknown_md5_field(self):
        payload = valid_start_payload()
        payload["md5"] = "abc"
        with self.assertRaisesRegex(SmartAppError, "unknown field"):
            StartApp.from_dict(payload)

    def test_start_copies_init_data_at_input_and_output_boundaries(self):
        payload = valid_start_payload()
        start = StartApp.from_dict(payload)
        payload["initData"]["items"].append("later")

        serialized = start.to_dict()
        serialized["initData"]["items"].append("external")

        self.assertEqual(start.to_dict()["initData"]["items"], [1, True, None])

    def test_stop_parses_reason_enum(self):
        stop = StopApp.from_dict(
            {
                "requestId": "request-2",
                "command": "stop_app",
                "sessionId": "session-1",
                "reason": "wake_word",
            }
        )

        self.assertEqual(stop.reason, StopReason.WAKE_WORD)
        self.assertEqual(stop.to_dict()["reason"], "wake_word")

    def test_stop_rejects_unknown_reason(self):
        with self.assertRaises(SmartAppError) as raised:
            StopApp.from_dict(
                {
                    "requestId": "request-2",
                    "command": "stop_app",
                    "sessionId": "session-1",
                    "reason": "shutdown",
                }
            )

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_cloud_data_defaults_target_and_optional_metadata(self):
        cloud_data = CloudData.from_dict(
            {
                "requestId": "request-3",
                "command": "cloud_data",
                "sessionId": "session-1",
                "seq": 0,
                "data": {"hello": "world"},
            }
        )

        self.assertEqual(cloud_data.target, MessageTarget.AUTO)
        self.assertIsNone(cloud_data.data_type)
        self.assertIsNone(cloud_data.trigger)
        self.assertEqual(cloud_data.to_dict()["target"], "auto")

    def test_cloud_data_parses_target_enum_and_rejects_boolean_seq(self):
        payload = {
            "requestId": "request-3",
            "command": "cloud_data",
            "sessionId": "session-1",
            "seq": True,
            "target": "web",
            "data": {},
        }
        with self.assertRaises(SmartAppError) as raised:
            CloudData.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)
        payload["seq"] = 2 ** 63
        with self.assertRaises(SmartAppError) as raised:
            CloudData.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_cloud_data_rejects_unknown_target(self):
        payload = {
            "requestId": "request-3",
            "command": "cloud_data",
            "sessionId": "session-1",
            "seq": 1,
            "target": "native",
            "data": {},
        }
        with self.assertRaises(SmartAppError) as raised:
            CloudData.from_dict(payload)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)

    def test_get_status_has_no_additional_fields(self):
        with self.assertRaisesRegex(SmartAppError, "unknown field"):
            GetStatus.from_dict(
                {"requestId": "request-4", "command": "get_status", "extra": 1}
            )

    def test_session_from_start_serializes_and_rejects_boolean_generation(self):
        session = Session.from_start(StartApp.from_dict(valid_start_payload()), 3)

        self.assertEqual(
            session.to_dict(),
            {
                "sessionId": "session:1",
                "appId": "demo_app",
                "version": "1.0.0",
                "generation": 3,
            },
        )
        with self.assertRaises(SmartAppError) as raised:
            Session.from_start(StartApp.from_dict(valid_start_payload()), True)

        self.assertEqual(raised.exception.code, ErrorCode.VALIDATION_ERROR)


if __name__ == "__main__":
    unittest.main()
