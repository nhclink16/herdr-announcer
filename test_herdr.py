import json
import unittest

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


if __name__ == "__main__":
    unittest.main()
