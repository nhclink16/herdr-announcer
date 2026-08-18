import io
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import announce


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
    @mock.patch.object(announce.subprocess, "run")
    def test_failed_speech_command_never_persists_its_secret_argv(self, run):
        secret = "sentinel-speech-secret"
        run.side_effect = subprocess.CalledProcessError(
            23, ["speaker", "--token", secret]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            (config_dir / "config.toml").write_text(
                'summary = "template"\n'
                'speak_command = ["speaker", "--token", "{}"]\n'.format(secret),
                encoding="utf-8",
            )
            environment = {
                "HERDR_PLUGIN_CONFIG_DIR": str(config_dir),
                "HERDR_PLUGIN_STATE_DIR": str(state_dir),
            }

            with mock.patch.dict(os.environ, environment, clear=False), mock.patch.object(
                announce.sys, "stderr", io.StringIO()
            ):
                result = announce.main(["--test"])

            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in (state_dir / "announcer.log", state_dir / "last-error.json")
            )

        self.assertEqual(result, 1)
        self.assertNotIn(secret, persisted)
        self.assertIn("speak-command: exit-23", persisted)

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
                announce, "get_context", return_value=("builder", "work")
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

        self.assertNotIn("p1", state)

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


if __name__ == "__main__":
    unittest.main()
