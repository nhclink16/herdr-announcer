import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import announce
from announcer import summarize


class SummaryTranscriptTests(unittest.TestCase):
    def test_codex_chrome_is_removed_without_removing_response_text(self):
        raw = "\n".join(
            (
                "Finished the billing import.",
                "⚠ Heads up, less than 25% of your weekly limit remains.",
                "• Waiting for tests (47m 10s • esc to interrupt)",
                "› Write tests for @filename",
                "  gpt-5.6-sol · high · Fast off · ~/work · "
                "Context 24% used · weekly 96% left · Main [default]",
                "",
            )
        )

        result = summarize.summary_transcript(raw)

        self.assertEqual(
            result,
            "\n".join(
                (
                    "Finished the billing import.",
                    "⚠ Heads up, less than 25% of your weekly limit remains.",
                    "• Waiting for tests (<elapsed> • esc to interrupt)",
                    "› Write tests for @filename",
                )
            ),
        )


class SummaryReasonTests(unittest.TestCase):
    def config(self):
        config = dict(announce.DEFAULTS)
        config.update(
            {
                "summary_first_activity_timeout_seconds": 5,
                "codex_timeout_seconds": 30,
                "summary_command": ["summarize"],
                "summary_command_timeout_seconds": 20,
            }
        )
        return config

    @mock.patch.object(summarize.subprocess, "Popen")
    def test_codex_failure_records_event_and_last_stderr_line(self, popen):
        process = mock.Mock()
        process.stdout = io.StringIO(json.dumps({"type": "turn.failed"}) + "\n")
        process.stderr = io.StringIO("first\nlast useful error\n")
        process.poll.return_value = 0
        popen.return_value = process
        reasons = []

        result = summarize.codex_summary(
            self.config(), "builder", "work", "done", "transcript", reasons
        )

        self.assertIsNone(result)
        self.assertEqual(reasons, ["codex: turn.failed last useful error"])

    @mock.patch.object(summarize.subprocess, "run")
    def test_every_summary_command_gets_two_phase_outer_budget(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["summarize"], 0, stdout="done\n", stderr=""
        )

        result = summarize.command_summary(
            self.config(), "builder", "work", "done", "transcript"
        )

        self.assertEqual(result, "done")
        self.assertEqual(run.call_args.kwargs["timeout"], 27.0)

    @mock.patch.object(summarize.subprocess, "run")
    def test_command_failure_records_machine_scannable_reason(self, run):
        run.side_effect = subprocess.TimeoutExpired(["summarize"], 27)
        reasons = []

        result = summarize.command_summary(
            self.config(), "builder", "work", "done", "transcript", reasons
        )

        self.assertIsNone(result)
        self.assertEqual(reasons, ["command: timeout"])

    @mock.patch.object(summarize.subprocess, "run")
    def test_command_failure_diagnostic_redacts_inline_secret(self, run):
        secret = "super-secret-1234"
        config = self.config()
        config["summary_command"] = ["summarize", "--api-key", secret]
        run.side_effect = subprocess.CalledProcessError(
            2, config["summary_command"], stderr="failed for " + secret
        )
        reasons = []

        result = summarize.command_summary(
            config, "builder", "work", "done", "transcript", reasons
        )

        self.assertIsNone(result)
        self.assertIn("****1234", reasons[0])
        self.assertNotIn(secret, reasons[0])

    @mock.patch.object(summarize.subprocess, "run")
    def test_command_no_output_diagnostic_redacts_stderr_secret(self, run):
        secret = "sk-super-secret-1234"
        config = self.config()
        config["summary_command"] = ["summarize", secret]
        run.return_value = subprocess.CompletedProcess(
            config["summary_command"], 0, stdout="", stderr="failed for " + secret
        )
        reasons = []

        result = summarize.command_summary(
            config, "builder", "work", "done", "transcript", reasons
        )

        self.assertIsNone(result)
        self.assertIn("****1234", reasons[0])
        self.assertNotIn(secret, reasons[0])

class StatusReasonTests(unittest.TestCase):
    def test_persisted_last_error_and_unknown_keys_appear_in_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            (config_dir / "config.toml").write_text(
                'summary = "template"\ntypo_key = true\n', encoding="utf-8"
            )
            announce.persist_last_error(state_dir, ["elevenlabs: HTTP 401"])
            output = io.StringIO()
            with mock.patch.object(
                announce, "capabilities", return_value={
                    name: None
                    for name in (
                        "codex", "claude", "say", "spd-say", "espeak-ng",
                        "espeak", "mpv", "ffplay", "afplay", "paplay",
                        "pw-play", "aplay",
                    )
                }
            ), contextlib.redirect_stdout(output):
                result = announce.show_status(config_dir, state_dir)

        self.assertEqual(result, 0)
        text = output.getvalue()
        self.assertIn("state: {}".format(state_dir), text)
        self.assertIn("unrecognized keys: typo_key", text)
        self.assertIn("last error:", text)
        self.assertIn("elevenlabs: HTTP 401", text)
        self.assertIn("espeak-ng: no", text)
        self.assertIn("pw-play: no", text)

    def test_status_masks_secrets_embedded_in_commands(self):
        secret = "super-secret-1234"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            (config_dir / "config.toml").write_text(
                'summary_command = ["helper", "--api-key", "{}"]\n'
                'speak_command = ["speaker", "API_TOKEN={}"]\n'.format(
                    secret, secret
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with mock.patch.object(
                announce,
                "capabilities",
                return_value={
                    name: None
                    for name in (
                        "codex", "claude", "say", "spd-say", "espeak-ng",
                        "espeak", "mpv", "ffplay", "afplay", "paplay",
                        "pw-play", "aplay",
                    )
                },
            ), contextlib.redirect_stdout(output):
                announce.show_status(config_dir, state_dir)

        text = output.getvalue()
        self.assertIn("****1234", text)
        self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
