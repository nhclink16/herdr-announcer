import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from announcer.config import DEFAULTS, load_config
from announcer import wizard


class ConfigWriterTests(unittest.TestCase):
    def test_private_config_helpers_alias_public_api(self):
        self.assertIs(wizard._load_raw_config, wizard.load_raw_config)

    def test_load_raw_config_reads_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('voice = "Alex"\n', encoding="utf-8")

            loaded = wizard.load_raw_config(path)

        self.assertEqual(loaded["voice"], "Alex")

    def test_private_writer_keeps_nondefault_values_without_chosen(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            config = dict(DEFAULTS)
            config["voice"] = "Alex"

            with contextlib.redirect_stdout(io.StringIO()):
                wizard._write_config(path, config, [])

            loaded = load_config(config_dir)

        self.assertEqual(loaded["voice"], "Alex")

    def test_quoted_known_key_is_replaced_without_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            path.write_text('"toast" = false\n', encoding="utf-8")
            config = load_config(config_dir)
            config["toast"] = True

            with contextlib.redirect_stdout(io.StringIO()):
                wizard.write_config(path, config, ["toast"])

            contents = path.read_text(encoding="utf-8")
            loaded = load_config(config_dir)

        self.assertEqual(contents.count("toast"), 1)
        self.assertTrue(loaded["toast"])

    def test_unrelated_update_preserves_multiline_known_values(self):
        original = (
            'announce = [\n    "done",\n    "idle",\n]\n'
            'custom_prompt = """First line\nSecond line"""\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            path.write_text(original, encoding="utf-8")
            config = load_config(config_dir)
            config["toast"] = True

            with contextlib.redirect_stdout(io.StringIO()):
                wizard.write_config(path, config, ["toast"])

            contents = path.read_text(encoding="utf-8")

        self.assertIn(original, contents)
        self.assertIn("toast = true", contents)

    def test_targeted_update_replaces_multiline_known_value(self):
        original = 'announce = [\n    "done",\n    "idle",\n]\nvoice = "Alex"\n'
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            path.write_text(original, encoding="utf-8")
            config = load_config(config_dir)
            config["announce"] = ["blocked"]

            with contextlib.redirect_stdout(io.StringIO()):
                wizard.write_config(path, config, ["announce"])

            contents = path.read_text(encoding="utf-8")
            loaded = load_config(config_dir)

        self.assertEqual(loaded["announce"], ["blocked"])
        self.assertIn('voice = "Alex"', contents)
        self.assertNotIn('"idle"', contents)

    def test_multiline_string_ending_in_quote_does_not_consume_next_key(self):
        for multiline in ('"""abc""""', "'''abc''''"):
            with self.subTest(multiline=multiline), tempfile.TemporaryDirectory() as directory:
                config_dir = Path(directory)
                path = config_dir / "config.toml"
                path.write_text(
                    "custom_prompt = {}\ntoast = false\n".format(multiline),
                    encoding="utf-8",
                )
                config = load_config(config_dir)
                config["toast"] = True

                with contextlib.redirect_stdout(io.StringIO()):
                    wizard.write_config(path, config, ["toast"])

                contents = path.read_text(encoding="utf-8")
                loaded = load_config(config_dir)

            self.assertEqual(contents.count("toast"), 1)
            self.assertTrue(loaded["toast"])
            if wizard.tomllib is not None:
                self.assertEqual(
                    loaded["custom_prompt"],
                    'abc"' if multiline[0] == '"' else "abc'",
                )

    def test_unicode_escaped_known_key_is_replaced_without_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            path.write_text('"\\U00000074oast" = false\n', encoding="utf-8")
            config = load_config(config_dir)
            config["toast"] = True

            with contextlib.redirect_stdout(io.StringIO()):
                wizard.write_config(path, config, ["toast"])

            contents = path.read_text(encoding="utf-8")
            loaded = load_config(config_dir)

        self.assertEqual(contents.count("toast"), 1)
        self.assertNotIn("\\U00000074", contents)
        self.assertTrue(loaded["toast"])

    def test_unknown_float_and_table_survive_known_update(self):
        original_unknown = (
            "future_timeout = 1.5\n"
            "[future]\n"
            'provider = "new-backend"\n'
            "nested = { enabled = true }\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            path.write_text('summary = "template"\n' + original_unknown)
            config = load_config(config_dir)
            config["toast"] = True

            with contextlib.redirect_stdout(io.StringIO()):
                wizard.write_config(path, config, ["toast"])

            contents = path.read_text(encoding="utf-8")

        self.assertIn('summary = "template"', contents)
        self.assertIn("toast = true", contents)
        self.assertIn(original_unknown, contents)

    def test_stale_writers_rebase_chosen_keys_on_latest_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            path = config_dir / "config.toml"
            stale_a = dict(DEFAULTS)
            stale_b = dict(DEFAULTS)
            stale_a["toast"] = True
            stale_b["voice"] = "Alex"

            with contextlib.redirect_stdout(io.StringIO()):
                wizard.write_config(path, stale_a, ["toast"])
                wizard.write_config(path, stale_b, ["voice"])

            loaded = load_config(config_dir)

        self.assertTrue(loaded["toast"])
        self.assertEqual(loaded["voice"], "Alex")

    def test_config_lock_serializes_writers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            entered = threading.Event()
            release = threading.Event()
            second_entered = threading.Event()

            def first():
                with wizard.config_lock(path):
                    entered.set()
                    release.wait(2.0)

            def second():
                entered.wait(2.0)
                with wizard.config_lock(path):
                    second_entered.set()

            first_thread = threading.Thread(target=first)
            second_thread = threading.Thread(target=second)
            first_thread.start()
            second_thread.start()
            self.assertTrue(entered.wait(2.0))
            self.assertFalse(second_entered.wait(0.05))
            release.set()
            first_thread.join(2.0)
            second_thread.join(2.0)

        self.assertTrue(second_entered.is_set())

    def test_write_then_reload_preserves_all_values(self):
        config = dict(DEFAULTS)
        config.update(
            {
                "announce": ["idle", "done"],
                "debounce_seconds": 9,
                "summary": "command",
                "summary_fallback": "codex",
                "summary_first_activity_timeout_seconds": 7,
                "codex_model": "model",
                "codex_effort": "medium",
                "codex_timeout_seconds": 33,
                "summary_command": ["tool", "{agent}"],
                "summary_command_timeout_seconds": 44,
                "style": "custom",
                "custom_prompt": "custom {status}",
                "speak_command": ["speaker"],
                "elevenlabs_api_key": "secret",
                "elevenlabs_voice_id": "voice",
                "elevenlabs_model": "eleven",
                "voice": "Samantha",
                "toast": True,
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                wizard._write_config(
                    config_dir / "config.toml", config, list(DEFAULTS)
                )
            loaded = load_config(config_dir)

        self.assertEqual(loaded, config)
        self.assertIn("do not preserve comments", output.getvalue())

    def test_preview_masks_api_key_to_last_four(self):
        line = 'elevenlabs_api_key = "super-secret-1234"'
        preview = wizard._preview_line(line)
        self.assertEqual(preview, 'elevenlabs_api_key = "****1234"')
        self.assertNotIn("super-secret", preview)


class WizardFlowTests(unittest.TestCase):
    def test_wizard_intro_explains_every_exit_key(self):
        capabilities = {
            name: None
            for name in (
                "codex",
                "claude",
                "say",
                "spd-say",
                "espeak-ng",
                "espeak",
                "mpv",
                "ffplay",
                "afplay",
                "paplay",
                "pw-play",
                "aplay",
            )
        }
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            wizard, "capabilities", return_value=capabilities
        ), mock.patch.object(
            wizard, "tty_active", return_value=True
        ), mock.patch.object(
            wizard, "ask_multiselect", side_effect=KeyboardInterrupt
        ), contextlib.redirect_stdout(output):
            with self.assertRaises(KeyboardInterrupt):
                wizard._setup_wizard(
                    Path(directory) / "config", Path(directory) / "state"
                )

        text = output.getvalue()
        self.assertIn("q quits choices", text)
        self.assertIn("Esc or Ctrl-C exits", text)

    def test_ctrl_c_after_write_restores_config_backup_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            state_dir = root / "state"
            config_dir.mkdir()
            path = config_dir / "config.toml"
            backup_path = config_dir / "config.toml.bak"
            original = b'# exact original\nsummary = "codex"\n'
            original_backup = b'# exact prior backup\nsummary = "command"\n'
            path.write_bytes(original)
            backup_path.write_bytes(original_backup)
            path.chmod(0o640)
            backup_path.chmod(0o600)

            def write_then_interrupt(_config_dir, _state_dir, write_state):
                config = load_config(config_dir)
                config["summary"] = "template"
                wizard.write_config(path, config, ["summary"])
                write_state["written"] = True
                raise KeyboardInterrupt

            output = io.StringIO()
            with mock.patch.object(
                wizard, "_setup_wizard", side_effect=write_then_interrupt
            ), contextlib.redirect_stdout(output):
                result = wizard.run_setup(config_dir, state_dir)

            contents = path.read_bytes()
            backup_contents = backup_path.read_bytes()
            restored_mode = path.stat().st_mode & 0o7777
            restored_backup_mode = backup_path.stat().st_mode & 0o7777
            restore_temps = list(config_dir.glob("*.restore.*.tmp"))
            reacquired = threading.Event()

            def reacquire():
                with wizard.config_lock(path):
                    reacquired.set()

            thread = threading.Thread(target=reacquire, daemon=True)
            thread.start()
            thread.join(1.0)

        self.assertEqual(result, 130)
        self.assertEqual(contents, original)
        self.assertEqual(backup_contents, original_backup)
        self.assertEqual(restored_mode, 0o640)
        self.assertEqual(restored_backup_mode, 0o600)
        self.assertEqual(restore_temps, [])
        self.assertTrue(reacquired.is_set())
        self.assertIn("nothing written", output.getvalue())

    def test_abort_before_write_does_not_replace_unchanged_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_dir = root / "config"
            config_dir.mkdir()
            path = config_dir / "config.toml"
            original = b'# untouched\nsummary = "codex"\n'
            path.write_bytes(original)
            before = path.stat()

            with mock.patch.object(
                wizard, "_setup_wizard", side_effect=KeyboardInterrupt
            ), contextlib.redirect_stdout(io.StringIO()):
                result = wizard.run_setup(config_dir, root / "state")

            after = path.stat()
            contents = path.read_bytes()

        self.assertEqual(result, 130)
        self.assertEqual(contents, original)
        self.assertEqual(after.st_ino, before.st_ino)
        self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)

    def test_fresh_install_voice_choices_omit_keep_current(self):
        class StopWizard(Exception):
            pass

        capabilities = {
            name: None
            for name in (
                "codex",
                "claude",
                "say",
                "spd-say",
                "espeak-ng",
                "espeak",
                "mpv",
                "ffplay",
                "afplay",
                "paplay",
                "pw-play",
                "aplay",
            )
        }
        seen_voice_options = []

        def select(title, options, default):
            if title == "Who writes the summary sentence?":
                return "template", True
            if title == "Where should the voice come out?":
                seen_voice_options.extend(options)
                raise StopWizard
            raise AssertionError(title)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.object(wizard, "capabilities", return_value=capabilities), mock.patch.object(
                wizard, "tty_active", return_value=False
            ), mock.patch.object(
                wizard, "ask_multiselect", return_value=(["done", "blocked"], False)
            ), mock.patch.object(
                wizard, "ask_select", side_effect=select
            ), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(StopWizard):
                    wizard._setup_wizard(root / "config", root / "state")

        self.assertNotIn("keep", [value for value, _label in seen_voice_options])


if __name__ == "__main__":
    unittest.main()
