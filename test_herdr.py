import json
import subprocess
import unittest
from unittest import mock

from announcer import herdr
from announcer.herdr import parse_event


class ParseEventTests(unittest.TestCase):
    def test_known_top_level_shape(self):
        self.assertEqual(
            parse_event(json.dumps({"pane_id": "p1", "agent_status": "DONE"})),
            ("p1", "done"),
        )

    def test_nested_shape_uses_compatibility_fallback(self):
        payload = {"event": {"pane_id": "nested", "agent_status": "blocked"}}
        self.assertEqual(parse_event(json.dumps(payload)), ("nested", "blocked"))

    def test_top_level_values_win_when_both_are_present(self):
        payload = {
            "pane_id": "top",
            "agent_status": "done",
            "nested": {"pane_id": "nested", "agent_status": "blocked"},
        }
        self.assertEqual(parse_event(json.dumps(payload)), ("top", "done"))

    def test_each_missing_top_level_field_falls_back_independently(self):
        payload = {
            "pane_id": "top",
            "nested": {"pane_id": "nested", "agent_status": "idle"},
        }
        self.assertEqual(parse_event(json.dumps(payload)), ("top", "idle"))

    def test_junk_payloads_are_rejected(self):
        for payload in (None, [], {}, "text", 12):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_event(json.dumps(payload))

    def test_non_string_values_do_not_shadow_nested_strings(self):
        payload = {
            "pane_id": 9,
            "agent_status": False,
            "nested": {"pane_id": "p2", "agent_status": "working"},
        }
        self.assertEqual(parse_event(json.dumps(payload)), ("p2", "working"))

    def test_invalid_status_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid agent_status"):
            parse_event('{"pane_id":"p","agent_status":"sleeping"}')


class HerdrClientTests(unittest.TestCase):
    @mock.patch.object(herdr, "_run_text")
    def test_context_accepts_wrapped_agents_and_top_level_workspaces(self, run):
        run.side_effect = (
            json.dumps(
                {
                    "result": {
                        "agents": [
                            {
                                "pane_id": "p1",
                                "name": "builder",
                                "workspace_id": "w1",
                            }
                        ]
                    }
                }
            ),
            json.dumps({"workspaces": [{"id": "w1", "label": "billing"}]}),
        )

        result = herdr.get_context("herdr", "p1")

        self.assertEqual(result, ("builder", "billing"))
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["herdr", "agent", "list"], ["herdr", "workspace", "list"]],
        )

    @mock.patch.object(herdr, "_run_text")
    def test_missing_pane_returns_generic_context_and_reason(self, run):
        run.return_value = json.dumps({"result": {"agents": []}})
        reasons = []

        result = herdr.get_context("herdr", "missing", reasons)

        self.assertEqual(result, ("an agent", ""))
        self.assertEqual(reasons, ["herdr: pane-not-found"])

    @mock.patch.object(herdr, "_run_text")
    def test_agent_list_error_is_short_and_nonfatal(self, run):
        run.side_effect = OSError("unavailable")
        reasons = []

        result = herdr.get_context("herdr", "p1", reasons)

        self.assertEqual(result, ("an agent", ""))
        self.assertEqual(reasons, ["herdr-agent: unavailable"])

    def test_extract_read_text_supports_plain_and_nested_json(self):
        self.assertEqual(herdr._extract_read_text("plain output"), "plain output")
        self.assertEqual(
            herdr._extract_read_text(
                json.dumps({"result": {"content": ["one", "two"]}})
            ),
            "one\ntwo",
        )

    @mock.patch.object(herdr, "_run_text")
    def test_transcript_falls_back_to_pane_read_and_keeps_tail(self, run):
        transcript = "x" * 4500
        run.side_effect = (
            subprocess.CalledProcessError(1, ["herdr", "agent", "read"]),
            json.dumps({"result": {"text": transcript}}),
        )

        result = herdr.get_transcript("herdr", "p1")

        self.assertEqual(result, transcript[-4000:])
        self.assertEqual(run.call_args_list[0].args[0][1:3], ["agent", "read"])
        self.assertEqual(run.call_args_list[1].args[0][1:3], ["pane", "read"])

    @mock.patch.object(herdr, "_run_text")
    def test_transcript_records_reason_when_both_reads_fail(self, run):
        run.side_effect = OSError("missing")
        reasons = []

        result = herdr.get_transcript("herdr", "p1", reasons)

        self.assertEqual(result, "")
        self.assertEqual(reasons, ["herdr-read: missing"])


if __name__ == "__main__":
    unittest.main()
