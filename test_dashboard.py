#!/usr/bin/env python3
"""Tests for the announcer dashboard (dashboard.py).

Every test runs headless: both plugin directories are redirected into a
temporary directory, no test speaks, spawns a real subprocess, or needs a TTY.
"""

import contextlib
import importlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import announce
import dashboard
from announcer import config_io


NOW = 1_700_000_000.0


# ---- shared harness --------------------------------------------------------


class FakeIO:
    """Drives Dashboard headlessly."""

    def __init__(self, keys):
        self.keys = list(keys)
        self.written = []

    def read_key(self):
        return self.keys.pop(0) if self.keys else "q"

    def write(self, text):
        self.written.append(text)
        return len(text)

    def flush(self):
        pass

    def wait_input(self, timeout):
        return True


def make_dashboard(tmp, keys=(), now=NOW):
    fake_io = FakeIO(keys)
    board = dashboard.Dashboard(
        tmp / "config", tmp / "state",
        read_key=fake_io.read_key, write=fake_io.write, flush=fake_io.flush,
        wait_input=fake_io.wait_input, now=lambda: now,
    )
    return board, fake_io


@contextlib.contextmanager
def terminal_size(columns, lines):
    """Pin the measured terminal so frame geometry is deterministic."""
    with mock.patch.object(dashboard.shutil, "get_terminal_size",
                           return_value=os.terminal_size((columns, lines))):
        yield


