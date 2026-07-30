import io
import json
import queue
import subprocess
import threading
import time
import unittest
from unittest import mock

import announce


class SummaryFallbackTests(unittest.TestCase):
    def config(self):
        config = dict(announce.DEFAULTS)
        config.update(
            {
                "summary": "command",
                "summary_fallback": "codex",
                "summary_first_activity_timeout_seconds": 5,
                "codex_model": "gpt-5.6-terra",
                "summary_command": ["summarize"],
                "summary_command_timeout_seconds": 30,
            }
        )
        return config

    @mock.patch.object(announce, "codex_summary", return_value="Terra summary")
    @mock.patch.object(announce, "command_summary", return_value=None)
    def test_command_failure_falls_back_to_codex(self, command, codex):
        result = announce.make_announcement(
            self.config(), "builder", "billing", "done", "test output"
        )

        self.assertEqual(result, ("Terra summary", "codex-fallback"))
        command.assert_called_once()
        codex.assert_called_once()

    @mock.patch.object(announce, "codex_summary", return_value=None)
    @mock.patch.object(announce, "command_summary", return_value=None)
    def test_both_failures_fall_back_to_template(self, command, codex):
        result = announce.make_announcement(
            self.config(), "builder", "billing", "blocked", "test output"
        )

        self.assertEqual(result, ("builder needs your input in billing.", "template"))

    @mock.patch.object(announce, "codex_summary")
    @mock.patch.object(announce, "command_summary", return_value="Sonnet summary")
    def test_successful_command_does_not_call_fallback(self, command, codex):
        result = announce.make_announcement(
            self.config(), "builder", "billing", "done", "test output"
        )

        self.assertEqual(result, ("Sonnet summary", "command"))
        codex.assert_not_called()

    @mock.patch.object(announce.subprocess, "run")
    def test_command_uses_configured_timeout(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["summarize"], 0, stdout="summary\n", stderr=""
        )

        result = announce.command_summary(
            self.config(), "builder", "billing", "done", "test output"
        )

        self.assertEqual(result, "summary")
        self.assertEqual(run.call_args.kwargs["timeout"], 30.0)
        self.assertEqual(
            run.call_args.kwargs["env"][
                "HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS"
            ],
            "5",
        )

    @mock.patch.object(announce.subprocess, "run")
    def test_bundled_acp_command_allows_both_timeout_phases(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["python3", "examples/acp-summary.py"],
            0,
            stdout="summary\n",
            stderr="",
        )
        config = self.config()
        config["summary_command"] = ["python3", "examples/acp-summary.py"]

        result = announce.command_summary(
            config, "builder", "billing", "done", "test output"
        )

        self.assertEqual(result, "summary")
        self.assertEqual(run.call_args.kwargs["timeout"], 37.0)

    @mock.patch.object(announce.subprocess, "Popen")
    def test_codex_invalid_first_activity_timeout_falls_back(self, popen):
        config = self.config()
        config["summary_first_activity_timeout_seconds"] = "not-a-number"

        result = announce.codex_summary(
            config, "builder", "billing", "done", "test output"
        )

        self.assertIsNone(result)
        popen.assert_not_called()

    @mock.patch.object(announce.subprocess, "run")
    def test_command_invalid_timeout_falls_back(self, run):
        config = self.config()
        config["summary_command_timeout_seconds"] = "not-a-number"

        result = announce.command_summary(
            config, "builder", "billing", "done", "test output"
        )

        self.assertIsNone(result)
        run.assert_not_called()

    def test_codex_no_model_activity_times_out(self):
        messages = queue.Queue()
        messages.put(json.dumps({"type": "thread.started"}))
        messages.put(json.dumps({"type": "turn.started"}))

        with self.assertRaisesRegex(TimeoutError, "no model activity"):
            announce._collect_codex_summary(
                messages,
                first_activity_deadline=time.monotonic() + 0.005,
                completion_timeout=0.1,
            )

    def test_codex_reasoning_start_allows_slow_completion(self):
        messages = queue.Queue()
        messages.put(
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"id": "item_0", "type": "reasoning"},
                }
            )
        )

        def finish():
            messages.put(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "agent_message",
                            "text": "Terra finished the summary.",
                        },
                    }
                )
            )
            messages.put(json.dumps({"type": "turn.completed"}))

        timer = threading.Timer(0.02, finish)
        timer.start()
        try:
            result = announce._collect_codex_summary(
                messages,
                first_activity_deadline=time.monotonic() + 0.005,
                completion_timeout=0.1,
            )
        finally:
            timer.cancel()

        self.assertEqual(result, "Terra finished the summary.")

    def test_codex_completion_window_starts_at_first_activity(self):
        messages = queue.Queue()

        def start():
            messages.put(
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {"id": "item_0", "type": "reasoning"},
                    }
                )
            )

        def finish():
            messages.put(
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "agent_message",
                            "text": "Finished after a slow start.",
                        },
                    }
                )
            )
            messages.put(json.dumps({"type": "turn.completed"}))

        start_timer = threading.Timer(0.04, start)
        finish_timer = threading.Timer(0.08, finish)
        start_timer.start()
        finish_timer.start()
        try:
            result = announce._collect_codex_summary(
                messages,
                first_activity_deadline=time.monotonic() + 0.05,
                completion_timeout=0.06,
            )
        finally:
            start_timer.cancel()
            finish_timer.cancel()

        self.assertEqual(result, "Finished after a slow start.")

    @mock.patch.object(announce.subprocess, "Popen")
    def test_codex_uses_json_stream_and_configured_model(self, popen):
        events = "\n".join(
            [
                json.dumps(
                    {
                        "type": "item.started",
                        "item": {"id": "item_0", "type": "reasoning"},
                    }
                ),
                json.dumps(
                    {
                        "type": "item.completed",
                        "item": {
                            "id": "item_1",
                            "type": "agent_message",
                            "text": "Terra summary",
                        },
                    }
                ),
                json.dumps({"type": "turn.completed"}),
            ]
        ) + "\n"
        process = mock.Mock()
        process.stdout = io.StringIO(events)
        process.stderr = io.StringIO("")
        process.poll.return_value = 0
        popen.return_value = process

        result = announce.codex_summary(
            self.config(), "builder", "billing", "done", "test output"
        )

        self.assertEqual(result, "Terra summary")
        command = popen.call_args.args[0]
        self.assertIn("--json", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertEqual(command[command.index("-m") + 1], "gpt-5.6-terra")


if __name__ == "__main__":
    unittest.main()
