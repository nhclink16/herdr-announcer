import importlib.util
import json
import queue
import threading
import time
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parent / "examples" / "acp-summary.py"
SPEC = importlib.util.spec_from_file_location("acp_summary", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
acp_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acp_summary)


class AcpFirstActivityTests(unittest.TestCase):
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

        timer = threading.Timer(0.02, finish)
        timer.start()
        chunks = []
        try:
            result = acp_summary.wait_for_response(
                None,
                messages,
                2,
                time.monotonic() + 0.1,
                chunks,
                first_activity_deadline=time.monotonic() + 0.005,
            )
        finally:
            timer.cancel()

        self.assertEqual(result, {})
        self.assertEqual(chunks, ["Finished."])


if __name__ == "__main__":
    unittest.main()
