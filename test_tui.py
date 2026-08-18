import contextlib
import io
import os
import subprocess
import sys
import termios
import time
import unittest
from typing import Tuple
from unittest import mock

from announcer import tui


class PromptAbortTests(unittest.TestCase):
    @staticmethod
    def wait_and_drain(
        process: subprocess.Popen, master_fd: int, output: bytearray, timeout: float
    ) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                output.extend(os.read(master_fd, 4096))
            except BlockingIOError:
                pass
            return_code = process.poll()
            if return_code is not None:
                return return_code
            time.sleep(0.005)
        raise subprocess.TimeoutExpired(process.args, timeout)

    def run_in_pty(self, expression: str, keys: bytes) -> Tuple[int, bytes, bool]:
        master_fd, slave_fd = os.openpty()
        before = termios.tcgetattr(slave_fd)
        script = (
            "from announcer import tui\n"
            "try:\n"
            "    result = {}\n"
            "except KeyboardInterrupt:\n"
            "    print('PROMPT_ABORTED')\n"
            "    raise SystemExit(130)\n"
            "print('PROMPT_RESULT={{!r}}'.format(result))\n"
        ).format(expression)
        process = subprocess.Popen(
            [sys.executable, "-c", script],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
        os.set_blocking(master_fd, False)
        output = bytearray()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.fail("prompt ended before raw mode: {!r}".format(bytes(output)))
            try:
                output.extend(os.read(master_fd, 4096))
            except BlockingIOError:
                pass
            if not termios.tcgetattr(slave_fd)[3] & termios.ICANON:
                break
            time.sleep(0.005)
        else:
            process.terminate()
            process.wait(2.0)
            self.fail("prompt did not enter raw mode")
        os.write(master_fd, keys)
        needed_cleanup = False
        try:
            return_code = self.wait_and_drain(process, master_fd, output, 1.0)
        except subprocess.TimeoutExpired:
            needed_cleanup = True
            os.write(master_fd, b"\x03")
            return_code = self.wait_and_drain(process, master_fd, output, 2.0)
        after = termios.tcgetattr(slave_fd)
        # macOS sets PENDIN after queued PTY input; it does not describe a
        # raw/cooked-mode setting owned by the prompt.
        after[3] &= ~getattr(termios, "PENDIN", 0)
        before[3] &= ~getattr(termios, "PENDIN", 0)
        self.assertEqual(after, before)

        while True:
            try:
                chunk = os.read(master_fd, 4096)
            except BlockingIOError:
                break
            if not chunk:
                break
            output.extend(chunk)
        os.close(master_fd)
        os.close(slave_fd)
        return return_code, bytes(output), needed_cleanup

    def assert_aborted(self, expression: str, keys: bytes) -> None:
        return_code, output, needed_cleanup = self.run_in_pty(expression, keys)
        self.assertFalse(needed_cleanup, "prompt ignored the abort key")
        self.assertEqual(return_code, 130)
        self.assertIn(b"PROMPT_ABORTED", output)
        self.assertIn(b"\x1b[?25h", output)

    def assert_completed(self, expression: str, keys: bytes) -> bytes:
        return_code, output, needed_cleanup = self.run_in_pty(expression, keys)
        self.assertFalse(needed_cleanup)
        self.assertEqual(return_code, 0)
        self.assertIn(b"PROMPT_RESULT=", output)
        self.assertIn(b"\x1b[?25h", output)
        return output

    def test_ctrl_c_aborts_every_prompt_and_restores_terminal(self):
        prompts = (
            "tui.ask_select('Pick', [('one', 'One')], 'one')",
            "tui.ask_multiselect('Pick', [('one', 'One')], ['one'])",
            "tui.ask_confirm('Continue?', True)",
            "tui.ask_text('Name', 'default')",
            "tui.ask_secret('Secret', '')",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assert_aborted(prompt, b"\x03")

    def test_select_q_aborts_and_restores_terminal(self):
        self.assert_aborted(
            "tui.ask_select('Pick', [('one', 'One')], 'one')", b"q"
        )

    def test_multiselect_q_aborts_and_restores_terminal(self):
        self.assert_aborted(
            "tui.ask_multiselect('Pick', [('one', 'One')], ['one'])", b"q"
        )

    def test_confirm_q_aborts_and_restores_terminal(self):
        self.assert_aborted("tui.ask_confirm('Continue?', True)", b"q")

    def test_text_ctrl_c_mid_entry_aborts_and_restores_terminal(self):
        self.assert_aborted("tui.ask_text('Name', 'default')", b"typed\x03")

    def test_secret_ctrl_c_mid_entry_aborts_and_restores_terminal(self):
        self.assert_aborted("tui.ask_secret('Secret', '')", b"typed\x03")

    def test_q_remains_text_and_secret_input(self):
        text_output = self.assert_completed(
            "tui.ask_text('Name', 'default')", b"q\r"
        )
        secret_output = self.assert_completed(
            "tui.ask_secret('Secret', '')", b"q\r"
        )
        self.assertIn(b"PROMPT_RESULT=('q', True)", text_output)
        self.assertIn(b"PROMPT_RESULT=('q', True)", secret_output)

    def test_ctrl_d_accepts_text_and_secret_defaults(self):
        prompts = (
            "tui.ask_text('Name', 'default')",
            "tui.ask_secret('Secret', 'default')",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                output = self.assert_completed(prompt, b"\x04")
                self.assertIn(b"PROMPT_RESULT=('default', False)", output)

    def test_text_entry_preserves_unicode_and_backspace_editing(self):
        output = self.assert_completed(
            "tui.ask_text('Name', 'default')", "qa\x7fé\r".encode("utf-8")
        )
        self.assertIn("PROMPT_RESULT=('qé', True)".encode("utf-8"), output)
        self.assertNotIn(b"\x1b[2A", output)

    def test_bare_escape_aborts_every_prompt(self):
        prompts = (
            "tui.ask_select('Pick', [('one', 'One')], 'one')",
            "tui.ask_multiselect('Pick', [('one', 'One')], ['one'])",
            "tui.ask_confirm('Continue?', True)",
            "tui.ask_text('Name', 'default')",
            "tui.ask_secret('Secret', '')",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assert_aborted(prompt, b"\x1b")

    def test_arrow_and_unknown_escape_sequence_do_not_abort(self):
        expression = "tui.ask_select('Pick', [('one', 'One')], 'one')"
        for sequence in (b"\x1b[B\r", b"\x1b[C\r", b"\x1b[1;2q\r"):
            with self.subTest(sequence=sequence):
                output = self.assert_completed(expression, sequence)
                self.assertIn(b"PROMPT_RESULT=('one', False)", output)

    def test_prompt_footer_explains_how_to_exit(self):
        output = self.assert_completed(
            "tui.ask_confirm('Continue?', True)", b"\r"
        )
        self.assertIn(b"q quit", output)
        self.assertIn(b"Ctrl-C quit", output)

    def test_non_tty_literal_ctrl_c_aborts_every_prompt(self):
        prompts = (
            lambda: tui.ask_select("Pick", [("one", "One")], "one"),
            lambda: tui.ask_multiselect("Pick", [("one", "One")], ["one"]),
            lambda: tui.ask_confirm("Continue?", True),
            lambda: tui.ask_text("Name", "default"),
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt), mock.patch.object(
                tui, "tty_active", return_value=False
            ), mock.patch(
                "builtins.input", return_value="typed\x03"
            ), contextlib.redirect_stdout(
                io.StringIO()
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prompt()
        with mock.patch.object(
            tui, "tty_active", return_value=False
        ), mock.patch("getpass.getpass", return_value="typed\x03"):
            with self.assertRaises(KeyboardInterrupt):
                tui.ask_secret("Secret", "")

    def test_non_tty_q_only_aborts_choice_prompts(self):
        choices = (
            lambda: tui.ask_select("Pick", [("one", "One")], "one"),
            lambda: tui.ask_multiselect("Pick", [("one", "One")], ["one"]),
            lambda: tui.ask_confirm("Continue?", True),
        )
        for prompt in choices:
            with self.subTest(prompt=prompt), mock.patch.object(
                tui, "tty_active", return_value=False
            ), mock.patch(
                "builtins.input", return_value="q"
            ), contextlib.redirect_stdout(
                io.StringIO()
            ):
                with self.assertRaises(KeyboardInterrupt):
                    prompt()
        with mock.patch.object(
            tui, "tty_active", return_value=False
        ), mock.patch("builtins.input", return_value="q"):
            self.assertEqual(tui.ask_text("Name", "default"), ("q", True))
        with mock.patch.object(
            tui, "tty_active", return_value=False
        ), mock.patch("getpass.getpass", return_value="q"):
            self.assertEqual(tui.ask_secret("Secret", ""), ("q", True))


if __name__ == "__main__":
    unittest.main()
