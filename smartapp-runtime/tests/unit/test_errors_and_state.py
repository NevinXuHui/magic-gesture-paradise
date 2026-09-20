import unittest

from smartapp_runtime.domain.errors import ErrorCode, SmartAppError
from smartapp_runtime.domain.state import RuntimeState


class ErrorAndStateTests(unittest.TestCase):
    def test_error_redacts_secrets_from_malformed_url(self):
        error = SmartAppError(
            ErrorCode.INTERNAL_ERROR,
            "upstream http://user:secret@[?token=value#fragment",
        )

        self.assertEqual(error.message, "upstream http://[")
        for secret in ("user", "secret", "token=value", "fragment"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, error.message)

    def test_error_keeps_malformed_url_as_sanitized_text(self):
        error = SmartAppError(ErrorCode.INTERNAL_ERROR, "upstream http://[")

        self.assertEqual(error.message, "upstream http://[")
        self.assertEqual(
            error.to_dict(),
            {"code": "INTERNAL_ERROR", "message": "upstream http://["},
        )

    def test_error_sanitizes_and_copies_details(self):
        details = {"source": "https://user:secret@example.test/path?token=abc#frag\nnext"}
        error = SmartAppError(
            ErrorCode.DOWNLOAD_FAILED,
            "  https://user:secret@example.test/path?token=abc#frag\tfailed  ",
            details,
        )
        details["source"] = "changed"

        self.assertEqual(error.code, ErrorCode.DOWNLOAD_FAILED)
        self.assertEqual(error.message, "https://example.test/path failed")
        self.assertEqual(error.details["source"], "https://example.test/path next")
        self.assertEqual(
            error.to_dict(),
            {
                "code": "DOWNLOAD_FAILED",
                "message": "https://example.test/path failed",
                "details": {"source": "https://example.test/path next"},
            },
        )

    def test_error_omits_empty_details_and_limits_message_length(self):
        error = SmartAppError(ErrorCode.INTERNAL_ERROR, "x" * 600)

        self.assertEqual(len(error.message), 512)
        self.assertEqual(
            error.to_dict(), {"code": "INTERNAL_ERROR", "message": "x" * 512}
        )

    def test_runtime_states_have_exact_stable_values(self):
        self.assertEqual(
            [state.value for state in RuntimeState],
            [
                "BOOT_RECOVERY",
                "IDLE",
                "PREPARING",
                "DOWNLOADING",
                "VERIFYING",
                "INSTALLING",
                "STARTING",
                "RUNNING",
                "STOPPING",
                "CLEANING",
            ],
        )


if __name__ == "__main__":
    unittest.main()
