#!/usr/bin/env python3
"""Announcer dashboard - live status and one-key controls for the plugin."""

# ---- section A: helpers, log, snooze, config, capabilities ----------------

import os
import platform
import select
import shlex
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from announcer import tui as kit
from announcer.config import DEFAULTS, load_config
from announcer.config_io import (
    STATE_ORDER,
    announce_states,
    config_path,
    load_config_safely,
    write_config_keys,
)
from announcer.log import (
    LOG_SCAN_BYTES,
    LOG_SCAN_ENTRIES,
    LogEntry,
    parse_log_line,
    read_log,
)
from announcer.paths import (
    PLUGIN_ID,
    local_plugin_dirs,
    resolve_dirs_without_env,
)
from announcer.snooze import (
    SNOOZE_HOUR,
    SNOOZE_STEPS,
    format_snooze_remaining,
    next_morning,
    next_snooze_step,
    parse_duration,
    read_snooze,
    set_snooze,
    snooze_active,
    snooze_label,
    snooze_message,
    snooze_path,
    snooze_step,
    snooze_target_label,
    write_snooze,
)


PYTHON = sys.executable or "python3"

# Compatibility name retained for callers that reached this helper through
# dashboard.py before directory resolution moved into the package.
_resolve_dirs_without_env = resolve_dirs_without_env

WIDTH = 86                  # widest content line, inside the plugin popup
FRAME_HEIGHT = 28           # tallest frame; render() clamps to the terminal
MIN_WIDTH = 60              # below this a frame is not worth drawing at all
MIN_HEIGHT = 16             # ... nor below this many rows
MIN_FRAME = MIN_HEIGHT - 1  # frame rows at that floor (one row is the cursor);
                            # render() can compose exactly this few lines
POPUP_COLUMNS = 94          # herdr-plugin.toml [[panes]] width ...
POPUP_LINES = 30            # ... and height; also the unmeasurable fallback
RECENT_LINES = 5            # log rows shown
TICK_SECONDS = 1.0          # repaint cadence when no key arrives
MESSAGE_SECONDS = 5.0       # a transient message ages out of the frame

STATE_HELP = {
    "done": "an agent finished work you weren't watching",
    "blocked": "an agent is waiting on your input",
    "idle": "an agent settled while you were watching",
    "working": "an agent started doing something (chatty)",
    "unknown": "unrecognized agent activity (chatty)",
}
CAPABILITY_NAMES = ("codex", "claude", "say", "spd-say", "espeak-ng", "espeak")
# the local-voice probe order, same as announcer/speech.py: espeak-ng is the
# only speech binary on a modern Debian/Arch box, and omitting it made such a
# machine report "nothing detected!" while the announcer spoke happily
VOICE_TOOLS = ("spd-say", "espeak-ng", "espeak")
CONFIG_UNREADABLE = "config unreadable - showing the last good values"
TOO_SMALL = "announcer dashboard: terminal too small ({}x{}, need {}x{})"

# The full key map needs 90 columns, which is why the popup asks for
# POPUP_COLUMNS. A narrower terminal gets a shorter map instead of a wrapped
# line: kit.frame counts logical lines, so one wrap makes the frame walk.
FOOTER = (
    "j/k or arrows move · space/enter toggle · s snooze · t test"
    " · w wizard · r reload · q quit"
)
FOOTER_COMPACT = (
    "j/k move · space toggle · s snooze · t test · w wizard"
    " · r reload · q quit"
)
FOOTER_MINIMAL = "j/k move · space toggle · s snooze · q quit"


@dataclass(frozen=True)
class Row:
    kind: str    # "snooze" | "state" | "toast" | "test" | "wizard"
    key: str     # state name for kind == "state", "" otherwise


ROWS: Tuple[Row, ...] = (
    Row("snooze", ""),
    Row("state", "done"),
    Row("state", "blocked"),
    Row("state", "idle"),
    Row("state", "working"),
    Row("state", "unknown"),
    Row("toast", ""),
    Row("test", ""),
    Row("wizard", ""),
)


