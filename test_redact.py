import tempfile
import unittest
from pathlib import Path

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
                "Authorization: Bearer ****3456",
            ],
        )

    def test_redact_command_keeps_existing_paths_and_prose_visible(self):
        script = str(
            Path(__file__).resolve().parent / "examples" / "acp-summary.py"
        )
        instruction = (
            "One spoken sentence, maximum 25 words, plain words only, no "
            "markdown or file paths, leading with the agent name {agent} "
            "which just changed to {status} in workspace {workspace}: "
            "summarize the agent terminal output below for a voice "
            "announcement. Reply with only the sentence."
        )
        command = ["python3", script, instruction]

        self.assertEqual(redact_command(command), command)

    def test_redact_command_masks_credential_shaped_positionals(self):
        credentials = (
            ("sk-example-secret-1234", "****1234"),
            ("ghp_example-token-5678", "****5678"),
            ("xoxb-example-secret-9012", "****9012"),
            ("AKIAIOSFODNN7EXAMPLE", "****MPLE"),
            (
                "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
                "signatureABCD1234",
                "****1234",
            ),
            ("Bearer bearer-secret-3456", "Bearer ****3456"),
            ("AbCdEfGhIjKlMnOp1234", "****1234"),
        )

        for credential, expected in credentials:
            with self.subTest(credential=credential):
                self.assertEqual(
                    redact_command(["helper", credential]),
                    ["helper", expected],
                )

    def test_redact_command_applies_secret_rules_to_argv_zero(self):
        self.assertEqual(
            redact_command(["sk-single-command-secret-1234"]),
            ["****1234"],
        )

    def test_redact_command_keeps_non_sensitive_assignment_values_visible(self):
        script = str(
            Path(__file__).resolve().parent / "examples" / "acp-summary.py"
        )
        command = ["helper", "MODE=debug", "--output={}".format(script)]

        self.assertEqual(redact_command(command), command)

    def test_redact_command_keeps_flags_and_short_plain_values_visible(self):
        command = ["helper", "--verbose", "word", "-XPOST"]

        self.assertEqual(redact_command(command), command)

    def test_redact_command_does_not_mask_an_existing_high_entropy_path(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "AbCdEfGhIjKlMnOp1234"
            path.touch()

            self.assertEqual(redact_command([str(path)]), [str(path)])

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

    def test_redact_command_text_removes_positional_credentials_from_diagnostics(self):
        command = ["helper", "sk-private-topic-4321"]

        redacted = redact_command_text(str(command), command)

        self.assertNotIn("sk-private-topic-4321", redacted)
        self.assertIn("****4321", redacted)


if __name__ == "__main__":
    unittest.main()
