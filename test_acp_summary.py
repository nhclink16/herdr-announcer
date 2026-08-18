import io
import importlib.util
import json
import queue
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parent / "examples" / "acp-summary.py"
SPEC = importlib.util.spec_from_file_location("acp_summary", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
acp_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acp_summary)


class AcpFirstActivityTests(unittest.TestCase):
    @mock.patch.object(acp_summary.subprocess, "Popen")
    def test_start_adapter_uses_pinned_package_version(self, popen):
        acp_summary.start_adapter()

        self.assertEqual(
            popen.call_args.args[0],
            [
                "npx",
                "-y",
                "@agentclientprotocol/claude-agent-acp@0.70.0",
            ],
        )

    @mock.patch.object(acp_summary.subprocess, "Popen")
    def test_start_adapter_inherits_stderr_for_diagnostics(self, popen):
        acp_summary.start_adapter()

        self.assertIsNone(popen.call_args.kwargs["stderr"])

    def test_non_model_events_do_not_satisfy_first_activity(self):
        messages = queue.Queue()
        messages.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {"sessionUpdate": "available_commands_update"}
                    },
                }
            )
        )

        with self.assertRaisesRegex(TimeoutError, "no activity"):
            acp_summary.wait_for_response(
                None,
                messages,
                2,
                time.monotonic() + 0.1,
                [],
                first_activity_deadline=time.monotonic() + 0.005,
            )

    def test_thought_activity_allows_slow_completion(self):
        messages = queue.Queue()
        messages.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "thinking"},
                        }
                    },
                }
            )
        )

        def finish():
            messages.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "Finished."},
                            }
                        },
                    }
                )
            )
            messages.put(
                json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}})
            )

        timer = threading.Timer(0.05, finish)
        timer.start()
        chunks = []
        try:
            result = acp_summary.wait_for_response(
                None,
                messages,
                2,
                None,
                chunks,
                first_activity_deadline=time.monotonic() + 0.005,
                completion_timeout=5.0,
            )
        finally:
            timer.cancel()

        self.assertEqual(result, {})
        self.assertEqual(chunks, ["Finished."])

    def test_completion_window_starts_at_first_activity(self):
        messages = queue.Queue()

        def start():
            messages.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_thought_chunk",
                                "content": {"type": "text", "text": "thinking"},
                            }
                        },
                    }
                )
            )

        def finish():
            messages.put(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": "Finished."},
                            }
                        },
                    }
                )
            )
            messages.put(
                json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}})
            )

        start_timer = threading.Timer(2.0, start)
        finish_timer = threading.Timer(3.0, finish)
        start_timer.start()
        finish_timer.start()
        chunks = []
        try:
            result = acp_summary.wait_for_response(
                None,
                messages,
                2,
                None,
                chunks,
                first_activity_deadline=time.monotonic() + 2.5,
                completion_timeout=2.5,
            )
        finally:
            start_timer.cancel()
            finish_timer.cancel()

        self.assertEqual(result, {})
        self.assertEqual(chunks, ["Finished."])

    @mock.patch.object(acp_summary, "send_message")
    def test_new_session_disables_tools_and_setting_sources(self, send_message):
        messages = queue.Queue()
        messages.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"sessionId": "session-1"},
                }
            )
        )

        session_id = acp_summary.new_session(
            None, messages, time.monotonic() + 0.1, "/tmp/summary"
        )

        self.assertEqual(session_id, "session-1")
        params = send_message.call_args.args[1]["params"]
        self.assertEqual(params["mcpServers"], [])
        self.assertTrue(params["_meta"]["disableBuiltInTools"])
        self.assertEqual(
            params["_meta"]["claudeCode"]["options"]["settingSources"], []
        )

    @mock.patch.object(acp_summary, "initialize")
    @mock.patch.object(acp_summary, "start_adapter")
    def test_activity_deadline_includes_adapter_startup(self, start_adapter, initialize):
        process = mock.Mock()
        process.stdout = io.StringIO("")
        process.stdin = io.StringIO()
        process.poll.return_value = 0

        def delayed_start():
            time.sleep(0.01)
            return process

        def assert_expired(_process, _messages, deadline):
            self.assertLessEqual(deadline, time.monotonic())
            raise TimeoutError("expected")

        start_adapter.side_effect = delayed_start
        initialize.side_effect = assert_expired
        with mock.patch.dict(
            acp_summary.os.environ,
            {
                "HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS": "0.005",
                "HERDR_SUMMARY_OVERALL_TIMEOUT_SECONDS": "0.1",
            },
        ):
            with self.assertRaisesRegex(TimeoutError, "expected"):
                acp_summary.generate("summarize", "transcript")


if __name__ == "__main__":
    unittest.main()
