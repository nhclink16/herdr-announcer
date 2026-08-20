import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import announce
from announcer import speech


class CliRoutingTests(unittest.TestCase):
    def environment(self, config_dir, state_dir):
        return {
            "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
            "HERDR_PLUGIN_STATE_DIR": str(state_dir),
        }

    def test_setup_subcommand_routes_to_wizard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(
                os.environ, self.environment(root / "config", root / "state"), clear=False
            ):
                with mock.patch.object(announce, "run_setup", return_value=17) as setup:
                    result = announce.main(["setup"])

        self.assertEqual(result, 17)
        setup.assert_called_once_with(root / "config", root / "state")

    def test_status_subcommand_routes_to_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(
                os.environ, self.environment(root / "config", root / "state"), clear=False
            ):
                with mock.patch.object(announce, "show_status", return_value=0) as status:
                    self.assertEqual(announce.main(["status"]), 0)

        status.assert_called_once_with(root / "config", root / "state")

    def test_no_args_with_tty_prints_usage_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdin = mock.Mock()
            stdin.isatty.return_value = True
            stderr = io.StringIO()
            old_cwd = Path.cwd()
            os.chdir(root)
            try:
                with mock.patch.object(announce.sys, "stdin", stdin), mock.patch.object(
                    announce.sys, "stderr", stderr
                ), mock.patch.dict(
                    os.environ,
                    {
                        "HERDR_PLUGIN_CONFIG_DIR": "",
                        "HERDR_PLUGIN_STATE_DIR": "",
                    },
                    clear=False,
                ):
                    result = announce.main([])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(list(root.iterdir()), [])

        self.assertEqual(result, 2)
        self.assertIn("usage:", stderr.getvalue())

    def test_no_args_with_pipe_enters_event_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stdin = io.StringIO('{"pane_id":"p","agent_status":"done"}')
            with mock.patch.dict(
                os.environ, self.environment(root / "config", root / "state"), clear=False
            ), mock.patch.object(announce.sys, "stdin", stdin), mock.patch.object(
                announce, "process_invocation", return_value="skipped-status"
            ) as process:
                result = announce.main([])

        self.assertEqual(result, 0)
        self.assertFalse(process.call_args.args[2])

    def test_unknown_argument_uses_argparse_error(self):
        stderr = io.StringIO()
        with mock.patch.object(announce.sys, "stderr", stderr):
            with self.assertRaisesRegex(SystemExit, "2"):
                announce.main(["unknown"])
        self.assertIn("usage:", stderr.getvalue())

    def test_test_mode_uses_resolved_dirs_without_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "real-config"
            state_dir = root / "real-state"
            cwd = root / "cwd"
            cwd.mkdir()
            old_cwd = Path.cwd()
            os.chdir(cwd)
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "HERDR_PLUGIN_CONFIG_DIR": "",
                        "HERDR_PLUGIN_STATE_DIR": "",
                    },
                    clear=False,
                ), mock.patch.object(
                    announce,
                    "resolve_dirs",
                    return_value=(config_dir, state_dir),
                ), mock.patch.object(
                    announce, "process_invocation", return_value="announced+mock"
                ) as process:
                    result = announce.main(["--test"])
            finally:
                os.chdir(old_cwd)

            self.assertEqual(list(cwd.iterdir()), [])

        self.assertEqual(result, 0)
        self.assertEqual(process.call_args.args[:3], (config_dir, state_dir, True))


