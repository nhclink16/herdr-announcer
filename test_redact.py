import unittest

from announcer.redact import mask_secret, redact_command, redact_command_text


class CommandRedactionTests(unittest.TestCase):
    def test_mask_secret_matches_the_wizard_preview_shape(self):
        self.assertEqual(mask_secret("super-secret-1234"), "****1234")
        self.assertEqual(mask_secret(""), "")

    def test_redact_command_masks_common_inline_secret_shapes(self):
        command = [
            "helper",
            "--api-key",
            "super-secret-1234",
            "--token=token-value-5678",
            "SERVICE_PASSWORD=password-9012",
            "-H",
            "Authorization: Bearer bearer-secret-3456",
        ]

        self.assertEqual(
            redact_command(command),
            [
                "helper",
                "--api-key",
                "****1234",
                "--token=****5678",
                "SERVICE_PASSWORD=****9012",
                "-H",
                "Authorization: Bearer ****3456",
            ],
        )

    def test_redact_command_text_removes_recognized_values_from_diagnostics(self):
        command = ["helper", "--api-key", "super-secret-1234"]
        diagnostic = (
            "Command '['helper', '--api-key', 'super-secret-1234']' "
            "returned non-zero exit status 2"
        )

        redacted = redact_command_text(diagnostic, command)

        self.assertNotIn("super-secret-1234", redacted)
        self.assertIn("****1234", redacted)


if __name__ == "__main__":
    unittest.main()