def _clip(text: Any, width: int) -> str:
    """Flatten line breaks and cut to width, marking the cut with an ellipsis.
    Interior runs of spaces are PRESERVED: every row of the frame is built out
    of _pad'ed columns, and collapsing whitespace here squeezed that padding
    back out and destroyed the column alignment. Line breaks and tabs are still
    flattened to single spaces - they would tear the frame.
    PLAIN TEXT ONLY - never pass a string that already contains ANSI escapes."""
    plain = str(text)
    for char in ("\r\n", "\n", "\r", "\t", "\x0b", "\x0c"):
        plain = plain.replace(char, " ")
    if width <= 0:
        return ""
    if len(plain) <= width:
        return plain
    return plain[: width - 1] + "…"


def _pad(text: Any, width: int) -> str:
    """_clip then ljust to exactly width."""
    return _clip(text, width).ljust(width)


# _cut was the alignment-preserving twin of a whitespace-collapsing _clip.
# _clip preserves spacing itself now, so the name survives only as an alias.
_cut = _clip


def _plain(text: str) -> str:
    """Strip ANSI SGR/CSI escapes. Used by snapshot() and by tests."""
    result: List[str] = []
    index = 0
    total = len(text)
    while index < total:
        char = text[index]
        if char != "\x1b":
            result.append(char)
            index += 1
            continue
        index += 1
        if index < total and text[index] == "[":
            index += 1
            # a CSI sequence runs until its final byte in the @-~ range
            while index < total and not ("@" <= text[index] <= "~"):
                index += 1
            index += 1
    return "".join(result)


def _label(text: str, focused: bool) -> str:
    """Bold when focused, untouched otherwise."""
    return kit.colorize(text, "1") if focused else text


def format_timestamp(timestamp: str) -> str:
    """'2026-08-17T12:04:11+02:00' -> '12:04:11'. Never raises."""
    text = str(timestamp)
    if "T" not in text:
        return _clip(text, 8)
    # keep the clock part only; the offset suffix is noise in a 8-wide column
    return text.split("T", 1)[1][:8]


def _local_resolve_dirs() -> Tuple[Path, Path]:
    """Last-resort literals, no subprocess."""
    return local_plugin_dirs()


def resolve_dirs() -> Tuple[Path, Path]:
    """(config_dir, state_dir) from the plugin env, with a fallback for each
    side that Herdr did not inject."""
    config_value = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ""
    state_value = os.environ.get("HERDR_PLUGIN_STATE_DIR") or ""
    if config_value and state_value:
        return Path(config_value), Path(state_value)
    fallback_config, fallback_state = _resolve_dirs_without_env()
    return (
        Path(config_value) if config_value else Path(fallback_config),
        Path(state_value) if state_value else Path(fallback_state),
    )


def capabilities() -> Dict[str, Optional[str]]:
    """Capability subset displayed by the dashboard."""
    return {name: shutil.which(name) for name in CAPABILITY_NAMES}


def voice_backend_label(
    config: Dict[str, Any],
    caps: Optional[Dict[str, Optional[str]]] = None,
) -> str:
    """Mirrors announce.speak()'s backend precedence."""
    tools = capabilities() if caps is None else caps
    command = config.get("speak_command")
    if command is not None:
        if not isinstance(command, list):
            return "custom command  (invalid)"
        try:
            rendered = shlex.join(str(part) for part in command)
        except (TypeError, ValueError):
            return "custom command  (invalid)"
        return "custom command  " + _clip(rendered, 48)
    if config.get("elevenlabs_api_key"):
        return "ElevenLabs  voice " + str(config.get("elevenlabs_voice_id"))
    system = platform.system()
    if system == "Darwin":
        voice = config.get("voice")
        if isinstance(voice, str) and voice:
            return "local say  voice " + voice
        return "local say  system voice"
    if system == "Linux":
        found = [name for name in VOICE_TOOLS if tools.get(name)]
        if found:
            return "local " + " / ".join(found)
        return "local  nothing detected!"
    return "unsupported platform: " + system


def measure_terminal() -> Tuple[int, int]:
    """(columns, lines) of the surrounding terminal. An unmeasurable stdout
    (a pipe, a captured stream, CI) reports the popup geometry the manifest
    asks Herdr for, so the frame keeps its full size there. Never raises."""
    size = shutil.get_terminal_size((POPUP_COLUMNS, POPUP_LINES))
    return int(size.columns), int(size.lines)


def footer_text(width: int) -> str:
    """The widest key map that fits `width` columns. The full map needs 90
    columns; a narrower terminal gets a shorter one rather than a wrapped
    line, because kit.frame's cursor-up count is in LOGICAL lines and one
    wrap makes every repaint walk down the screen."""
    for text in (FOOTER, FOOTER_COMPACT, FOOTER_MINIMAL):
        if len(text) <= width:
            return text
    return _clip(FOOTER_MINIMAL, width)