class OrchestrationTests(unittest.TestCase):
    def test_failed_custom_speech_never_persists_or_displays_argv_secret(self):
        secret = "sk-sentinel-secret-4321"
        command = [secret]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text(
                'speak_command = ["{}"]\n'.format(secret),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                    "HERDR_PLUGIN_STATE_DIR": str(state_dir),
                },
                clear=False,
            ), mock.patch.object(
                speech.subprocess,
                "run",
                side_effect=subprocess.CalledProcessError(2, command),
            ), mock.patch.object(announce.sys, "stderr", stderr):
                result = announce.main(["--test"])

            status = io.StringIO()
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
            ), contextlib.redirect_stdout(status):
                announce.show_status(config_dir, state_dir)

            persisted = (state_dir / "last-error.json").read_text(encoding="utf-8")
            log = (state_dir / "announcer.log").read_text(encoding="utf-8")

        self.assertEqual(result, 1)
        combined = "\n".join((stderr.getvalue(), persisted, log, status.getvalue()))
        self.assertNotIn(secret, combined)
        self.assertIn("****4321", combined)

    def test_template_mode_never_invokes_codex_fallback(self):
        config = dict(announce.DEFAULTS)
        config["summary"] = "template"
        config["summary_fallback"] = "codex"
        with mock.patch.object(announce, "codex_summary") as codex:
            result = announce.make_announcement(
                config, "builder", "billing", "done", "transcript", []
            )

        self.assertEqual(result, ("builder finished in billing.", "template"))
        codex.assert_not_called()

    def test_failed_speech_rolls_back_debounce_record(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            state_dir.mkdir()
            (config_dir / "config.toml").write_text(
                'summary = "template"\n', encoding="utf-8"
            )
            event = '{"pane_id":"p1","agent_status":"done"}'
            with mock.patch.dict(
                os.environ, {"HERDR_PLUGIN_EVENT_JSON": event}, clear=False
            ), mock.patch.object(
                announce,
                "get_context_with_active_panes",
                return_value=("builder", "work", {"p1"}),
            ), mock.patch.object(
                announce, "get_transcript", return_value="output"
            ), mock.patch.object(
                announce, "speak", side_effect=OSError("speaker failed")
            ):
                with self.assertRaisesRegex(OSError, "speaker failed"):
                    announce.process_invocation(
                        config_dir,
                        state_dir,
                        False,
                        {"pane_id": "-", "status": "-"},
                        [],
                    )

            state = announce.load_debounce_state(state_dir / "last.json")
            fingerprints = announce.load_content_fingerprints(
                state_dir / announce.FINGERPRINT_STATE_FILE
            )

        self.assertNotIn("p1", state)
        self.assertNotIn("p1", fingerprints)

    def test_log_reasons_are_appended_after_existing_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            announce.log_invocation(
                state_dir, "p", "done", "template", 1.25, reasons=["codex: failed"]
            )
            line = (state_dir / "announcer.log").read_text(encoding="utf-8")

        self.assertRegex(
            line,
            r" pane_id=p status=done action=template elapsed=1\.250 reasons=codex: failed\n$",
        )

    def test_large_log_is_trimmed_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            path = state_dir / "announcer.log"
            path.write_bytes((b"old line\n" * 70000))

            announce.log_invocation(state_dir, "p", "done", "new", 0.0)

            self.assertLess(path.stat().st_size, announce.LOG_TAIL_BYTES + 1024)
            self.assertTrue(path.read_bytes().endswith(b"action=new elapsed=0.000\n"))


class ContentDedupeCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_dir = self.root / "config"
        self.state_dir = self.root / "state"
        self.config_dir.mkdir()
        self.transcript_path = self.root / "transcript.txt"
        self.spoken_path = self.root / "spoken.txt"

        self.fake_herdr = self.root / "fake-herdr"
        self.fake_herdr.write_text(
            """#!/usr/bin/env python3
import json
import os
import sys

arguments = sys.argv[1:]
if arguments[:2] == ["agent", "list"]:
    panes = os.environ.get("ANNOUNCER_TEST_PANES", "p1").split(",")
    agents = [
        {"pane_id": pane, "name": "builder", "workspace_id": "w1"}
        for pane in panes if pane
    ]
    print(json.dumps({"result": {"agents": agents}}))
elif arguments[:2] == ["workspace", "list"]:
    print(json.dumps({"result": {"workspaces": [{"id": "w1", "label": "work"}]}}))
elif arguments[:2] in (["agent", "read"], ["pane", "read"]):
    with open(os.environ["ANNOUNCER_TEST_TRANSCRIPT"], encoding="utf-8") as handle:
        print(json.dumps({"result": {"text": handle.read()}}))
else:
    raise SystemExit("unexpected fake Herdr command: {!r}".format(arguments))
""",
            encoding="utf-8",
        )
        self.fake_herdr.chmod(0o755)

        self.speaker = self.root / "speaker.py"
        self.speaker.write_text(
            """import os
import sys
import time

time.sleep(float(os.environ.get("ANNOUNCER_TEST_SPEAKER_DELAY", "0")))
with open(sys.argv[1], "a", encoding="utf-8") as handle:
    handle.write(sys.stdin.read() + "\\n")
""",
            encoding="utf-8",
        )
        command = [sys.executable, str(self.speaker), str(self.spoken_path)]
        self.config_dir.joinpath("config.toml").write_text(
            "\n".join(
                (
                    'announce = ["done", "blocked"]',
                    "debounce_seconds = 0",
                    'summary = "template"',
                    "speak_command = {}".format(json.dumps(command)),
                    "",
                )
            ),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temporary_directory.cleanup()

    def event_environment(self, status="done"):
        environment = dict(os.environ)
        environment.update(
            {
                "ANNOUNCER_TEST_PANES": "p1",
                "ANNOUNCER_TEST_TRANSCRIPT": str(self.transcript_path),
                "HERDR_BIN_PATH": str(self.fake_herdr),
                "HERDR_PLUGIN_CONFIG_DIR": str(self.config_dir),
                "HERDR_PLUGIN_EVENT_JSON": json.dumps(
                    {"pane_id": "p1", "agent_status": status}
                ),
                "HERDR_PLUGIN_STATE_DIR": str(self.state_dir),
            }
        )
        return environment

    def announce_command(self):
        return [sys.executable, str(Path(__file__).with_name("announce.py"))]

    def run_event(self, transcript, status="done", speaker_delay=0.0):
        self.transcript_path.write_text(transcript, encoding="utf-8")
        environment = self.event_environment(status)
        environment["ANNOUNCER_TEST_SPEAKER_DELAY"] = str(speaker_delay)
        return subprocess.run(
            self.announce_command(),
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

    def actions(self):
        return [
            entry.action
            for entry in announce.read_log(
                self.state_dir / "announcer.log", limit=10
            )
        ]

    def spoken_lines(self):
        if not self.spoken_path.exists():
            return []
        return self.spoken_path.read_text(encoding="utf-8").splitlines()

    def test_same_content_across_processes_announces_once_and_logs_duplicate(self):
        first = self.run_event("Finished the billing import.\n")
        second = self.run_event("Finished the billing import.\n")

        self.assertEqual((first.returncode, first.stderr), (0, ""))
        self.assertEqual((second.returncode, second.stderr), (0, ""))
        self.assertEqual(self.spoken_lines(), ["builder finished in work."])
        self.assertEqual(
            self.actions(),
            ["announced+summary-template+speak-command", "skipped-duplicate"],
        )

    def test_concurrent_same_content_announces_once(self):
        self.transcript_path.write_text(
            "Finished the billing import.\n", encoding="utf-8"
        )
        environment = self.event_environment()
        environment["ANNOUNCER_TEST_SPEAKER_DELAY"] = "0.25"
        first = subprocess.Popen(
            self.announce_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        time.sleep(0.05)
        second = subprocess.Popen(
            self.announce_command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )

        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)

        self.assertEqual((first.returncode, first_stdout, first_stderr), (0, "", ""))
        self.assertEqual(
            (second.returncode, second_stdout, second_stderr), (0, "", "")
        )
        self.assertEqual(self.spoken_lines(), ["builder finished in work."])
        self.assertCountEqual(
            self.actions(),
            ["announced+summary-template+speak-command", "skipped-duplicate"],
        )

    def test_changed_content_announces_again(self):
        first = self.run_event("Finished the billing import.\n")
        second = self.run_event("Finished the owner export.\n")

        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(
            self.spoken_lines(),
            ["builder finished in work.", "builder finished in work."],
        )
        self.assertEqual(
            self.actions(),
            [
                "announced+summary-template+speak-command",
                "announced+summary-template+speak-command",
            ],
        )

    def test_volatile_codex_footer_changes_are_still_duplicates(self):
        response = "Finished the billing import.\n\n› Write tests for @filename\n\n"
        first = self.run_event(
            response
            + "• Waiting for tests (22s • esc to interrupt)\n"
            + "  gpt-5.6-sol · high · Fast off · ~/work · "
            "Context 41% used · weekly 97% left · Main [default]\n"
        )
        second = self.run_event(
            response
            + "• Waiting for tests (47m 10s • esc to interrupt)\n"
            + "  gpt-5.6-sol · high · Fast off · ~/work · "
            "Context 24% used · weekly 96% left · Main [default]\n"
        )

        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(self.spoken_lines(), ["builder finished in work."])
        self.assertEqual(
            self.actions(),
            ["announced+summary-template+speak-command", "skipped-duplicate"],
        )

    def test_first_event_announces_and_persists_its_fingerprint(self):
        result = self.run_event("Finished the billing import.\n")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.spoken_lines(), ["builder finished in work."])
        state = announce.load_content_fingerprints(
            self.state_dir / announce.FINGERPRINT_STATE_FILE
        )
        self.assertEqual(set(state), {"p1"})
        self.assertRegex(state["p1"], r"^[0-9a-f]{64}$")

    def test_fingerprints_for_closed_panes_are_pruned(self):
        self.state_dir.mkdir()
        (self.state_dir / announce.FINGERPRINT_STATE_FILE).write_text(
            json.dumps({"closed-pane": "a" * 64}) + "\n",
            encoding="utf-8",
        )

        result = self.run_event("Finished the billing import.\n")

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        state = announce.load_content_fingerprints(
            self.state_dir / announce.FINGERPRINT_STATE_FILE
        )
        self.assertEqual(set(state), {"p1"})

    def test_duplicate_event_also_prunes_closed_panes(self):
        transcript = "Finished the billing import.\n"
        self.state_dir.mkdir()
        (self.state_dir / announce.FINGERPRINT_STATE_FILE).write_text(
            json.dumps(
                {
                    "closed-pane": "a" * 64,
                    "p1": announce.content_fingerprint(
                        announce.summary_transcript(transcript)
                    ),
                }
            )
            + "\n",
            encoding="utf-8",
        )

        result = self.run_event(transcript)

        self.assertEqual((result.returncode, result.stderr), (0, ""))
        self.assertEqual(self.actions(), ["skipped-duplicate"])
        state = announce.load_content_fingerprints(
            self.state_dir / announce.FINGERPRINT_STATE_FILE
        )
        self.assertEqual(set(state), {"p1"})

    def test_blocked_events_keep_their_existing_announcement_behavior(self):
        first = self.run_event("Waiting for approval.\n", status="blocked")
        second = self.run_event("Waiting for approval.\n", status="blocked")

        self.assertEqual((first.returncode, second.returncode), (0, 0))
        self.assertEqual(
            self.spoken_lines(),
            [
                "builder needs your input in work.",
                "builder needs your input in work.",
            ],
        )
        self.assertEqual(
            self.actions(),
            [
                "announced+summary-template+speak-command",
                "announced+summary-template+speak-command",
            ],
        )


if __name__ == "__main__":
    unittest.main()
