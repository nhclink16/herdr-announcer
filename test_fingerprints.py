import tempfile
import unittest
from pathlib import Path

from announcer import fingerprints


class ContentFingerprintStateTests(unittest.TestCase):
    def test_only_recorded_content_matches_the_next_process(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)

            first = fingerprints.is_duplicate_content(
                state_dir, "p1", "first"
            )
            fingerprints.record_content_fingerprint(
                state_dir, "p1", "first"
            )
            second = fingerprints.is_duplicate_content(
                state_dir, "p1", "first"
            )

            saved = fingerprints.load_content_fingerprints(
                state_dir / fingerprints.FINGERPRINT_STATE_FILE
            )

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(saved, {"p1": "first"})

    def test_checks_do_not_persist_an_inflight_announcement(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            first = fingerprints.is_duplicate_content(
                state_dir, "p1", "same"
            )
            second = fingerprints.is_duplicate_content(
                state_dir, "p1", "same"
            )
            saved = fingerprints.load_content_fingerprints(
                state_dir / fingerprints.FINGERPRINT_STATE_FILE
            )

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertEqual(saved, {})

    def test_duplicate_reservation_still_prunes_closed_panes(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            fingerprints.record_content_fingerprint(
                state_dir, "p1", "same"
            )
            fingerprints.record_content_fingerprint(
                state_dir, "closed", "old"
            )

            result = fingerprints.is_duplicate_content(
                state_dir,
                "p1",
                "same",
                active_pane_ids={"p1"},
            )
            saved = fingerprints.load_content_fingerprints(
                state_dir / fingerprints.FINGERPRINT_STATE_FILE
            )

        self.assertTrue(result)
        self.assertEqual(saved, {"p1": "same"})

    def test_current_event_pane_survives_a_lagging_active_pane_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            fingerprints.record_content_fingerprint(
                state_dir, "p1", "same"
            )

            result = fingerprints.is_duplicate_content(
                state_dir,
                "p1",
                "same",
                active_pane_ids=set(),
            )

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