def terminal_fits() -> bool:
    """True when the terminal can hold a useful frame at all. The Dashboard
    clamps its own width and height to whatever it is given (see
    Dashboard.measure), so this only guards the floor: below it the frame is
    replaced by a one-line notice and the CLI prefers a static snapshot."""
    columns, lines = measure_terminal()
    return columns >= MIN_WIDTH and lines >= MIN_HEIGHT


def stdin_ready(timeout: float) -> bool:
    """True when a key is waiting. On a closed or unselectable stdin return
    True so the loop falls back to a blocking read."""
    try:
        ready, _unused, _unused2 = select.select([sys.stdin], [], [], timeout)
    except (OSError, ValueError):
        return True
    return bool(ready)


# ---- section B: Dashboard --------------------------------------------------


class _Sink:
    """Minimal stdout stand-in so the kit's writers reach injected io."""

    def __init__(self, write, flush):
        self.write = write
        self._flush = flush

    def flush(self):
        self._flush()

    def isatty(self):
        return False


class Dashboard:
    def __init__(
        self,
        config_dir: Path,
        state_dir: Path,
        read_key: Optional[Callable[[], str]] = None,
        write: Optional[Callable[[str], Any]] = None,
        flush: Optional[Callable[[], Any]] = None,
        wait_input: Optional[Callable[[float], bool]] = None,
        now: Optional[Callable[[], float]] = None,
    ) -> None:
        self.config_dir = Path(config_dir)
        self.state_dir = Path(state_dir)
        # the one switch for "may I touch the real terminal": raw mode, cursor
        # hiding and exec are all gated on it, so injected io is always safe
        self.owns_terminal = (
            read_key is None and write is None and kit.tty_active()
        )
        self.read_key = kit.read_key if read_key is None else read_key
        if write is None:
            self.write = sys.stdout.write
            self.flush = sys.stdout.flush if flush is None else flush
        else:
            self.write = write
            self.flush = (lambda: None) if flush is None else flush
        self.wait_input = stdin_ready if wait_input is None else wait_input
        self.now = time.time if now is None else now
        # the message property stamps itself with self.now, so both exist first
        self._message = ""
        self._message_at = 0.0
        self.columns = POPUP_COLUMNS
        self.lines = POPUP_LINES
        self.width = WIDTH
        self.frame_height = FRAME_HEIGHT
        self.footer_width = len(FOOTER)
        self.too_small = False
        self.measure()
        loaded = load_config_safely(self.config_dir)
        self.config = dict(DEFAULTS) if loaded is None else loaded
        self.entries: List[LogEntry] = []
        self.snooze_until = 0.0
        self.index = 0
        self.height = 0
        self.message = "" if loaded is not None else CONFIG_UNREADABLE
        self.running = True
        self.wizard_requested = False
        self.last_save_ok = True
        self.test_process: Optional[subprocess.Popen] = None
        self.caps = capabilities()

    @property
    def message(self) -> str:
        return self._message

    @message.setter
    def message(self, text: str) -> None:
        """Every message carries the moment it was set: render() retires it
        after MESSAGE_SECONDS so a stale 'voice test ok' does not sit under the
        frame for the rest of the session."""
        self._message = text
        self._message_at = self.now() if text else 0.0

    def measure(self) -> None:
        """Fit the frame to the terminal; called at construction and on 'r'.
        kit.frame moves the cursor up by a count of LOGICAL lines, so a line
        that wraps or a frame taller than the window makes every repaint walk
        down the screen - the clamp is what keeps an 80x24 terminal steady."""
        self.columns, self.lines = measure_terminal()
        # two columns of slack: a line that reaches the last cell wraps on
        # terminals that defer the wrap, and one wrap makes the frame walk
        self.width = max(MIN_WIDTH - 2, min(WIDTH, self.columns - 2))
        self.frame_height = max(MIN_FRAME, min(FRAME_HEIGHT, self.lines - 1))
        # the footer sits outside the box, so it gets the whole terminal
        self.footer_width = max(1, self.columns - 1)
        self.too_small = self.columns < MIN_WIDTH or self.lines < MIN_HEIGHT

    @contextmanager
    def _stdout(self):
        with redirect_stdout(_Sink(self.write, self.flush)):
            yield

    def _paint(self, lines: List[str]) -> None:
        with self._stdout():
            self.height = kit.frame(lines, self.height)

    def render(self) -> List[str]:
        """Build the frame from self.* only - no io, exactly self.frame_height
        lines, because _frame cannot erase a shrinking frame's leftovers."""
        moment = self.now()
        if self._message and moment - self._message_at >= MESSAGE_SECONDS:
            self.message = ""
        if self.too_small:
            # one plain line, no frame: nothing else can be drawn honestly
            return [TOO_SMALL.format(
                self.columns, self.lines, MIN_WIDTH, MIN_HEIGHT
            )]
        width = self.width
        rail = kit.colorize("│", "90")
        states = announce_states(self.config)
        # line indices that may be dropped, in the order they may go, when the
        # terminal is too short for the whole frame
        recent_extra: List[int] = []
        spacers: List[int] = []
        optional: List[int] = []
        last_resort: List[int] = []

        def cursor(position):
            return kit.colorize("❯", "36") if self.index == position else " "

        def box(on):
            return kit.colorize("◼", "36") if on else kit.colorize("◻", "90")

        if not states:
            badge = kit.colorize("silent · no states selected", "31")
        elif self.snooze_until > moment:
            badge = kit.colorize(
                "snoozed · " + format_snooze_remaining(self.snooze_until, moment),
                "33",
            )
        else:
            badge = kit.colorize("live", "32")

        lines = [
            kit.colorize("◆", "36") + " " + kit.colorize("Announcer", "1") + "  " + badge,
            rail + "  " + kit.colorize(
                _clip("config " + str(config_path(self.config_dir)), width - 3),
                "90",
            ),
            rail,
            rail + "  " + kit.colorize("Recent", "1"),
        ]
        last_resort.append(1)                 # the config path line
        spacers.append(2)
        last_resort.append(3)                 # the "Recent" heading

        recent = list(reversed(self.entries))[:RECENT_LINES]
        rows: List[str] = []
        if not recent:
            rows.append(
                rail + "    " + kit.colorize("no announcements logged yet", "90")
            )
        for entry in recent:
            text = "{}  {}  {}  {}".format(
                _pad(format_timestamp(entry.timestamp), 8),
                _pad(entry.status, 8),
                _pad(entry.pane_id, 12),
                entry.action,
            )
            if entry.reasons:
                text += "  (" + ";".join(entry.reasons) + ")"
            if entry.action == "error":
                code = "31"
            elif entry.action == "snoozed":
                code = "33"
            else:
                code = "90"
            rows.append(rail + "    " + kit.colorize(_clip(text, width - 5), code))
        while len(rows) < RECENT_LINES:
            rows.append(rail)
        for position, row in enumerate(rows):
            lines.append(row)
            if position:                      # the newest row always stays
                recent_extra.append(len(lines) - 1)
        recent_extra.reverse()                # give up the oldest row first

        lines.append(rail)
        spacers.append(len(lines) - 1)

        text = _pad("Snooze", 9) + snooze_label(self.snooze_until, moment)
        hint = "s cycles " + " / ".join(SNOOZE_STEPS)
        lines.append(
            rail + " " + cursor(0) + " "
            + _label(_clip(text, 40), self.index == 0)
            + "  " + kit.colorize(_clip(hint, max(0, width - 46)), "90")
        )

        lines.append(rail)
        spacers.append(len(lines) - 1)
        lines.append(rail + "  " + kit.colorize("Announce on", "1"))
        for position, state in enumerate(STATE_ORDER):
            index = 1 + position
            text = _pad(state, 9) + STATE_HELP[state]
            lines.append(
                rail + " " + cursor(index) + box(state in states) + " "
                + _label(_clip(text, width - 5), self.index == index)
            )

        lines.append(rail)
        spacers.append(len(lines) - 1)
        text = _pad("toast", 9) + "mirror each announcement as a Herdr notification"
        lines.append(
            rail + " " + cursor(6) + box(bool(self.config.get("toast"))) + " "
            + _label(_clip(text, width - 5), self.index == 6)
        )
        lines.append(
            rail + "  " + _pad("voice", 9) + kit.colorize(
                _clip(voice_backend_label(self.config, self.caps), width - 14),
                "90",
            )
        )
        optional.append(len(lines) - 1)
        parts = []
        used = 0
        for name in CAPABILITY_NAMES:
            cost = len(name) + 2 + (2 if parts else 0)
            if used + cost > width - 12:      # rail, gutter and the "tools" tag
                break
            mark = kit.colorize("✓", "32") if self.caps.get(name) else kit.colorize("✗", "90")
            parts.append(name + " " + mark)
            used += cost
        lines.append(rail + "  " + _pad("tools", 9) + "  ".join(parts))
        optional.append(len(lines) - 1)
        optional.reverse()                    # the tools strip goes first

        lines.append(rail)
        spacers.append(len(lines) - 1)
        lines.append(
            rail + " " + cursor(7) + kit.colorize("▸", "36") + " "
            + _label(
                _clip(_pad("Test voice", 15)
                      + "speak a sample announcement now", width - 5),
                self.index == 7,
            )
        )
        lines.append(
            rail + " " + cursor(8) + kit.colorize("▸", "36") + " "
            + _label(
                _clip(_pad("Full setup", 15)
                      + "open the setup wizard (replaces this screen)",
                      width - 5),
                self.index == 8,
            )
        )
        lines.append(kit.colorize("└", "90"))
        lines.append(kit.colorize(footer_text(self.footer_width), "90"))
        lines.append(
            kit.colorize(_clip(self.message, width), "36") if self.message else ""
        )

        spacers.reverse()                     # the lowest blank rail goes first
        return self._fit(lines, recent_extra + spacers + optional + last_resort)

    def _fit(self, lines: List[str], droppable: List[int]) -> List[str]:
        """Cut the composed frame down to self.frame_height by removing the
        least informative lines first, then pad back out. render() must return
        exactly frame_height lines: _frame erases only the lines it redraws."""
        surplus = len(lines) - self.frame_height
        if surplus > 0:
            removed = set(droppable[:surplus])
            lines = [
                line for index, line in enumerate(lines) if index not in removed
            ]
        while len(lines) < self.frame_height:
            lines.append("")
        return lines[: self.frame_height]

    def refresh(self) -> None:
        """Reload everything the frame shows; never raises. A config.toml that
        is corrupt right now (someone is hand-editing it in another pane) keeps
        the last good values on screen instead of tearing the dashboard down."""
        loaded = load_config_safely(self.config_dir)
        if loaded is None:
            self.message = CONFIG_UNREADABLE
        else:
            self.config = loaded
        try:
            self.entries = read_log(
                self.state_dir / "announcer.log", RECENT_LINES
            )
            self.snooze_until = read_snooze(self.state_dir, self.now())
        except Exception:
            pass
        try:
            self.poll_voice_test()
        except Exception:
            pass

    def draw(self) -> None:
        self._paint(self.render())

    def handle_key(self, key: str) -> bool:
        """Dispatch one key. Returns True to keep looping, False to quit."""
        if key in ("j", "down", "\t"):
            self.index = (self.index + 1) % len(ROWS)
            self.message = ""     # moving means the last message is history
        elif key in ("k", "up"):
            self.index = (self.index - 1) % len(ROWS)
            self.message = ""
        elif key in (" ", "\r", "\n"):
            self.activate()
            # activate() sets running = False for the wizard row, and _loop
            # assigns our return value straight onto self.running
            return self.running
        elif key == "s":
            self._snooze()
        elif key == "t":
            self.start_voice_test()
        elif key == "w":
            self.request_wizard()
            return False
        elif key == "r":
            self.measure()        # the window may have been resized
            self.refresh()
            self.message = "reloaded"
        elif key in ("q", "\x03", "\x04"):
            # ctrl-c quits cleanly: no half-written state to roll back.
            # "esc" is NOT a quit key: kit.read_key returns it for every
            # escape sequence it does not recognise - Left/Right arrows,
            # Home/End, mouse and scroll-wheel reports - so binding quit to it
            # closed the popup at random under a stray scroll.
            return False
        return True

    def activate(self) -> None:
        row = ROWS[self.index]
        try:
            if row.kind == "snooze":
                self._snooze()
            elif row.kind == "state":
                self.toggle_state(row.key)
                if self.last_save_ok:
                    states = announce_states(self.config)
                    if states:
                        self.message = "announce: " + ", ".join(states)
                    else:
                        self.message = "announce: (none) - nothing will speak"
            elif row.kind == "toast":
                value = self.toggle_toast()
                if self.last_save_ok:
                    self.message = "toast on" if value else "toast off"
            elif row.kind == "test":
                self.start_voice_test()
            elif row.kind == "wizard":
                self.request_wizard()
                self.running = False
        except (OSError, ValueError) as exc:
            # belt and braces: the toggles already report their own write
            # failures, and neither touches self.config unless the write landed
            self.message = "could not save config: " + _clip(exc, 60)

    def _snooze(self) -> None:
        try:
            until = self.cycle_snooze()
        except (OSError, ValueError) as exc:
            self.message = "could not save snooze: " + _clip(exc, 60)
            return
        self.message = snooze_message(until, self.now())

    def _save(self, updates: Dict[str, Any]) -> bool:
        """Persist a config change, reporting a failed write in the message
        line instead of crashing the popup. On failure self.config is left
        exactly as it was, so the frame keeps describing what is on disk."""
        try:
            self.config = write_config_keys(self.config_dir, updates)
        except (OSError, ValueError) as exc:
            self.message = "could not save config: " + _clip(exc, 60)
            self.last_save_ok = False
            return False
        self.last_save_ok = True
        return True

    def toggle_state(self, state: str) -> bool:
        """Flip `state` in the announce list and persist it in STATE_ORDER.
        Returns the membership that is now on disk - the OLD one when the
        write failed, because the flip is rolled back rather than faked."""
        current = announce_states(self.config)
        member = state not in current
        if member:
            chosen = [
                name for name in STATE_ORDER
                if name in current or name == state
            ]
        else:
            chosen = [name for name in current if name != state]
        if not self._save({"announce": chosen}):
            return not member
        return member

    def toggle_toast(self) -> bool:
        value = not bool(self.config.get("toast"))
        if not self._save({"toast": value}):
            return not value
        return value

    def cycle_snooze(self) -> float:
        step = next_snooze_step(self.snooze_until, self.now())
        # every SNOOZE_STEPS name resolves, so the `or 0.0` is only a guard
        # against a future step name that set_snooze cannot parse
        until = set_snooze(self.state_dir, step, self.now()) or 0.0
        self.snooze_until = until
        return until

    def start_voice_test(self) -> Optional[subprocess.Popen]:
        if self.test_process is not None and self.test_process.poll() is None:
            self.message = "voice test already running"
            return None
        command = [PYTHON, str(SCRIPT_DIR / "announce.py"), "--test"]
        env = dict(os.environ)
        # announce.py --test falls back to "." without these, never to the
        # env-less resolver, so both dirs must be passed explicitly
        env["HERDR_PLUGIN_CONFIG_DIR"] = str(self.config_dir)
        env["HERDR_PLUGIN_STATE_DIR"] = str(self.state_dir)
        try:
            self.test_process = subprocess.Popen(
                command,
                cwd=str(SCRIPT_DIR),
                env=env,
                stdin=subprocess.DEVNULL,
                # nothing ever reads the child's stdout, and an unread pipe
                # fills up and blocks a chatty --test run forever
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            self.message = "voice test failed: " + str(exc)
            self.test_process = None
            return None
        self.message = "voice test running…"
        return self.test_process

    def poll_voice_test(self) -> None:
        if self.test_process is None:
            return
        code = self.test_process.poll()
        if code is None:
            return
        stderr = ""
        if self.test_process.stderr is not None:
            stderr = (self.test_process.stderr.read() or "").strip()
        self._close_test_pipes()
        if code == 0:
            self.message = "voice test ok"
        else:
            detail = stderr.splitlines()[-1] if stderr else "exit {}".format(code)
            self.message = "voice test failed: " + _clip(detail, 60)
        self.test_process = None

    def _close_test_pipes(self) -> None:
        """Close the finished child's pipes explicitly. Dropping the Popen and
        letting the collector do it leaks a descriptor per test until then,
        and emits a ResourceWarning into the frame under -W error."""
        process = self.test_process
        if process is None:
            return
        for stream in (
            getattr(process, "stderr", None), getattr(process, "stdout", None)
        ):
            if stream is None:
                continue
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def request_wizard(self) -> None:
        self.wizard_requested = True
        self.running = False
        self.message = "opening the setup wizard…"

    def exec_wizard(self) -> int:
        """Replace this process with the setup wizard. Only returns on
        failure, or immediately when we do not own the terminal."""
        if not self.owns_terminal:
            return 0
        env = dict(os.environ)
        env["HERDR_PLUGIN_CONFIG_DIR"] = str(self.config_dir)
        env["HERDR_PLUGIN_STATE_DIR"] = str(self.state_dir)
        try:
            os.chdir(str(SCRIPT_DIR))
            os.execvpe(
                PYTHON,
                [PYTHON, str(SCRIPT_DIR / "announce.py"), "setup"],
                env,
            )
        except OSError as exc:
            sys.stderr.write("dashboard error: {}\n".format(exc))
        return 1

    def _collapse_frame(self, answer: str) -> None:
        """Erase the painted frame and leave a single acknowledgement line, the
        way the wizard's own widgets sign off. Without this, `w` looked like it
        did nothing: the whole frame just sat there until execvpe repainted the
        screen, which on a slow wizard start is a visible second of confusion.
        """
        if self.height:
            with self._stdout():
                kit.collapse(self.height, "Announcer", answer)
        else:
            self.write(answer + "\n")
            self.flush()
        self.height = 1

    def run(self) -> int:
        if self.owns_terminal:
            kit.hide_cursor()
        try:
            if self.owns_terminal:
                with kit.raw_mode():
                    self._loop()
            else:
                self._loop()
            if self.wizard_requested:
                self._collapse_frame("opening the setup wizard…")
        finally:
            if self.owns_terminal:
                kit.show_cursor()
                # the collapse already ended the frame with its own newline
                if not self.wizard_requested:
                    self.write("\n")
                self.flush()
        # exec only after raw mode is released: execvpe never comes back
        if self.wizard_requested:
            return self.exec_wizard()
        return 0

    def _loop(self) -> None:
        while self.running:
            self.refresh()
            self.draw()
            if not self.wait_input(TICK_SECONDS):
                continue          # tick: countdown and test result stay live
            self.running = self.handle_key(self.read_key())


# ---- section C: command line -----------------------------------------------

USAGE = (
    "usage: dashboard.py"
    " [snooze 5m|30m|2h|tomorrow|off | toggle-toast | open]\n"
)


def snapshot(config_dir: Path, state_dir: Path) -> str:
    """One-shot plain-text render for non-TTY use (agents, pipes, CI). The
    frame is never repainted in place here, so it is always rendered at full
    size however small the surrounding terminal happens to be."""
    board = Dashboard(config_dir, state_dir, write=lambda text: None)
    board.width = WIDTH
    board.frame_height = FRAME_HEIGHT
    board.footer_width = len(FOOTER)
    board.too_small = False
    board.refresh()
    return "\n".join(_plain(line) for line in board.render()).rstrip() + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config_dir, state_dir = resolve_dirs()
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        if not args:
            if kit.tty_active():
                if terminal_fits():
                    return Dashboard(config_dir, state_dir).run()
                sys.stderr.write(
                    "dashboard: this terminal is smaller than {}x{};"
                    " showing a static snapshot\n".format(
                        MIN_WIDTH, MIN_HEIGHT
                    )
                )
            sys.stdout.write(snapshot(config_dir, state_dir))
            return 0
        if len(args) == 2 and args[0] == "snooze":
            until = set_snooze(state_dir, args[1])
            if until is None:
                sys.stderr.write(USAGE)
                return 2
            if not until:
                sys.stdout.write("snooze off\n")
            else:
                sys.stdout.write("snoozed until {} ({})\n".format(
                    time.strftime("%H:%M", time.localtime(until)), args[1]
                ))
            return 0
        if args == ["toggle-toast"]:
            value = not bool(load_config(config_dir).get("toast"))
            write_config_keys(config_dir, {"toast": value})
            sys.stdout.write("toast on\n" if value else "toast off\n")
            return 0
        if args == ["open"]:
            # route through HERDR_BIN_PATH: a manifest command array cannot
            # expand it, and a bare "herdr" is not always on PATH
            command = [
                os.environ.get("HERDR_BIN_PATH") or "herdr",
                "plugin", "pane", "open",
                "--plugin", PLUGIN_ID,
                "--entrypoint", "dashboard",
            ]
            try:
                result = subprocess.run(command, check=False)
            except OSError as exc:
                sys.stderr.write("dashboard error: {}\n".format(exc))
                return 1
            return result.returncode
        sys.stderr.write(USAGE)
        return 2
    except Exception as exc:
        sys.stderr.write("dashboard error: {}\n".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
