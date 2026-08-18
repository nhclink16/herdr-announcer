import unittest

from announcer.redact import mask_secret, redact_command, redact_command_text


class CommandRedactionTests(unittest.TestCase):
    def test_mask_secret_matches_the_wizard_preview_shape(self):
        self.assertEqual(mask_secret("super-secret-1234"), "****1234")
        self.assertEqual(mask_secret("key"), "****")
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
                "****3456",
            ],
        )

    def test_redact_command_masks_unlabeled_positional_values(self):
        command = ["helper", "private-topic-4321", "--verbose", "-c", "curl private-url-8765"]

        redacted = redact_command(command)

        self.assertEqual(
            redacted,
            ["helper", "****4321", "--verbose", "-c", "****8765"],
        )

    def test_redact_command_masks_attached_option_values(self):
        commands = (
            (["mysql", "-psuper-secret-1234"], "super-secret-1234"),
            (
                ["curl", "-HAuthorization: Bearer super-secret-1234"],
                "super-secret-1234",
            ),
            (["helper", "--tokensuper-secret-1234"], "super-secret-1234"),
        )

        for command, secret in commands:
            with self.subTest(command=command):
                rendered = " ".join(redact_command(command))
                diagnostic = redact_command_text(
                    "Command {!r} failed".format(command), command
                )
                self.assertNotIn(secret, rendered)
                self.assertNotIn(secret, diagnostic)
                self.assertIn("****1234", rendered)
                self.assertIn("****1234", diagnostic)

    def test_redact_command_text_removes_recognized_values_from_diagnostics(self):
        command = ["helper", "--api-key", "super-secret-1234"]
        diagnostic = (
            "Command '['helper', '--api-key', 'super-secret-1234']' "
            "returned non-zero exit status 2"
        )

        redacted = redact_command_text(diagnostic, command)

        self.assertNotIn("super-secret-1234", redacted)
        self.assertIn("****1234", redacted)

    def test_redact_command_text_removes_unlabeled_values_from_diagnostics(self):
        command = ["helper", "private-topic-4321"]

        redacted = redact_command_text(str(command), command)

        self.assertNotIn("private-topic-4321", redacted)
        self.assertIn("****4321", redacted)


if __name__ == "__main__":
    unittest.main()