@contextlib.contextmanager
def capture():
    """Capture stdout and stderr so the suite stays silent."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        yield out, err


def entry(action="announced", timestamp="2026-08-17T12:04:11+02:00",
          pane_id="pane-1", status="done", elapsed=1.5, reasons=None):
    return dashboard.LogEntry(
        timestamp=timestamp,
        pane_id=pane_id,
        status=status,
        action=action,
        elapsed=elapsed,
        reasons=list(reasons or []),
        raw="{} pane_id={} status={} action={} elapsed={}".format(
            timestamp, pane_id, status, action, elapsed
        ),
    )


def log_line(timestamp="2026-08-17T12:04:11+02:00", pane_id="pane-1",
             status="done", action="announced", elapsed="0.421", reasons=""):
    line = "{} pane_id={} status={} action={} elapsed={}".format(
        timestamp, pane_id, status, action, elapsed
    )
    if reasons:
        line += " reasons=" + reasons
    return line


class DashboardTestCase(unittest.TestCase):
    """Temporary plugin dirs, exported through the Herdr env vars."""

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.config_dir = self.tmp / "config"
        self.state_dir = self.tmp / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        patcher = mock.patch.dict(
            os.environ,
            {
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # the frame clamps itself to the terminal, so every test that builds a
        # frame is pinned to the popup geometry the manifest asks Herdr for
        size = mock.patch.object(
            dashboard.shutil, "get_terminal_size",
            return_value=os.terminal_size(
                (dashboard.POPUP_COLUMNS, dashboard.POPUP_LINES)
            ),
        )
        size.start()
        self.addCleanup(size.stop)

    def write_config(self, text):
        self.config_dir.mkdir(parents=True, exist_ok=True)
        path = self.config_dir / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def write_log(self, lines):
        path = self.state_dir / "announcer.log"
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return path

    def board(self, keys=(), now=NOW):
        board, fake_io = make_dashboard(self.tmp, keys, now)
        self.assertFalse(
            board.owns_terminal,
            "injected io must leave owns_terminal False - every terminal "
            "safety guarantee in this suite depends on it",
        )
        return board, fake_io


# ---- text helpers ----------------------------------------------------------


class TextHelperTests(unittest.TestCase):
    def test_clip_flattens_line_breaks_but_keeps_spacing(self):
        # collapsing interior spaces here squeezed _pad's columns back out
        self.assertEqual(dashboard._clip("  a \n b\tc  ", 40), "  a   b c  ")
        self.assertEqual(dashboard._clip("a\r\nb", 40), "a b")

    def test_clip_and_cut_are_the_same_helper(self):
        self.assertIs(dashboard._cut, dashboard._clip)

    def test_clip_never_exceeds_width(self):
        clipped = dashboard._clip("x" * 200, 12)
        self.assertLessEqual(len(clipped), 12)
        self.assertTrue(clipped.startswith("x"))

    def test_clip_leaves_short_text_alone(self):
        self.assertEqual(dashboard._clip("short", 40), "short")

    def test_clip_accepts_non_strings(self):
        self.assertEqual(dashboard._clip(7, 10), "7")

    def test_pad_is_exactly_width(self):
        self.assertEqual(len(dashboard._pad("ab", 9)), 9)
        self.assertEqual(len(dashboard._pad("y" * 40, 9)), 9)
        self.assertTrue(dashboard._pad("ab", 9).startswith("ab"))

    def test_plain_strips_ansi(self):
        colored = dashboard.kit._c("hello", "36")
        self.assertNotEqual(colored, "hello")
        self.assertEqual(dashboard._plain(colored), "hello")

    def test_plain_leaves_clean_text_alone(self):
        self.assertEqual(dashboard._plain("plain text"), "plain text")

    def test_label_bolds_only_when_focused(self):
        self.assertEqual(dashboard._label("row", False), "row")
        self.assertEqual(dashboard._label("row", True),
                         dashboard.kit._c("row", "1"))


# ---- log parsing -----------------------------------------------------------


class LogParsingTests(DashboardTestCase):
    def test_legacy_line_parses_every_field(self):
        parsed = dashboard.parse_log_line(log_line())

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.timestamp, "2026-08-17T12:04:11+02:00")
        self.assertEqual(parsed.pane_id, "pane-1")
        self.assertEqual(parsed.status, "done")
        self.assertEqual(parsed.action, "announced")
        self.assertEqual(parsed.elapsed, 0.421)
        self.assertIsInstance(parsed.elapsed, float)
        self.assertEqual(parsed.reasons, [])

    def test_line_with_reasons_parses_both_reasons(self):
        parsed = dashboard.parse_log_line(
            log_line(action="snoozed", reasons="debounced;quiet-hours")
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.action, "snoozed")
        self.assertEqual(parsed.reasons, ["debounced", "quiet-hours"])

    def test_reasons_ignores_empty_segments(self):
        parsed = dashboard.parse_log_line(log_line(reasons="only;;"))

        self.assertEqual(parsed.reasons, ["only"])

    def test_unknown_future_field_is_ignored(self):
        parsed = dashboard.parse_log_line(log_line() + " foo=bar")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.action, "announced")
        self.assertEqual(parsed.pane_id, "pane-1")

    def test_missing_pane_and_status_default_to_dash(self):
        parsed = dashboard.parse_log_line(
            "2026-08-17T12:04:11+02:00 action=error elapsed=0.010"
        )

        self.assertEqual(parsed.pane_id, "-")
        self.assertEqual(parsed.status, "-")
        self.assertEqual(parsed.action, "error")

    def test_timestamp_is_empty_when_first_token_is_a_field(self):
        parsed = dashboard.parse_log_line("action=error elapsed=0.010")

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.timestamp, "")

    def test_traceback_lines_after_an_error_are_not_records(self):
        traceback_lines = [
            "Traceback (most recent call last):",
            '  File "announce.py", line 1630, in main',
            "    action = process_invocation(",
            "ValueError: event payload has no string pane_id",
        ]
        for line in traceback_lines:
            self.assertIsNone(dashboard.parse_log_line(line), line)

    def test_blank_line_is_not_a_record(self):
        self.assertIsNone(dashboard.parse_log_line(""))
        self.assertIsNone(dashboard.parse_log_line("\n"))
        self.assertIsNone(dashboard.parse_log_line("   \n"))

    def test_line_without_action_is_not_a_record(self):
        self.assertIsNone(
            dashboard.parse_log_line(
                "2026-08-17T12:04:11+02:00 pane_id=pane-1 status=done"
            )
        )

    def test_unparseable_elapsed_becomes_zero(self):
        parsed = dashboard.parse_log_line(log_line(elapsed="nope"))

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.elapsed, 0.0)

    def test_raw_keeps_the_original_line(self):
        line = log_line()
        parsed = dashboard.parse_log_line(line + "\n")

        self.assertEqual(parsed.raw, line)

    def test_read_log_missing_file_is_empty(self):
        self.assertEqual(dashboard.read_log(self.state_dir / "nope.log"), [])

    def test_read_log_unreadable_path_is_empty(self):
        self.assertEqual(dashboard.read_log(self.state_dir), [])

    def test_read_log_returns_oldest_first(self):
        self.write_log([log_line(action="a{}".format(i)) for i in range(4)])

        entries = dashboard.read_log(self.state_dir / "announcer.log", 10)

        self.assertEqual([e.action for e in entries], ["a0", "a1", "a2", "a3"])

    def test_read_log_limit_keeps_the_newest_entries(self):
        self.write_log([log_line(action="a{}".format(i)) for i in range(10)])

        entries = dashboard.read_log(self.state_dir / "announcer.log", 3)

        self.assertEqual([e.action for e in entries], ["a7", "a8", "a9"])

    def test_read_log_default_limit_is_recent_lines(self):
        self.write_log([log_line(action="a{}".format(i)) for i in range(20)])

        entries = dashboard.read_log(self.state_dir / "announcer.log")

        self.assertEqual(len(entries), dashboard.RECENT_LINES)

    def test_read_log_skips_traceback_lines(self):
        self.write_log([
            log_line(action="announced"),
            log_line(action="error"),
            "Traceback (most recent call last):",
            '  File "announce.py", line 1630, in main',
            "ValueError: boom",
            log_line(action="debounced"),
        ])

        entries = dashboard.read_log(self.state_dir / "announcer.log", 10)

        self.assertEqual([e.action for e in entries],
                         ["announced", "error", "debounced"])

    def test_read_log_tail_drops_the_leading_partial_line(self):
        path = self.state_dir / "announcer.log"
        huge = "x" * (dashboard.LOG_SCAN_BYTES + 4096) + " action=phantom"
        with path.open("w", encoding="utf-8") as handle:
            handle.write(huge + "\n")
            for index in range(3):
                handle.write(log_line(action="tail{}".format(index)) + "\n")
        self.assertGreater(path.stat().st_size, dashboard.LOG_SCAN_BYTES)

        entries = dashboard.read_log(path, 50)

        self.assertEqual([e.action for e in entries],
                         ["tail0", "tail1", "tail2"])
        self.assertNotIn("phantom", [e.action for e in entries])

    def test_read_log_large_file_still_returns_the_newest_entries(self):
        path = self.state_dir / "announcer.log"
        lines = [log_line(action="a{}".format(index), pane_id="pane-%d" % index)
                 for index in range(4000)]
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        self.assertGreater(path.stat().st_size, dashboard.LOG_SCAN_BYTES)

        entries = dashboard.read_log(path, dashboard.RECENT_LINES)

        self.assertEqual([e.action for e in entries],
                         ["a3995", "a3996", "a3997", "a3998", "a3999"])

    def test_format_timestamp_of_iso_stamp(self):
        self.assertEqual(
            dashboard.format_timestamp("2026-08-17T12:04:11+02:00"), "12:04:11"
        )

    def test_format_timestamp_of_garbage_is_clipped(self):
        result = dashboard.format_timestamp("not-a-timestamp")

        self.assertLessEqual(len(result), 8)
        self.assertTrue(result.startswith("not-a"))

    def test_format_timestamp_of_empty_string(self):
        self.assertEqual(dashboard.format_timestamp(""), "")


# ---- snooze state ----------------------------------------------------------


class SnoozeStateTests(DashboardTestCase):
    def test_snooze_path_is_state_dir_snooze_json(self):
        self.assertEqual(
            dashboard.snooze_path(self.state_dir), self.state_dir / "snooze.json"
        )

    def test_write_then_read_round_trip(self):
        dashboard.write_snooze(self.state_dir, NOW + 600.0)

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), NOW + 600.0)
        self.assertTrue(dashboard.snooze_active(self.state_dir, NOW))

    def test_written_document_is_exactly_until(self):
        dashboard.write_snooze(self.state_dir, NOW + 60.0)

        with dashboard.snooze_path(self.state_dir).open(encoding="utf-8") as fh:
            payload = json.load(fh)

        self.assertEqual(payload, {"until": NOW + 60.0})
        self.assertIsInstance(payload["until"], float)

    def test_write_snooze_creates_a_missing_state_dir(self):
        fresh = self.tmp / "fresh-state"

        dashboard.write_snooze(fresh, NOW + 10.0)

        self.assertTrue((fresh / "snooze.json").exists())

    def test_write_snooze_leaves_no_temp_files_behind(self):
        dashboard.write_snooze(self.state_dir, NOW + 10.0)

        leftovers = [p.name for p in self.state_dir.iterdir()
                     if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_expired_deadline_reads_as_off(self):
        dashboard.write_snooze(self.state_dir, NOW - 1.0)

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)
        self.assertFalse(dashboard.snooze_active(self.state_dir, NOW))

    def test_deadline_exactly_now_reads_as_off(self):
        dashboard.write_snooze(self.state_dir, NOW)

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_missing_file_reads_as_off(self):
        self.assertEqual(dashboard.read_snooze(self.tmp / "gone", NOW), 0.0)

    def test_zero_reads_as_off(self):
        dashboard.write_snooze(self.state_dir, 0.0)

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_corrupt_bytes_read_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text("{not json",
                                                         encoding="utf-8")

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_json_list_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text("[1, 2, 3]",
                                                         encoding="utf-8")

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_json_scalar_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text("42", encoding="utf-8")

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_non_numeric_until_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text(
            json.dumps({"until": "soon"}), encoding="utf-8"
        )

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_infinite_until_reads_as_off(self):
        # json.load accepts Infinity by default; an infinite deadline would
        # mute the plugin forever and overflow the countdown formatter.
        dashboard.snooze_path(self.state_dir).write_text(
            '{"until": 1e999}', encoding="utf-8"
        )

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)
        self.assertFalse(dashboard.snooze_active(self.state_dir, NOW))

    def test_literal_infinity_token_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text(
            '{"until": Infinity}', encoding="utf-8"
        )

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_negative_infinity_until_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text(
            '{"until": -Infinity}', encoding="utf-8"
        )

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_nan_until_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text(
            '{"until": NaN}', encoding="utf-8"
        )

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_missing_until_key_reads_as_off(self):
        dashboard.snooze_path(self.state_dir).write_text(
            json.dumps({"other": 1}), encoding="utf-8"
        )

        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_read_snooze_never_rewrites_the_file(self):
        path = dashboard.snooze_path(self.state_dir)
        path.write_text("{not json", encoding="utf-8")

        dashboard.read_snooze(self.state_dir, NOW)

        self.assertTrue(path.exists())
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_read_snooze_defaults_now_to_wall_clock(self):
        dashboard.write_snooze(self.state_dir, NOW + 600.0)

        with mock.patch.object(dashboard.time, "time", return_value=NOW):
            self.assertEqual(dashboard.read_snooze(self.state_dir), NOW + 600.0)

    def test_parse_duration_table(self):
        cases = {
            "30m": 1800.0,
            "2h": 7200.0,
            "45s": 45.0,
            "90": 90.0,
            "  30m  ": 1800.0,
            "2H": 7200.0,
            "off": 0.0,
            "OFF": 0.0,
            "0": 0.0,
            "none": 0.0,
            "None": 0.0,
        }
        for spec, expected in cases.items():
            with self.subTest(spec=spec):
                self.assertEqual(dashboard.parse_duration(spec), expected)

    def test_parse_duration_rejects_junk(self):
        for spec in ("", "   ", "abc", "5x", "m30", "1.5h", "-", "30 m"):
            with self.subTest(spec=spec):
                self.assertIsNone(dashboard.parse_duration(spec))

    def test_set_snooze_writes_a_deadline(self):
        until = dashboard.set_snooze(self.state_dir, "30m", NOW)

        self.assertEqual(until, NOW + 1800.0)
        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), NOW + 1800.0)

    def test_set_snooze_off_writes_zero_and_keeps_the_file(self):
        dashboard.set_snooze(self.state_dir, "30m", NOW)

        result = dashboard.set_snooze(self.state_dir, "off", NOW)

        self.assertEqual(result, 0.0)
        self.assertTrue(dashboard.snooze_path(self.state_dir).exists())
        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)

    def test_set_snooze_with_invalid_spec_writes_nothing(self):
        result = dashboard.set_snooze(self.state_dir, "banana", NOW)

        self.assertIsNone(result)
        self.assertFalse(dashboard.snooze_path(self.state_dir).exists())

    def test_set_snooze_invalid_spec_leaves_an_active_snooze_alone(self):
        dashboard.set_snooze(self.state_dir, "2h", NOW)

        self.assertIsNone(dashboard.set_snooze(self.state_dir, "banana", NOW))
        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), NOW + 7200.0)

    def test_format_snooze_remaining_exact_strings(self):
        cases = [
            (0.0, "off"),
            (NOW - 5.0, "off"),
            (NOW, "off"),
            (NOW + 7200.0, "2h 00m left"),
            (NOW + 3600.0, "1h 00m left"),
            (NOW + 3599.0, "59m 59s left"),
            (NOW + 1800.0, "30m 00s left"),
            (NOW + 90.0, "1m 30s left"),
            (NOW + 60.0, "1m 00s left"),
            (NOW + 59.0, "59s left"),
            (NOW + 30.0, "30s left"),
            (NOW + 0.5, "0s left"),
        ]
        for until, expected in cases:
            with self.subTest(until=until):
                self.assertEqual(
                    dashboard.format_snooze_remaining(until, NOW), expected
                )

    def test_snooze_step_reads_a_deadline_back(self):
        cases = [
            (0.0, "off"),
            (NOW - 10.0, "off"),
            (NOW, "off"),
            (NOW + 60.0, "5m"),
            (NOW + 300.0, "5m"),
            (NOW + 301.0, "30m"),
            (NOW + 1800.0, "30m"),
            (NOW + 1801.0, "2h"),
            (NOW + 7200.0, "2h"),
        ]
        for until, expected in cases:
            with self.subTest(until=until):
                self.assertEqual(dashboard.snooze_step(until, NOW), expected)

    def test_snooze_step_recognises_a_tomorrow_deadline(self):
        until = dashboard.next_morning(NOW)

        self.assertEqual(dashboard.snooze_step(until, NOW), "tomorrow")

    def test_next_snooze_step_cycles_all_five_options(self):
        cases = [
            (0.0, "5m"),
            (NOW - 10.0, "5m"),
            (NOW + 60.0, "30m"),
            (NOW + 300.0, "30m"),
            (NOW + 1500.0, "2h"),
            (NOW + 1800.0, "2h"),
            (NOW + 1801.0, "tomorrow"),
            (NOW + 7200.0, "tomorrow"),
            (dashboard.next_morning(NOW), "off"),
        ]
        for until, expected in cases:
            with self.subTest(until=until):
                self.assertEqual(dashboard.next_snooze_step(until, NOW), expected)

    def test_snooze_steps_constant(self):
        self.assertEqual(
            dashboard.SNOOZE_STEPS, ("5m", "30m", "2h", "tomorrow", "off")
        )

    def test_next_morning_is_the_next_local_eight_oclock(self):
        moment = time.mktime((2026, 8, 17, 9, 30, 0, 0, 0, -1))

        target = time.localtime(dashboard.next_morning(moment))

        self.assertEqual(
            (target.tm_year, target.tm_mon, target.tm_mday), (2026, 8, 18)
        )
        self.assertEqual((target.tm_hour, target.tm_min, target.tm_sec),
                         (dashboard.SNOOZE_HOUR, 0, 0))

    def test_next_morning_at_one_minute_to_midnight_rolls_over(self):
        moment = time.mktime((2026, 8, 17, 23, 59, 0, 0, 0, -1))

        target = time.localtime(dashboard.next_morning(moment))

        self.assertEqual(
            (target.tm_year, target.tm_mon, target.tm_mday), (2026, 8, 18)
        )
        self.assertEqual(target.tm_hour, dashboard.SNOOZE_HOUR)

    def test_next_morning_before_eight_is_this_morning(self):
        moment = time.mktime((2026, 8, 17, 7, 0, 0, 0, 0, -1))

        target = time.localtime(dashboard.next_morning(moment))

        self.assertEqual(target.tm_mday, 17)
        self.assertEqual(target.tm_hour, dashboard.SNOOZE_HOUR)

    def test_set_snooze_tomorrow_writes_the_morning_deadline(self):
        moment = time.mktime((2026, 8, 17, 23, 59, 0, 0, 0, -1))

        until = dashboard.set_snooze(self.state_dir, "tomorrow", moment)

        self.assertEqual(until, dashboard.next_morning(moment))
        self.assertEqual(dashboard.read_snooze(self.state_dir, moment), until)
        payload = json.loads(
            dashboard.snooze_path(self.state_dir).read_text(encoding="utf-8")
        )
        self.assertEqual(set(payload), {"until"})     # schema unchanged

    def test_snooze_label_shows_the_choice_and_the_time_left(self):
        self.assertEqual(dashboard.snooze_label(0.0, NOW), "off")
        self.assertEqual(
            dashboard.snooze_label(NOW + 300.0, NOW), "5m · 5m 00s left"
        )
        self.assertEqual(
            dashboard.snooze_label(NOW + 1800.0, NOW), "30m · 30m 00s left"
        )
        self.assertEqual(
            dashboard.snooze_label(NOW + 7200.0, NOW), "2h · 2h 00m left"
        )

    def test_snooze_label_shows_the_target_for_tomorrow(self):
        until = dashboard.next_morning(NOW)

        label = dashboard.snooze_label(until, NOW)

        self.assertTrue(
            label.startswith("until {:02d}:00 · ".format(dashboard.SNOOZE_HOUR)),
            label,
        )

    def test_snooze_message_wording(self):
        self.assertEqual(dashboard.snooze_message(0.0, NOW), "snooze off")
        self.assertEqual(
            dashboard.snooze_message(NOW + 300.0, NOW), "snoozed · 5m 00s left"
        )
        self.assertEqual(
            dashboard.snooze_message(dashboard.next_morning(NOW), NOW),
            "snoozed until {:02d}:00".format(dashboard.SNOOZE_HOUR),
        )


# ---- announce.py snooze hook ----------------------------------------------


class SnoozeHookTests(DashboardTestCase):
    def event_env(self, pane_id="pane-1", status="done"):
        return mock.patch.dict(
            os.environ,
            {
                "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                    {"pane_id": pane_id, "agent_status": status}
                ),
                "HERDR_BIN_PATH": "herdr-not-real",
            },
        )

    def process(self, test_mode=False):
        return announce.process_invocation(
            self.config_dir, self.state_dir, test_mode, {}
        )

    def test_active_snooze_returns_snoozed_and_speaks_nothing(self):
        dashboard.write_snooze(self.state_dir, time.time() + 600.0)
        with self.event_env(), \
                mock.patch.object(announce, "speak") as speak, \
                mock.patch.object(announce, "get_context") as get_context, \
                mock.patch.object(announce, "get_transcript") as get_transcript:
            result = self.process()

        self.assertEqual(result, "snoozed")
        speak.assert_not_called()
        get_context.assert_not_called()
        get_transcript.assert_not_called()

    def test_an_unsubscribed_status_is_still_skipped_status_while_snoozed(self):
        # action=snoozed must mark exactly the announcements the snooze
        # silenced; an event nobody subscribed to was never going to speak
        self.write_config('announce = ["done"]\n')
        dashboard.write_snooze(self.state_dir, time.time() + 600.0)
        for status in ("working", "idle", "unknown"):
            with self.subTest(status=status):
                with self.event_env(status=status), \
                        mock.patch.object(announce, "speak") as speak:
                    result = self.process()

                self.assertEqual(result, "skipped-status")
                speak.assert_not_called()

    def test_a_subscribed_status_is_snoozed(self):
        self.write_config('announce = ["done"]\n')
        dashboard.write_snooze(self.state_dir, time.time() + 600.0)
        with self.event_env(status="done"), \
                mock.patch.object(announce, "speak") as speak:
            result = self.process()

        self.assertEqual(result, "snoozed")
        speak.assert_not_called()

    def test_snoozed_event_does_not_consume_the_debounce_slot(self):
        dashboard.write_snooze(self.state_dir, time.time() + 600.0)
        with self.event_env(), \
                mock.patch.object(announce, "get_context",
                                  return_value=("builder", "billing")), \
                mock.patch.object(announce, "get_transcript", return_value="out"), \
                mock.patch.object(announce, "make_announcement",
                                  return_value=("all done", "template")), \
                mock.patch.object(announce, "speak"):
            self.process()

        self.assertFalse((self.state_dir / "last.json").exists())

    def test_expired_snooze_runs_the_normal_path(self):
        dashboard.write_snooze(self.state_dir, time.time() - 60.0)
        with self.event_env(), \
                mock.patch.object(announce, "get_context",
                                  return_value=("builder", "billing")), \
                mock.patch.object(announce, "get_transcript", return_value="out"), \
                mock.patch.object(announce, "make_announcement",
                                  return_value=("all done", "template")), \
                mock.patch.object(announce, "speak", return_value="say") as speak:
            result = self.process()

        self.assertTrue(result.startswith("announced+"), result)
        speak.assert_called_once()

    def test_missing_snooze_file_runs_the_normal_path(self):
        with self.event_env(), \
                mock.patch.object(announce, "get_context",
                                  return_value=("builder", "billing")), \
                mock.patch.object(announce, "get_transcript", return_value="out"), \
                mock.patch.object(announce, "make_announcement",
                                  return_value=("all done", "template")), \
                mock.patch.object(announce, "speak", return_value="say"):
            result = self.process()

        self.assertTrue(result.startswith("announced+"), result)

    def test_corrupt_snooze_file_runs_the_normal_path(self):
        (self.state_dir / "snooze.json").write_text("{not json",
                                                    encoding="utf-8")
        with self.event_env(), \
                mock.patch.object(announce, "get_context",
                                  return_value=("builder", "billing")), \
                mock.patch.object(announce, "get_transcript", return_value="out"), \
                mock.patch.object(announce, "make_announcement",
                                  return_value=("all done", "template")), \
                mock.patch.object(announce, "speak", return_value="say"):
            result = self.process()

        self.assertTrue(result.startswith("announced+"), result)

    def test_infinite_snooze_deadline_runs_the_normal_path(self):
        # A corrupt Infinity deadline must not mute announcements forever.
        (self.state_dir / "snooze.json").write_text('{"until": 1e999}',
                                                    encoding="utf-8")
        with self.event_env(), \
                mock.patch.object(announce, "get_context",
                                  return_value=("builder", "billing")), \
                mock.patch.object(announce, "get_transcript", return_value="out"), \
                mock.patch.object(announce, "make_announcement",
                                  return_value=("all done", "template")), \
                mock.patch.object(announce, "speak", return_value="say"):
            result = self.process()

        self.assertTrue(result.startswith("announced+"), result)

    def test_snooze_file_holding_a_list_runs_the_normal_path(self):
        (self.state_dir / "snooze.json").write_text("[1, 2]", encoding="utf-8")
        with self.event_env(), \
                mock.patch.object(announce, "get_context",
                                  return_value=("builder", "billing")), \
                mock.patch.object(announce, "get_transcript", return_value="out"), \
                mock.patch.object(announce, "make_announcement",
                                  return_value=("all done", "template")), \
                mock.patch.object(announce, "speak", return_value="say"):
            result = self.process()

        self.assertTrue(result.startswith("announced+"), result)

    def test_test_mode_bypasses_an_active_snooze(self):
        dashboard.write_snooze(self.state_dir, time.time() + 600.0)
        with self.event_env(), \
                mock.patch.object(announce, "speak", return_value="say") as speak:
            result = self.process(test_mode=True)

        self.assertEqual(result, "announced+say")
        speak.assert_called_once()

    def test_snoozed_invocation_logs_a_parseable_line(self):
        def fake_process(config_dir, state_dir, test_mode, log_context, reasons=None):
            log_context["pane_id"] = "pane-1"
            log_context["status"] = "done"
            return "snoozed"

        with mock.patch.object(sys, "argv", ["announce.py"]), \
                mock.patch.object(announce, "process_invocation",
                                  side_effect=fake_process):
            with capture() as (out, err):
                code = announce.main()

        self.assertEqual(code, 0, err.getvalue())
        text = (self.state_dir / "announcer.log").read_text(encoding="utf-8")
        line = text.strip().splitlines()[-1]
        self.assertRegex(
            line,
            r"pane_id=pane-1 status=done action=snoozed elapsed=[0-9]+\.[0-9]+",
        )
        parsed = dashboard.parse_log_line(line)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.action, "snoozed")
        self.assertEqual(parsed.pane_id, "pane-1")
        self.assertEqual(parsed.status, "done")


# ---- derived display values ------------------------------------------------


class DerivedValueTests(DashboardTestCase):
    def test_announce_states_normalises_case_order_and_duplicates(self):
        config = {"announce": ["BLOCKED", "done", "blocked", "unknown"]}

        self.assertEqual(
            dashboard.announce_states(config), ["done", "blocked", "unknown"]
        )

    def test_announce_states_drops_unknown_and_non_string_members(self):
        config = {"announce": ["done", "sideways", 3, None, True]}

        self.assertEqual(dashboard.announce_states(config), ["done"])

    def test_announce_states_of_a_non_list_is_empty(self):
        self.assertEqual(dashboard.announce_states({"announce": "done"}), [])
        self.assertEqual(dashboard.announce_states({}), [])

    def test_state_order_matches_the_wizard_and_valid_statuses(self):
        self.assertEqual(
            dashboard.STATE_ORDER,
            ("done", "blocked", "idle", "working", "unknown"),
        )
        self.assertEqual(set(dashboard.STATE_ORDER), announce.VALID_STATUSES)
        self.assertEqual(set(dashboard.STATE_HELP), set(dashboard.STATE_ORDER))

    def test_voice_backend_label_prefers_a_custom_command(self):
        config = {"speak_command": ["say", "-v", "Alex"],
                  "elevenlabs_api_key": "key"}

        label = dashboard.voice_backend_label(config, {})

        self.assertTrue(label.startswith("custom command  "), label)
        self.assertIn("say", label)

    def test_voice_backend_label_marks_an_invalid_custom_command(self):
        config = {"speak_command": "say -v Alex"}

        self.assertEqual(
            dashboard.voice_backend_label(config, {}),
            "custom command  (invalid)",
        )

    def test_voice_backend_label_reports_elevenlabs(self):
        config = {"speak_command": None, "elevenlabs_api_key": "key",
                  "elevenlabs_voice_id": "voice-7"}

        self.assertEqual(
            dashboard.voice_backend_label(config, {}),
            "ElevenLabs  voice voice-7",
        )

    def test_voice_backend_label_on_macos(self):
        config = {"speak_command": None, "elevenlabs_api_key": "", "voice": "Alex"}
        with mock.patch.object(dashboard.platform, "system",
                               return_value="Darwin"):
            self.assertEqual(
                dashboard.voice_backend_label(config, {}),
                "local say  voice Alex",
            )
            config["voice"] = ""
            self.assertEqual(
                dashboard.voice_backend_label(config, {}),
                "local say  system voice",
            )

    def test_voice_backend_label_on_linux_lists_detected_tools(self):
        config = {"speak_command": None, "elevenlabs_api_key": "", "voice": ""}
        caps = {"spd-say": "/usr/bin/spd-say", "espeak": "/usr/bin/espeak"}
        with mock.patch.object(dashboard.platform, "system",
                               return_value="Linux"):
            self.assertEqual(
                dashboard.voice_backend_label(config, caps),
                "local spd-say / espeak",
            )

    def test_voice_backend_label_lists_espeak_ng(self):
        # an espeak-ng-only box speaks fine; the probe used to call it silent
        config = {"speak_command": None, "elevenlabs_api_key": "", "voice": ""}
        caps = {name: None for name in dashboard.CAPABILITY_NAMES}
        caps["espeak-ng"] = "/usr/bin/espeak-ng"
        with mock.patch.object(dashboard.platform, "system",
                               return_value="Linux"):
            self.assertEqual(
                dashboard.voice_backend_label(config, caps),
                "local espeak-ng",
            )

    def test_voice_backend_label_probe_order_matches_speech(self):
        self.assertEqual(dashboard.VOICE_TOOLS,
                         ("spd-say", "espeak-ng", "espeak"))
        for name in dashboard.VOICE_TOOLS:
            self.assertIn(name, dashboard.CAPABILITY_NAMES)

    def test_voice_backend_label_on_linux_without_tools(self):
        config = {"speak_command": None, "elevenlabs_api_key": "", "voice": ""}
        caps = {"spd-say": None, "espeak-ng": None, "espeak": None}
        with mock.patch.object(dashboard.platform, "system",
                               return_value="Linux"):
            self.assertEqual(
                dashboard.voice_backend_label(config, caps),
                "local  nothing detected!",
            )

    def test_voice_backend_label_on_an_unsupported_platform(self):
        config = {"speak_command": None, "elevenlabs_api_key": "", "voice": ""}
        with mock.patch.object(dashboard.platform, "system",
                               return_value="Windows"):
            self.assertEqual(
                dashboard.voice_backend_label(config, {}),
                "unsupported platform: Windows",
            )

    def test_capabilities_covers_every_capability_name(self):
        with mock.patch.object(dashboard.shutil, "which",
                               side_effect=lambda name: "/bin/" + name):
            caps = dashboard.capabilities()

        self.assertEqual(set(caps), set(dashboard.CAPABILITY_NAMES))
        self.assertEqual(caps["codex"], "/bin/codex")

    def test_capabilities_reports_missing_tools_as_none(self):
        with mock.patch.object(dashboard.shutil, "which", return_value=None):
            caps = dashboard.capabilities()

        self.assertEqual(set(caps.values()), {None})


# ---- rendering -------------------------------------------------------------


class RenderTests(DashboardTestCase):
    def frame(self, board):
        lines = board.render()
        self.assertEqual(len(lines), dashboard.FRAME_HEIGHT)
        return lines

    def plain(self, board):
        return "\n".join(dashboard._plain(line) for line in board.render())

    def test_frame_height_with_an_empty_config_dir_and_no_log(self):
        board, _unused = self.board()
        board.refresh()

        self.frame(board)

    def test_frame_height_with_one_entry(self):
        board, _unused = self.board()
        board.entries = [entry()]

        self.frame(board)

    def test_frame_height_with_fifty_entries(self):
        board, _unused = self.board()
        board.entries = [entry(action="a{}".format(i)) for i in range(50)]

        self.frame(board)

    def test_frame_height_with_snooze_on_and_off(self):
        board, _unused = self.board()
        board.snooze_until = NOW + 7200.0
        self.frame(board)
        board.snooze_until = 0.0
        self.frame(board)

    def test_frame_height_with_all_states_off(self):
        board, _unused = self.board()
        board.config["announce"] = []

        self.frame(board)

    def test_frame_height_with_every_state_on(self):
        board, _unused = self.board()
        board.config["announce"] = list(dashboard.STATE_ORDER)

        self.frame(board)

    def test_frame_height_with_a_message(self):
        board, _unused = self.board()
        board.message = "x" * 400

        self.frame(board)

    def test_frame_height_with_a_very_long_entry(self):
        board, _unused = self.board()
        board.entries = [entry(action="a" * 500, pane_id="p" * 200,
                               reasons=["r" * 100])]

        self.frame(board)

    def test_frame_height_on_every_focus_row(self):
        board, _unused = self.board()
        board.entries = [entry()]
        for index in range(len(dashboard.ROWS)):
            with self.subTest(index=index):
                board.index = index
                self.frame(board)

    def test_focus_cursor_appears_once_per_frame(self):
        board, _unused = self.board()
        for index in range(len(dashboard.ROWS)):
            with self.subTest(index=index):
                board.index = index
                self.assertEqual(self.plain(board).count("❯"), 1)

    def test_rows_ring_is_the_documented_order(self):
        kinds = [(row.kind, row.key) for row in dashboard.ROWS]

        self.assertEqual(
            kinds,
            [
                ("snooze", ""),
                ("state", "done"),
                ("state", "blocked"),
                ("state", "idle"),
                ("state", "working"),
                ("state", "unknown"),
                ("toast", ""),
                ("test", ""),
                ("wizard", ""),
            ],
        )

    def test_frame_layout_is_the_documented_one(self):
        board, _unused = self.board()
        board.entries = [entry()]
        lines = [dashboard._plain(line) for line in self.frame(board)]

        self.assertIn("Announcer", lines[0])
        self.assertIn("config", lines[1])
        self.assertIn("Recent", lines[3])
        self.assertIn("Snooze", lines[10])
        self.assertIn("Announce on", lines[12])
        self.assertIn("toast", lines[19])
        self.assertIn("voice", lines[20])
        self.assertIn("Test voice", lines[23])
        self.assertIn("Full setup", lines[24])
        self.assertEqual(lines[25], "└")
        for index in (2, 9, 11, 18, 22):
            with self.subTest(index=index):
                self.assertEqual(lines[index], "│")

    def test_frame_contains_the_static_headings(self):
        board, _unused = self.board()
        text = self.plain(board)

        self.assertIn("Announcer", text)
        self.assertIn("Recent", text)
        self.assertIn("Announce on", text)
        self.assertIn("Snooze", text)
        self.assertIn("toast", text)
        self.assertIn("Test voice", text)
        self.assertIn("Full setup", text)

    def test_frame_shows_the_config_path(self):
        board, _unused = self.board()

        self.assertIn("config", self.plain(board))

    def test_empty_log_shows_the_placeholder(self):
        board, _unused = self.board()
        board.entries = []

        self.assertIn("no announcements logged yet", self.plain(board))

    def test_entry_reasons_are_rendered(self):
        board, _unused = self.board()
        board.entries = [entry(action="snoozed", reasons=["quiet-hours"])]

        self.assertIn("quiet-hours", self.plain(board))
        self.assertNotIn("no announcements logged yet", self.plain(board))

    def test_recent_rows_are_newest_first(self):
        board, _unused = self.board()
        board.entries = [entry(action="oldest"), entry(action="newest")]
        lines = [dashboard._plain(line) for line in self.frame(board)]

        self.assertIn("newest", lines[4])
        self.assertIn("oldest", lines[5])

    def test_recent_rows_are_capped_at_recent_lines(self):
        board, _unused = self.board()
        board.entries = [entry(action="a{}".format(i)) for i in range(20)]
        lines = [dashboard._plain(line) for line in self.frame(board)]

        self.assertIn("a19", lines[4])
        self.assertIn("a15", lines[8])
        self.assertNotIn("a14", "\n".join(lines))

    def test_badge_says_live(self):
        board, _unused = self.board()
        board.config["announce"] = ["done"]
        board.snooze_until = 0.0

        self.assertIn("live", dashboard._plain(self.frame(board)[0]))

    def test_badge_says_snoozed_with_the_remaining_time(self):
        board, _unused = self.board()
        board.config["announce"] = ["done"]
        board.snooze_until = NOW + 1800.0
        badge = dashboard._plain(self.frame(board)[0])

        self.assertIn("snoozed · ", badge)
        self.assertIn("30m 00s left", badge)

    def test_badge_says_silent_when_no_state_is_selected(self):
        board, _unused = self.board()
        board.config["announce"] = []
        board.snooze_until = NOW + 1800.0

        self.assertIn(
            "silent · no states selected", dashboard._plain(self.frame(board)[0])
        )

    def test_message_line_is_last_and_empty_by_default(self):
        board, _unused = self.board()
        self.assertEqual(self.frame(board)[-1], "")

        board.message = "reloaded"
        self.assertIn("reloaded", dashboard._plain(self.frame(board)[-1]))

    def test_footer_lists_the_key_map(self):
        board, _unused = self.board()
        footer = dashboard._plain(self.frame(board)[26])

        for fragment in ("j/k or arrows move", "space/enter toggle",
                         "s snooze", "t test", "w wizard", "r reload",
                         "q quit"):
            self.assertIn(fragment, footer)

    def test_footer_shrinks_instead_of_wrapping_on_a_narrow_terminal(self):
        for columns, expected in ((dashboard.POPUP_COLUMNS, dashboard.FOOTER),
                                  (80, dashboard.FOOTER_COMPACT),
                                  (62, dashboard.FOOTER_MINIMAL)):
            with self.subTest(columns=columns):
                self.assertEqual(dashboard.footer_text(columns - 1), expected)
                self.assertLessEqual(len(expected), columns - 1)

    def test_snooze_row_shows_the_active_choice_and_the_time_left(self):
        board, _unused = self.board()
        board.snooze_until = NOW + 1800.0
        row = dashboard._plain(self.frame(board)[10])

        self.assertIn("Snooze", row)
        self.assertIn("30m · 30m 00s left", row)
        self.assertIn("s cycles 5m / 30m / 2h / tomorrow / off", row)

    def test_snooze_row_shows_the_target_for_a_tomorrow_snooze(self):
        board, _unused = self.board()
        board.snooze_until = dashboard.next_morning(NOW)
        row = dashboard._plain(self.frame(board)[10])

        self.assertIn("until {:02d}:00".format(dashboard.SNOOZE_HOUR), row)

    def test_state_rows_show_selection_boxes(self):
        board, _unused = self.board()
        board.config["announce"] = ["done"]
        lines = [dashboard._plain(line) for line in self.frame(board)]

        self.assertIn("◼", lines[13])
        self.assertIn("done", lines[13])
        self.assertIn("◻", lines[14])
        self.assertIn("blocked", lines[14])

    def test_state_rows_follow_state_order(self):
        board, _unused = self.board()
        lines = [dashboard._plain(line) for line in self.frame(board)]

        for position, state in enumerate(dashboard.STATE_ORDER):
            with self.subTest(state=state):
                self.assertIn(state, lines[13 + position])
                self.assertIn(dashboard.STATE_HELP[state], lines[13 + position])

    def test_toast_row_reflects_the_config(self):
        board, _unused = self.board()
        board.config["toast"] = True
        self.assertIn("◼", dashboard._plain(self.frame(board)[19]))

        board.config["toast"] = False
        self.assertIn("◻", dashboard._plain(self.frame(board)[19]))

    def test_capability_strip_marks_present_and_missing_tools(self):
        board, _unused = self.board()
        board.caps = {name: None for name in dashboard.CAPABILITY_NAMES}
        board.caps["codex"] = "/bin/codex"
        strip = dashboard._plain(self.frame(board)[21])

        for name in dashboard.CAPABILITY_NAMES:
            self.assertIn(name, strip)
        self.assertIn("✓", strip)
        self.assertIn("✗", strip)

    def test_render_reads_no_files_and_no_clock(self):
        board, _unused = self.board()
        with mock.patch.object(dashboard, "read_log") as read_log, \
                mock.patch.object(dashboard, "read_snooze") as read_snooze, \
                mock.patch.object(dashboard, "load_config") as load_config, \
                mock.patch.object(dashboard.subprocess, "Popen") as popen, \
                mock.patch.object(dashboard.time, "time") as clock:
            board.render()

        read_log.assert_not_called()
        read_snooze.assert_not_called()
        load_config.assert_not_called()
        popen.assert_not_called()
        clock.assert_not_called()

    def test_render_never_writes_to_the_terminal(self):
        board, fake_io = self.board()

        board.render()

        self.assertEqual(fake_io.written, [])

    def test_refresh_loads_config_log_and_snooze(self):
        self.write_config('announce = ["idle"]\n')
        self.write_log([log_line(action="announced")])
        dashboard.write_snooze(self.state_dir, NOW + 600.0)
        board, _unused = self.board()

        board.refresh()

        self.assertEqual(board.config["announce"], ["idle"])
        self.assertEqual(len(board.entries), 1)
        self.assertEqual(board.snooze_until, NOW + 600.0)

    def test_refresh_polls_the_voice_test(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = 0
        process.stderr = io.StringIO("")
        board.test_process = process

        board.refresh()

        self.assertEqual(board.message, "voice test ok")
        self.assertIsNone(board.test_process)

    def test_refresh_survives_a_missing_state_dir(self):
        board, _unused = make_dashboard(self.tmp / "nowhere")

        board.refresh()

        self.assertEqual(board.entries, [])
        self.assertEqual(board.snooze_until, 0.0)

    def test_draw_paints_through_the_kit_and_tracks_height(self):
        board, fake_io = self.board()

        board.draw()

        self.assertEqual(board.height, dashboard.FRAME_HEIGHT)
        self.assertTrue(fake_io.written)
        self.assertNotIn("\n".join(fake_io.written), ("",))


# ---- key dispatch ----------------------------------------------------------


class KeyDispatchTests(DashboardTestCase):
    def test_owns_terminal_is_false_with_injected_io(self):
        board, _unused = self.board()

        self.assertFalse(board.owns_terminal)

    def test_owns_terminal_stays_false_on_a_tty_when_io_is_injected(self):
        with mock.patch.object(dashboard.kit, "tty_active", return_value=True):
            board, _unused = make_dashboard(self.tmp)

            self.assertFalse(board.owns_terminal)

    def test_owns_terminal_is_true_only_without_injected_io(self):
        with mock.patch.object(dashboard.kit, "tty_active", return_value=True):
            with capture() as (out, err):
                board = dashboard.Dashboard(self.config_dir, self.state_dir)

            self.assertTrue(board.owns_terminal)

    def test_run_uses_raw_mode_when_it_owns_the_terminal(self):
        with mock.patch.object(dashboard.kit, "tty_active", return_value=True), \
                mock.patch.object(dashboard.kit, "read_key",
                                  return_value="q"), \
                mock.patch.object(dashboard.kit, "raw_mode") as raw_mode, \
                mock.patch.object(dashboard.kit, "hide_cursor") as hide, \
                mock.patch.object(dashboard.kit, "show_cursor") as show:
            with capture() as (out, err):
                board = dashboard.Dashboard(
                    self.config_dir, self.state_dir,
                    wait_input=lambda timeout: True,
                )
                code = board.run()

        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(board.owns_terminal)
        raw_mode.assert_called_once()
        hide.assert_called_once()
        show.assert_called_once()

    def test_j_and_down_and_tab_move_forward_and_wrap(self):
        for key in ("j", "down", "\t"):
            with self.subTest(key=key):
                board, _unused = self.board()
                self.assertTrue(board.handle_key(key))
                self.assertEqual(board.index, 1)
                board.index = len(dashboard.ROWS) - 1
                board.handle_key(key)
                self.assertEqual(board.index, 0)

    def test_k_and_up_move_backward_and_wrap(self):
        for key in ("k", "up"):
            with self.subTest(key=key):
                board, _unused = self.board()
                self.assertTrue(board.handle_key(key))
                self.assertEqual(board.index, len(dashboard.ROWS) - 1)
                board.handle_key(key)
                self.assertEqual(board.index, len(dashboard.ROWS) - 2)

    def test_space_and_enter_keys_activate_the_focused_row(self):
        for key in (" ", "\r", "\n"):
            with self.subTest(key=key):
                board, _unused = self.board()
                board.index = 6
                with mock.patch.object(board, "activate") as activate:
                    self.assertTrue(board.handle_key(key))
                activate.assert_called_once_with()

    def test_space_on_a_state_row_persists_the_change(self):
        board, _unused = self.board()
        board.index = 3          # ROWS[3] is the "idle" state row
        self.assertEqual(dashboard.ROWS[3].key, "idle")

        board.handle_key(" ")

        self.assertIn("idle", dashboard.announce_states(board.config))
        self.assertIn("idle", announce.load_config(self.config_dir)["announce"])
        self.assertIn("announce: ", board.message)

        board.handle_key(" ")

        self.assertNotIn("idle", dashboard.announce_states(board.config))
        self.assertNotIn(
            "idle", announce.load_config(self.config_dir)["announce"]
        )

    def test_toggled_states_are_written_in_state_order(self):
        board, _unused = self.board()
        board.config["announce"] = ["blocked"]
        board.index = 1          # "done"

        board.handle_key(" ")

        self.assertEqual(
            announce.load_config(self.config_dir)["announce"],
            ["done", "blocked"],
        )

    def test_toggle_state_returns_the_new_membership(self):
        board, _unused = self.board()

        self.assertFalse(board.toggle_state("done"))
        self.assertTrue(board.toggle_state("done"))

    def test_turning_off_the_last_state_is_allowed(self):
        self.write_config('announce = ["done"]\n')
        board, _unused = self.board()
        board.refresh()
        board.index = 1          # "done"

        board.handle_key(" ")

        self.assertEqual(announce.load_config(self.config_dir)["announce"], [])
        self.assertIn("announce: (none)", board.message)

    def test_space_on_the_toast_row_round_trips(self):
        board, _unused = self.board()
        board.index = 6
        self.assertEqual(dashboard.ROWS[6].kind, "toast")

        board.handle_key(" ")

        self.assertTrue(announce.load_config(self.config_dir)["toast"])
        self.assertEqual(board.message, "toast on")

        board.handle_key(" ")

        self.assertFalse(announce.load_config(self.config_dir)["toast"])
        self.assertEqual(board.message, "toast off")

    def test_toggle_toast_returns_the_new_value(self):
        board, _unused = self.board()

        self.assertTrue(board.toggle_toast())
        self.assertFalse(board.toggle_toast())

    def test_s_cycles_all_five_snooze_options(self):
        board, _unused = self.board()

        board.handle_key("s")
        self.assertEqual(board.snooze_until, NOW + 300.0)
        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), NOW + 300.0)
        self.assertIn("snoozed · ", board.message)

        board.handle_key("s")
        self.assertEqual(board.snooze_until, NOW + 1800.0)
        self.assertEqual(
            dashboard.read_snooze(self.state_dir, NOW), NOW + 1800.0
        )

        board.handle_key("s")
        self.assertEqual(board.snooze_until, NOW + 7200.0)
        self.assertEqual(
            dashboard.read_snooze(self.state_dir, NOW), NOW + 7200.0
        )

        board.handle_key("s")
        self.assertEqual(board.snooze_until, dashboard.next_morning(NOW))
        self.assertEqual(
            dashboard.read_snooze(self.state_dir, NOW),
            dashboard.next_morning(NOW),
        )
        self.assertIn("snoozed until ", board.message)

        board.handle_key("s")
        self.assertEqual(board.snooze_until, 0.0)
        self.assertEqual(dashboard.read_snooze(self.state_dir, NOW), 0.0)
        self.assertEqual(board.message, "snooze off")

        board.handle_key("s")
        self.assertEqual(board.snooze_until, NOW + 300.0)

    def test_activating_the_snooze_row_matches_the_s_key(self):
        board, _unused = self.board()
        board.index = 0
        self.assertEqual(dashboard.ROWS[0].kind, "snooze")

        board.handle_key(" ")

        self.assertEqual(board.snooze_until, NOW + 300.0)
        self.assertIn("snoozed · ", board.message)

    def test_t_spawns_the_voice_test_with_both_plugin_dirs(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(dashboard.subprocess, "Popen",
                               return_value=process) as popen:
            self.assertTrue(board.handle_key("t"))

        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(
            command,
            [dashboard.PYTHON, str(dashboard.SCRIPT_DIR / "announce.py"),
             "--test"],
        )
        kwargs = popen.call_args.kwargs
        self.assertEqual(kwargs["cwd"], str(dashboard.SCRIPT_DIR))
        self.assertEqual(
            kwargs["env"]["HERDR_PLUGIN_CONFIG_DIR"], str(self.config_dir)
        )
        self.assertEqual(
            kwargs["env"]["HERDR_PLUGIN_STATE_DIR"], str(self.state_dir)
        )
        self.assertIs(kwargs["stdin"], dashboard.subprocess.DEVNULL)
        # nobody reads the child's stdout; a PIPE nobody drains can block it
        self.assertIs(kwargs["stdout"], dashboard.subprocess.DEVNULL)
        self.assertIs(kwargs["stderr"], dashboard.subprocess.PIPE)
        self.assertIs(board.test_process, process)
        self.assertEqual(board.message, "voice test running…")

    def test_second_t_while_running_does_not_spawn_again(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = None
        with mock.patch.object(dashboard.subprocess, "Popen",
                               return_value=process) as popen:
            board.handle_key("t")
            board.handle_key("t")

        self.assertEqual(popen.call_count, 1)
        self.assertEqual(board.message, "voice test already running")

    def test_voice_test_spawn_failure_is_reported(self):
        board, _unused = self.board()
        with mock.patch.object(dashboard.subprocess, "Popen",
                               side_effect=OSError("no python")):
            self.assertIsNone(board.start_voice_test())

        self.assertIsNone(board.test_process)
        self.assertIn("voice test failed", board.message)
        self.assertIn("no python", board.message)

    def test_poll_voice_test_reports_success(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = 0
        process.stderr = io.StringIO("")
        board.test_process = process

        board.poll_voice_test()

        self.assertEqual(board.message, "voice test ok")
        self.assertIsNone(board.test_process)

    def test_poll_voice_test_reports_the_last_stderr_line(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = 1
        process.stderr = io.StringIO("Traceback...\nannouncer error: no voice\n")
        board.test_process = process

        board.poll_voice_test()

        self.assertIn("voice test failed", board.message)
        self.assertIn("announcer error: no voice", board.message)
        self.assertIsNone(board.test_process)

    def test_poll_voice_test_falls_back_to_the_exit_code(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = 3
        process.stderr = io.StringIO("   \n")
        board.test_process = process

        board.poll_voice_test()

        self.assertIn("exit 3", board.message)

    def test_poll_voice_test_leaves_a_running_process_alone(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = None
        board.test_process = process
        board.message = "voice test running…"

        board.poll_voice_test()

        self.assertIs(board.test_process, process)
        self.assertEqual(board.message, "voice test running…")

    def test_poll_voice_test_without_a_process_is_a_no_op(self):
        board, _unused = self.board()

        board.poll_voice_test()

        self.assertIsNone(board.test_process)
        self.assertEqual(board.message, "")

    def test_w_requests_the_wizard_and_leaves_the_loop(self):
        board, _unused = self.board()
        with mock.patch.object(dashboard.os, "execvpe") as execvpe:
            self.assertFalse(board.handle_key("w"))

        execvpe.assert_not_called()
        self.assertTrue(board.wizard_requested)
        self.assertFalse(board.running)

    def test_exec_wizard_is_a_no_op_without_the_terminal(self):
        board, _unused = self.board()
        with mock.patch.object(dashboard.os, "execvpe") as execvpe, \
                mock.patch.object(dashboard.os, "chdir") as chdir:
            self.assertEqual(board.exec_wizard(), 0)

        execvpe.assert_not_called()
        chdir.assert_not_called()

    def test_run_returns_zero_after_a_wizard_request(self):
        board, fake_io = self.board(keys=["w"])
        with mock.patch.object(dashboard.os, "execvpe") as execvpe:
            self.assertEqual(board.run(), 0)

        execvpe.assert_not_called()
        self.assertTrue(board.wizard_requested)

    def test_r_reloads_from_disk(self):
        board, _unused = self.board()
        self.write_config('announce = ["working"]\n')

        self.assertTrue(board.handle_key("r"))

        self.assertEqual(board.config["announce"], ["working"])
        self.assertEqual(board.message, "reloaded")

    def test_poll_voice_test_closes_the_child_pipes(self):
        board, _unused = self.board()
        process = mock.Mock()
        process.poll.return_value = 0
        process.stderr = io.StringIO("")
        process.stdout = None
        board.test_process = process

        board.poll_voice_test()

        self.assertTrue(process.stderr.closed)

    def test_quit_keys_stop_the_loop(self):
        for key in ("q", "\x03", "\x04"):
            with self.subTest(key=key):
                board, _unused = self.board()
                self.assertFalse(board.handle_key(key))

    def test_esc_does_not_quit(self):
        # kit._read_key returns "esc" for EVERY escape sequence it does not
        # recognise - Left/Right, Home/End, mouse and scroll reports - so a
        # quit on "esc" closed the popup at random
        board, _unused = self.board()
        board.index = 2

        self.assertTrue(board.handle_key("esc"))

        self.assertTrue(board.running)
        self.assertEqual(board.index, 2)
        self.assertEqual(board.message, "")

    def test_esc_keeps_the_loop_running(self):
        board, fake_io = self.board(keys=["esc", "esc", "j", "q"])

        self.assertEqual(board.run(), 0)

        self.assertEqual(board.index, 1)
        self.assertEqual(fake_io.keys, [])

    def test_unknown_keys_are_ignored(self):
        board, _unused = self.board()
        board.index = 2

        self.assertTrue(board.handle_key("z"))
        self.assertTrue(board.handle_key(""))
        self.assertTrue(board.handle_key("\x1b"))
        self.assertEqual(board.index, 2)
        self.assertEqual(board.message, "")

    def test_run_terminates_and_paints(self):
        board, fake_io = self.board(keys=["j", "j", "q"])

        self.assertEqual(board.run(), 0)

        self.assertTrue(fake_io.written)
        self.assertEqual(board.index, 2)
        self.assertFalse(board.running)

    def test_run_quits_on_ctrl_c_without_raising(self):
        board, _unused = self.board(keys=["\x03"])

        self.assertEqual(board.run(), 0)

    def test_run_never_touches_terminal_modes_with_injected_io(self):
        board, _unused = self.board(keys=["q"])
        with mock.patch.object(dashboard.kit, "raw_mode") as raw_mode, \
                mock.patch.object(dashboard.kit, "hide_cursor") as hide, \
                mock.patch.object(dashboard.kit, "show_cursor") as show:
            board.run()

        raw_mode.assert_not_called()
        hide.assert_not_called()
        show.assert_not_called()

    def test_run_ticks_without_a_key_and_never_sleeps(self):
        board, fake_io = self.board(keys=["q"])
        waits = []

        def wait_input(timeout):
            waits.append(timeout)
            return len(waits) > 1

        board.wait_input = wait_input
        with mock.patch.object(dashboard.time, "sleep") as sleep:
            self.assertEqual(board.run(), 0)

        sleep.assert_not_called()
        self.assertEqual(waits[0], dashboard.TICK_SECONDS)
        self.assertGreaterEqual(len(waits), 2)

    def pipe_stdin(self, payload=b""):
        """Replace sys.stdin with a real pipe; no sleeps, no terminal."""
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "r")
        self.addCleanup(stream.close)
        if payload:
            os.write(write_fd, payload)
        self.addCleanup(os.close, write_fd)
        patcher = mock.patch.object(sys, "stdin", stream)
        patcher.start()
        self.addCleanup(patcher.stop)
        return stream

    def test_stdin_ready_reports_a_waiting_key(self):
        self.pipe_stdin(b"j")

        self.assertTrue(dashboard.stdin_ready(0.0))

    def test_stdin_ready_is_false_when_nothing_is_waiting(self):
        self.pipe_stdin()

        self.assertFalse(dashboard.stdin_ready(0.0))

    def test_stdin_ready_falls_back_to_true_on_an_unselectable_stdin(self):
        stream = self.pipe_stdin()
        stream.close()

        self.assertTrue(dashboard.stdin_ready(0.0))


# ---- command line ----------------------------------------------------------


class SubcommandTests(DashboardTestCase):
    def test_snooze_thirty_minutes(self):
        with capture() as (out, err):
            code = dashboard.main(["snooze", "30m"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("snoozed until ", out.getvalue())
        self.assertTrue(dashboard.snooze_active(self.state_dir))

    def test_snooze_two_hours_mentions_the_spec(self):
        with capture() as (out, err):
            code = dashboard.main(["snooze", "2h"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("2h", out.getvalue())

    def test_snooze_tomorrow(self):
        with capture() as (out, err):
            code = dashboard.main(["snooze", "tomorrow"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("tomorrow", out.getvalue())
        until = dashboard.read_snooze(self.state_dir)
        self.assertEqual(until, dashboard.next_morning())
        self.assertEqual(dashboard.snooze_step(until), "tomorrow")

    def test_snooze_five_minutes(self):
        with capture() as (out, err):
            code = dashboard.main(["snooze", "5m"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("snoozed until ", out.getvalue())
        self.assertTrue(dashboard.snooze_active(self.state_dir))

    def test_snooze_off(self):
        dashboard.set_snooze(self.state_dir, "30m")
        with capture() as (out, err):
            code = dashboard.main(["snooze", "off"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("snooze off", out.getvalue())
        self.assertEqual(dashboard.read_snooze(self.state_dir), 0.0)

    def test_snooze_with_a_bad_spec_writes_nothing(self):
        with capture() as (out, err):
            code = dashboard.main(["snooze", "banana"])

        self.assertEqual(code, 2)
        self.assertFalse(dashboard.snooze_path(self.state_dir).exists())
        self.assertTrue(err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_snooze_without_a_spec_is_a_usage_error(self):
        with capture() as (out, err):
            code = dashboard.main(["snooze"])

        self.assertEqual(code, 2)
        self.assertIn("usage", err.getvalue())

    def test_toggle_toast_round_trips_on_disk(self):
        with capture() as (out, err):
            first = dashboard.main(["toggle-toast"])
        self.assertEqual(first, 0, err.getvalue())
        self.assertIn("toast on", out.getvalue())
        self.assertTrue(announce.load_config(self.config_dir)["toast"])

        with capture() as (out, err):
            second = dashboard.main(["toggle-toast"])
        self.assertEqual(second, 0, err.getvalue())
        self.assertIn("toast off", out.getvalue())
        self.assertFalse(announce.load_config(self.config_dir)["toast"])

    def test_open_uses_the_herdr_bin_path(self):
        completed = mock.Mock()
        completed.returncode = 0
        with mock.patch.dict(os.environ, {"HERDR_BIN_PATH": "/opt/bin/herdr"}), \
                mock.patch.object(dashboard.subprocess, "run",
                                  return_value=completed) as run:
            with capture() as (out, err):
                code = dashboard.main(["open"])

        self.assertEqual(code, 0, err.getvalue())
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/opt/bin/herdr")
        self.assertEqual(command[1:4], ["plugin", "pane", "open"])
        self.assertIn("--plugin", command)
        self.assertEqual(command[command.index("--plugin") + 1],
                         dashboard.PLUGIN_ID)
        self.assertIn("--entrypoint", command)
        self.assertEqual(command[command.index("--entrypoint") + 1], "dashboard")

    def test_open_falls_back_to_bare_herdr(self):
        completed = mock.Mock()
        completed.returncode = 0
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HERDR_BIN_PATH", None)
            with mock.patch.object(dashboard.subprocess, "run",
                                   return_value=completed) as run:
                with capture() as (out, err):
                    code = dashboard.main(["open"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(run.call_args.args[0][0], "herdr")

    def test_open_returns_the_child_return_code(self):
        completed = mock.Mock()
        completed.returncode = 7
        with mock.patch.object(dashboard.subprocess, "run",
                               return_value=completed):
            with capture() as (out, err):
                code = dashboard.main(["open"])

        self.assertEqual(code, 7)

    def test_open_reports_a_missing_binary(self):
        with mock.patch.object(dashboard.subprocess, "run",
                               side_effect=OSError("no herdr")):
            with capture() as (out, err):
                code = dashboard.main(["open"])

        self.assertEqual(code, 1)
        self.assertTrue(err.getvalue())

    def test_unknown_subcommand_prints_usage(self):
        with capture() as (out, err):
            code = dashboard.main(["bogus"])

        self.assertEqual(code, 2)
        self.assertIn("usage", err.getvalue())
        self.assertEqual(out.getvalue(), "")

    def test_no_argv_without_a_tty_writes_a_plain_snapshot(self):
        self.write_log([log_line(action="announced")])
        with mock.patch.object(dashboard.kit, "tty_active", return_value=False), \
                mock.patch.object(dashboard.Dashboard, "run") as run:
            with capture() as (out, err):
                code = dashboard.main([])

        self.assertEqual(code, 0, err.getvalue())
        run.assert_not_called()
        text = out.getvalue()
        self.assertIn("Announcer", text)
        self.assertNotIn("\x1b[", text)
        self.assertTrue(text.endswith("\n"))

    def test_no_argv_with_a_tty_runs_the_dashboard(self):
        with mock.patch.object(dashboard.kit, "tty_active", return_value=True), \
                mock.patch.object(dashboard.Dashboard, "run",
                                  return_value=0) as run:
            with capture() as (out, err):
                code = dashboard.main([])

        self.assertEqual(code, 0, err.getvalue())
        run.assert_called_once()

    def test_snapshot_is_plain_text_and_reads_the_log(self):
        self.write_log([log_line(action="announced", pane_id="pane-9")])

        text = dashboard.snapshot(self.config_dir, self.state_dir)

        self.assertNotIn("\x1b[", text)
        self.assertIn("Announcer", text)
        self.assertIn("pane-9", text)
        self.assertTrue(text.endswith("\n"))

    def test_snapshot_survives_an_infinite_snooze_deadline(self):
        dashboard.snooze_path(self.state_dir).write_text(
            '{"until": 1e999}', encoding="utf-8"
        )

        text = dashboard.snapshot(self.config_dir, self.state_dir)

        self.assertIn("off", text)
        self.assertTrue(text.endswith("\n"))

    def test_main_creates_the_state_dir(self):
        missing = self.tmp / "missing-state"
        with mock.patch.dict(os.environ,
                             {"HERDR_PLUGIN_STATE_DIR": str(missing)}):
            with capture() as (out, err):
                code = dashboard.main(["snooze", "off"])

        self.assertEqual(code, 0, err.getvalue())
        self.assertTrue(missing.is_dir())

    def test_main_reports_errors_instead_of_raising(self):
        with mock.patch.object(dashboard, "set_snooze",
                               side_effect=RuntimeError("boom")):
            with capture() as (out, err):
                code = dashboard.main(["snooze", "30m"])

        self.assertEqual(code, 1)
        self.assertIn("dashboard error", err.getvalue())

    def test_usage_string_lists_every_subcommand_and_snooze_option(self):
        for fragment in ("snooze", "toggle-toast", "open",
                         "5m", "30m", "2h", "tomorrow", "off"):
            self.assertIn(fragment, dashboard.USAGE)

    def test_snapshot_renders_full_size_on_a_tiny_terminal(self):
        with terminal_size(40, 10):
            text = dashboard.snapshot(self.config_dir, self.state_dir)

        self.assertIn("Announcer", text)
        self.assertNotIn("too small", text)
        self.assertEqual(len(text.rstrip("\n").split("\n")),
                         dashboard.FRAME_HEIGHT - 1)   # the message line is bare


# ---- config round trips and directory resolution ---------------------------


class ConfigRoundTripTests(DashboardTestCase):
    def config_text(self):
        return (self.config_dir / "config.toml").read_text(encoding="utf-8")

    def test_write_config_keys_creates_a_missing_file(self):
        merged = dashboard.write_config_keys(self.config_dir, {"toast": True})

        self.assertTrue((self.config_dir / "config.toml").exists())
        self.assertTrue(merged["toast"])
        self.assertEqual(merged["announce"], announce.DEFAULTS["announce"])
        text = self.config_text()
        self.assertIn("toast = true", text)
        self.assertNotIn("summary =", text)
        self.assertNotIn("announce =", text)

    def test_write_config_keys_returns_defaults_merged_with_disk(self):
        self.write_config('voice = "Alex"\n')

        merged = dashboard.write_config_keys(self.config_dir, {"toast": True})

        self.assertEqual(merged["voice"], "Alex")
        self.assertTrue(merged["toast"])
        self.assertEqual(merged["summary"], announce.DEFAULTS["summary"])

    def test_unknown_keys_survive_a_write(self):
        self.write_config("future_thing = 1\n")

        dashboard.write_config_keys(self.config_dir, {"toast": True})

        self.assertIn("future_thing = 1", self.config_text())

    def test_a_key_written_at_its_default_value_survives(self):
        self.write_config("toast = false\n")

        dashboard.write_config_keys(self.config_dir, {"voice": "Alex"})

        text = self.config_text()
        self.assertIn("toast = false", text)
        self.assertIn('voice = "Alex"', text)
        self.assertFalse(announce.load_config(self.config_dir)["toast"])

    def test_second_write_leaves_a_backup(self):
        dashboard.write_config_keys(self.config_dir, {"toast": True})
        self.assertFalse((self.config_dir / "config.toml.bak").exists())

        dashboard.write_config_keys(self.config_dir, {"toast": False})

        self.assertTrue((self.config_dir / "config.toml.bak").exists())

    def test_empty_announce_list_persists(self):
        dashboard.write_config_keys(self.config_dir, {"announce": []})

        self.assertIn("announce = []", self.config_text())
        self.assertEqual(announce.load_config(self.config_dir)["announce"], [])

    def test_write_config_keys_goes_through_the_kit(self):
        with mock.patch.object(config_io, "write_config") as write_config:
            dashboard.write_config_keys(self.config_dir, {"toast": True})

        write_config.assert_called_once()
        path, config, chosen = write_config.call_args.args
        self.assertEqual(path, dashboard.config_path(self.config_dir))
        self.assertTrue(config["toast"])
        self.assertIn("toast", chosen)

    def test_config_path_is_config_dir_config_toml(self):
        self.assertEqual(
            dashboard.config_path(self.config_dir),
            self.config_dir / "config.toml",
        )

    def test_resolve_dirs_prefers_the_environment(self):
        config_dir, state_dir = dashboard.resolve_dirs()

        self.assertEqual(config_dir, Path(str(self.config_dir)))
        self.assertEqual(state_dir, Path(str(self.state_dir)))

    def test_resolve_dirs_uses_the_core_resolver_without_env(self):
        fallback = (self.tmp / "fallback-config", self.tmp / "fallback-state")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HERDR_PLUGIN_CONFIG_DIR", None)
            os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
            with mock.patch.object(dashboard, "_resolve_dirs_without_env",
                                   return_value=fallback) as resolver:
                result = dashboard.resolve_dirs()

        self.assertEqual(result, fallback)
        resolver.assert_called_once()

    def test_resolve_dirs_fills_in_only_the_missing_side(self):
        fallback = (self.tmp / "fallback-config", self.tmp / "fallback-state")
        with mock.patch.dict(os.environ, {}):
            os.environ.pop("HERDR_PLUGIN_STATE_DIR", None)
            with mock.patch.object(dashboard, "_resolve_dirs_without_env",
                                   return_value=fallback):
                config_dir, state_dir = dashboard.resolve_dirs()

        self.assertEqual(config_dir, Path(str(self.config_dir)))
        self.assertEqual(state_dir, fallback[1])

    def test_resolve_dirs_ignores_empty_environment_values(self):
        fallback = (self.tmp / "fallback-config", self.tmp / "fallback-state")
        with mock.patch.dict(os.environ, {"HERDR_PLUGIN_CONFIG_DIR": ""}), \
                mock.patch.object(dashboard, "_resolve_dirs_without_env",
                                  return_value=fallback):
            config_dir, state_dir = dashboard.resolve_dirs()

        self.assertEqual(config_dir, fallback[0])
        self.assertEqual(state_dir, Path(str(self.state_dir)))

    def test_local_resolve_dirs_uses_the_documented_literals(self):
        config_dir, state_dir = dashboard._local_resolve_dirs()

        self.assertEqual(
            config_dir,
            Path.home() / ".config" / "herdr" / "plugins" / "config"
            / dashboard.PLUGIN_ID,
        )
        self.assertEqual(
            state_dir,
            Path.home() / ".local" / "state" / "herdr" / "plugins"
            / dashboard.PLUGIN_ID,
        )

    def test_local_resolve_dirs_runs_no_subprocess(self):
        with mock.patch.object(dashboard.subprocess, "run") as run, \
                mock.patch.object(dashboard.subprocess, "Popen") as popen:
            dashboard._local_resolve_dirs()

        run.assert_not_called()
        popen.assert_not_called()


# ---- suite invariants ------------------------------------------------------


class SuiteInvariantTests(unittest.TestCase):
    def test_root_dashboard_is_the_package_implementation(self):
        self.assertEqual(dashboard.__name__, "announcer.dashboard")

    def test_tui_private_names_alias_the_public_api(self):
        pairs = (
            ("_frame", "frame"),
            ("_read_key", "read_key"),
            ("_raw_mode", "raw_mode"),
            ("_c", "colorize"),
            ("_tty_active", "tty_active"),
            ("_hide_cursor", "hide_cursor"),
            ("_show_cursor", "show_cursor"),
            ("_collapse", "collapse"),
        )
        for private, public in pairs:
            with self.subTest(private=private):
                self.assertIs(
                    getattr(dashboard.kit, private),
                    getattr(dashboard.kit, public),
                )

    def test_announce_never_imports_dashboard(self):
        saved = sys.modules.pop("dashboard", None)
        try:
            importlib.reload(announce)
            self.assertNotIn("dashboard", sys.modules)
        finally:
            if saved is not None:
                sys.modules["dashboard"] = saved

    def test_frame_height_and_width_constants(self):
        self.assertEqual(dashboard.FRAME_HEIGHT, 28)
        self.assertEqual(dashboard.WIDTH, 86)
        self.assertEqual(dashboard.RECENT_LINES, 5)
        self.assertEqual(len(dashboard.ROWS), 9)


# ---- regressions -----------------------------------------------------------


class RegressionTests(DashboardTestCase):
    """One case per defect found in review; each fails on the old code."""

    def test_space_on_the_wizard_row_leaves_the_loop(self):
        board, _unused = self.board()
        board.index = len(dashboard.ROWS) - 1

        keep_going = board.handle_key(" ")

        self.assertFalse(keep_going)
        self.assertFalse(board.running)
        self.assertTrue(board.wizard_requested)

    def test_space_on_the_wizard_row_ends_run(self):
        board, _unused = self.board(keys=[" "])
        board.index = len(dashboard.ROWS) - 1

        self.assertEqual(board.run(), 0)
        self.assertTrue(board.wizard_requested)

    def test_space_on_an_ordinary_row_keeps_looping(self):
        board, _unused = self.board()
        board.index = 6

        self.assertTrue(board.handle_key(" "))
        self.assertTrue(board.running)

    def test_reason_parts_may_contain_spaces(self):
        parsed = dashboard.parse_log_line(
            log_line(reasons="playback-lock: timeout;play: mpv/ffplay missing")
        )

        self.assertEqual(
            parsed.reasons,
            ["playback-lock: timeout", "play: mpv/ffplay missing"],
        )
        self.assertEqual(parsed.elapsed, 0.421)
        self.assertEqual(parsed.pane_id, "pane-1")

    def test_padded_columns_keep_their_alignment(self):
        board, _unused = self.board()
        board.entries = [entry(pane_id="p1"), entry(pane_id="pane-abcdef")]
        lines = [dashboard._plain(line) for line in board.render()]

        self.assertEqual(lines[4].index("announced"), lines[5].index("announced"))
        helps = [
            lines[13 + position].index(dashboard.STATE_HELP[state])
            for position, state in enumerate(dashboard.STATE_ORDER)
        ]
        self.assertEqual(len(set(helps)), 1)

    def test_clip_keeps_interior_spacing_and_width(self):
        self.assertEqual(dashboard._clip("a   b", 40), "a   b")
        self.assertEqual(dashboard._clip("a\nb\tc", 40), "a b c")
        self.assertLessEqual(len(dashboard._clip("x" * 200, 12)), 12)

    def test_padded_columns_line_up_in_a_rendered_frame(self):
        board, _unused = self.board()
        board.entries = [
            entry(pane_id="p1", status="done", timestamp="2026-08-17T09:00:01"),
            entry(pane_id="pane-abcdef", status="blocked",
                  timestamp="2026-08-17T12:04:11+02:00", action="snoozed"),
        ]
        lines = [dashboard._plain(line) for line in board.render()]

        # the status column starts at the same offset on both recent rows
        self.assertEqual(lines[4].index("blocked"), lines[5].index("done"))
        self.assertEqual(lines[4].index("pane-abcdef"), lines[5].index("p1"))
        starts = [
            lines[13 + position].index(dashboard.STATE_HELP[state])
            for position, state in enumerate(dashboard.STATE_ORDER)
        ]
        self.assertEqual(len(set(starts)), 1)

    def test_corrupt_config_does_not_break_refresh_or_render(self):
        self.write_config('announce = [\n')
        board, _unused = self.board()

        board.refresh()

        # The frame must survive regardless of parser. The message depends on
        # it: tomllib (3.11+) raises on the corrupt line, so the dashboard
        # reports the config unreadable; the tiny-TOML fallback on older
        # interpreters deliberately skips bad lines instead of raising (so a
        # corrupt config can never mute announcements), and then there is no
        # unreadable condition to report.
        self.assertEqual(len(board.render()), dashboard.FRAME_HEIGHT)
        try:
            import tomllib  # noqa: F401
        except ImportError:
            self.assertEqual(board.message, "")
        else:
            self.assertEqual(board.message, dashboard.CONFIG_UNREADABLE)

    def test_config_writes_print_nothing_to_stdout(self):
        original = config_io.write_config

        def noisy(path, config, chosen):
            print("Note: wizard writes do not preserve comments")
            return original(path, config, chosen)

        board, fake_io = self.board()
        with mock.patch.object(config_io, "write_config", noisy):
            with capture() as (out, _err):
                board.toggle_toast()

        self.assertEqual(out.getvalue(), "")
        self.assertEqual(fake_io.written, [])

    def test_a_failed_config_write_is_reported_not_raised(self):
        board, _unused = self.board()
        board.index = 1
        with mock.patch.object(config_io, "write_config",
                               side_effect=OSError("read-only file system")):
            board.activate()

        self.assertIn("could not save config: ", board.message)
        self.assertIn("read-only file system", board.message)
        self.assertTrue(board.running)

    def test_a_failed_state_write_leaves_the_config_untouched(self):
        self.write_config('announce = ["done"]\n')
        board, _unused = self.board()
        board.refresh()
        board.index = 3          # the "idle" state row
        with mock.patch.object(config_io, "write_config",
                               side_effect=OSError("read-only file system")):
            board.activate()

        self.assertEqual(dashboard.announce_states(board.config), ["done"])
        self.assertEqual(announce.load_config(self.config_dir)["announce"],
                         ["done"])
        self.assertIn("could not save config: ", board.message)
        self.assertNotIn("announce: ", board.message)
        self.assertEqual(len(board.render()), board.frame_height)

    def test_a_failed_toast_write_leaves_the_config_untouched(self):
        board, _unused = self.board()
        board.index = 6
        with mock.patch.object(config_io, "write_config",
                               side_effect=ValueError("bad toml")):
            board.activate()

        self.assertFalse(bool(board.config.get("toast")))
        self.assertIn("could not save config: ", board.message)
        self.assertNotIn("toast on", board.message)

    def test_terminal_fits_only_above_the_minimum(self):
        for columns, lines, expected in (
            (dashboard.POPUP_COLUMNS, dashboard.POPUP_LINES, True),
            (80, 24, True),
            (dashboard.MIN_WIDTH, dashboard.MIN_HEIGHT, True),
            (59, 24, False),
            (80, 15, False),
        ):
            with self.subTest(columns=columns, lines=lines):
                with terminal_size(columns, lines):
                    self.assertIs(dashboard.terminal_fits(), expected)

    def test_terminal_fits_when_the_size_cannot_be_measured(self):
        # shutil falls back to the popup geometry, which always fits
        with mock.patch.object(dashboard.shutil, "get_terminal_size",
                               side_effect=lambda fallback: os.terminal_size(
                                   fallback)):
            self.assertTrue(dashboard.terminal_fits())
            self.assertEqual(
                dashboard.measure_terminal(),
                (dashboard.POPUP_COLUMNS, dashboard.POPUP_LINES),
            )

    def test_the_frame_is_clamped_to_a_small_terminal(self):
        with terminal_size(80, 24):
            board, _unused = self.board()
        board.entries = [entry(action="a{}".format(index)) for index in range(5)]

        lines = board.render()

        self.assertEqual(board.width, 78)
        self.assertEqual(board.frame_height, 23)
        self.assertEqual(len(lines), 23)
        widest = max(len(dashboard._plain(line)) for line in lines)
        self.assertLessEqual(widest, 79)      # nothing can wrap in 80 columns

    def test_the_frame_never_exceeds_the_terminal_width(self):
        for columns, lines in ((80, 24), (72, 20), (60, 16)):
            with self.subTest(columns=columns):
                with terminal_size(columns, lines):
                    board, _unused = self.board()
                board.entries = [entry(pane_id="pane-abcdefghijkl")]
                board.message = "x" * 200
                frame = board.render()

                self.assertEqual(len(frame), board.frame_height)
                self.assertLessEqual(len(frame), lines - 1)
                widest = max(len(dashboard._plain(line)) for line in frame)
                self.assertLess(widest, columns)

    def test_a_clamped_frame_keeps_the_controls(self):
        with terminal_size(80, 24):
            board, _unused = self.board()
        text = "\n".join(dashboard._plain(line) for line in board.render())

        for fragment in ("Announcer", "Snooze", "Announce on", "toast",
                         "Test voice", "Full setup", "q quit"):
            self.assertIn(fragment, text)

    def test_a_terminal_below_the_minimum_gets_one_plain_line(self):
        with terminal_size(40, 10):
            board, _unused = self.board()

        lines = board.render()

        self.assertEqual(len(lines), 1)
        self.assertNotIn("\x1b[", lines[0])
        self.assertIn("too small", lines[0])
        self.assertIn("60x16", lines[0])

    def test_r_re_measures_the_terminal(self):
        with terminal_size(dashboard.POPUP_COLUMNS, dashboard.POPUP_LINES):
            board, _unused = self.board()
        self.assertEqual(board.frame_height, dashboard.FRAME_HEIGHT)

        with terminal_size(80, 24):
            board.handle_key("r")

        self.assertEqual(board.width, 78)
        self.assertEqual(board.frame_height, 23)
        self.assertEqual(len(board.render()), 23)

    def test_a_message_ages_out_of_the_frame(self):
        clock = [NOW]
        board, _unused = self.board()
        board.now = lambda: clock[0]
        board.message = "reloaded"

        self.assertIn("reloaded", dashboard._plain(board.render()[-1]))

        clock[0] = NOW + dashboard.MESSAGE_SECONDS + 0.1
        self.assertEqual(board.render()[-1], "")
        self.assertEqual(board.message, "")

    def test_navigation_clears_the_message_at_once(self):
        for key in ("j", "k", "up", "down", "\t"):
            with self.subTest(key=key):
                board, _unused = self.board()
                board.message = "voice test ok"

                board.handle_key(key)

                self.assertEqual(board.message, "")

    def test_the_wizard_handoff_erases_the_frame_and_says_so(self):
        board, fake_io = self.board(keys=["w"])

        self.assertEqual(board.run(), 0)

        painted = "".join(fake_io.written)
        self.assertIn("opening the setup wizard", dashboard._plain(painted))
        # the frame was collapsed, not left on screen
        self.assertEqual(board.height, 1)
        self.assertTrue(board.wizard_requested)

    def test_a_too_small_terminal_falls_back_to_the_snapshot(self):
        with mock.patch.object(dashboard.kit, "tty_active", return_value=True), \
                mock.patch.object(dashboard, "terminal_fits",
                                  return_value=False), \
                mock.patch.object(dashboard.Dashboard, "run") as run:
            with capture() as (out, err):
                code = dashboard.main([])

        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("Announcer", out.getvalue())
        self.assertIn("static snapshot", err.getvalue())

    def test_a_large_enough_terminal_runs_the_dashboard(self):
        with mock.patch.object(dashboard.kit, "tty_active", return_value=True), \
                mock.patch.object(dashboard, "terminal_fits",
                                  return_value=True), \
                mock.patch.object(dashboard.Dashboard, "run",
                                  return_value=0) as run:
            with capture() as (_out, _err):
                code = dashboard.main([])

        self.assertEqual(code, 0)
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
