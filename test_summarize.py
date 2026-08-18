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


if __name__ == "__main__":
    unittest.main()
