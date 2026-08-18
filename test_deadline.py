import importlib.util
import json
import queue
import unittest
from pathlib import Path
from unittest import mock

from announcer.deadline import TwoPhaseDeadline


MODULE_PATH = Path(__file__).parent / "examples" / "acp-summary.py"
SPEC = importlib.util.spec_from_file_location("deadline_acp_summary", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
acp_summary = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acp_summary)


class EmptyMessages:
    def __init__(self, clock):
        self.clock = clock

    def get(self, timeout):
        self.clock[0] += timeout
        raise queue.Empty


class DeadlineMachineTests(unittest.TestCase):
    def test_first_activity_timeout(self):
        now = [0.0]
        deadline = TwoPhaseDeadline(
            first_activity_deadline=1.0,
            completion_timeout=5.0,
            first_timeout_message="first activity timed out",
            clock=lambda: now[0],
        )

        with self.assertRaisesRegex(TimeoutError, "first activity"):
            deadline.get(EmptyMessages(now))

    def test_completion_timeout_after_activity(self):
        now = [2.0]
        deadline = TwoPhaseDeadline(
            first_activity_deadline=3.0,
            completion_timeout=4.0,
            completion_timeout_message="completion timed out",
            clock=lambda: now[0],
        )
        deadline.record_activity()
        self.assertEqual(deadline.active_deadline(), 6.0)

        with self.assertRaisesRegex(TimeoutError, "completion"):
            deadline.get(EmptyMessages(now))

    def test_immediate_success_value_is_returned(self):
        messages = mock.Mock()
        messages.get.return_value = "ready"
        deadline = TwoPhaseDeadline(deadline=10.0, clock=lambda: 0.0)

        self.assertEqual(deadline.get(messages), "ready")
        messages.get.assert_called_once_with(timeout=10.0)


class AcpWrapperDeadlineTests(unittest.TestCase):
    def activity_message(self):
        return json.dumps(
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

    def test_wrapper_immediate_success(self):
        messages = queue.Queue()
        messages.put(json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}))

        result = acp_summary.wait_for_response(
            None, messages, 7, deadline=acp_summary.time.monotonic() + 1.0
        )

        self.assertEqual(result, {"ok": True})

    def test_wrapper_first_activity_timeout_uses_shared_machine(self):
        messages = queue.Queue()
        with mock.patch.object(
            acp_summary,
            "TwoPhaseDeadline",
            wraps=TwoPhaseDeadline,
        ) as deadline_class:
            with self.assertRaisesRegex(TimeoutError, "no activity"):
                acp_summary.wait_for_response(
                    None,
                    messages,
                    2,
                    first_activity_deadline=acp_summary.time.monotonic() + 0.001,
                    completion_timeout=0.01,
                )

        deadline_class.assert_called_once()

    def test_wrapper_activity_then_stall_hits_completion_timeout(self):
        messages = queue.Queue()
        messages.put(self.activity_message())

        with self.assertRaisesRegex(TimeoutError, "request timed out"):
            acp_summary.wait_for_response(
                None,
                messages,
                2,
                chunks=[],
                first_activity_deadline=acp_summary.time.monotonic() + 0.05,
                completion_timeout=0.001,
            )


if __name__ == "__main__":
    unittest.main()
