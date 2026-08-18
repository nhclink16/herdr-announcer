"""Small clack-style terminal widgets used by the setup wizard."""

import getpass
import os
import select as _select_mod
import sys
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import termios
    import tty
except ImportError:  # non-Unix
    termios = None  # type: ignore
    tty = None  # type: ignore


def tty_active() -> bool:
    return termios is not None and sys.stdin.isatty() and sys.stdout.isatty()


def colorize(text: str, code: str) -> str:
    return "\x1b[{}m{}\x1b[0m".format(code, text)


@contextmanager
def raw_mode() -> Iterator[None]:
    """Hold raw mode for a whole widget loop so buffered keys never get
    cooked-mode line buffering between reads."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_character(fd: int, first: bytes) -> str:
    data = first
    while len(data) < 4:
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as error:
            if error.reason != "unexpected end of data":
                return data.decode("utf-8", "ignore")
            next_byte = os.read(fd, 1)
            if not next_byte:
                return data.decode("utf-8", "ignore")
            data += next_byte
    return data.decode("utf-8", "ignore")


def read_key(distinguish_escape_sequences: bool = False) -> str:
    """Read one keypress; assumes raw mode. Arrows return 'up'/'down'.

    The default preserves the historical ``"esc"`` result for unknown escape
    sequences. Prompt widgets opt into distinguishing those sequences so only
    a bare Escape key can abort them.
    """
    fd = sys.stdin.fileno()
    first = os.read(fd, 1)
    ch = _read_character(fd, first)
    if ch == "\x1b":
        ready, _unused, _unused2 = _select_mod.select([fd], [], [], 0.05)
        if not ready:
            return "esc"
        sequence = bytearray(os.read(fd, 1))
        if sequence in (b"[", b"O"):
            while len(sequence) < 32:
                ready, _unused, _unused2 = _select_mod.select([fd], [], [], 0.05)
                if not ready:
                    break
                sequence.extend(os.read(fd, 1))
                if 0x40 <= sequence[-1] <= 0x7E:
                    break
        seq = bytes(sequence).decode("utf-8", "ignore")
        if seq in ("[A", "OA"):
            return "up"
        if seq in ("[B", "OB"):
            return "down"
        return "sequence" if distinguish_escape_sequences else "esc"
    return ch


def frame(lines: List[str], previous_height: int) -> int:
    """Redraw a widget frame in place; returns the new frame height."""
    out = sys.stdout
    if previous_height:
        out.write("\x1b[{}A".format(previous_height))
    for line in lines:
        out.write("\r\x1b[2K" + line + "\n")
    out.flush()
    return len(lines)


def collapse(height: int, title: str, answer: str) -> None:
    sys.stdout.write("\x1b[{}A".format(height))
    for _unused in range(height):
        sys.stdout.write("\r\x1b[2K\n")
    sys.stdout.write("\x1b[{}A".format(height))
    sys.stdout.write(
        "\r\x1b[2K"
        + colorize("◇", "90")
        + " "
        + colorize(title, "90")
        + colorize(" · ", "90")
        + colorize(answer, "36")
        + "\n"
    )
    sys.stdout.flush()


def hide_cursor() -> None:
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def ask_select(
    title: str,
    options: Sequence[Tuple[str, str]],
    default_value: str,
    hint: str = "",
) -> Tuple[str, bool]:
    """options: (value, label). Returns (value, changed-from-default)."""
    if not tty_active():
        print(title)
        choices: Dict[str, str] = {}
        for index, (value, label) in enumerate(options, 1):
            choices[str(index)] = value
            print("  {}) {}".format(index, label))
        default_number = next(
            (n for n, v in choices.items() if v == default_value), "1"
        )
        return _prompt_choice("  choice", choices, default_number)

    index = next(
        (i for i, (value, _unused) in enumerate(options) if value == default_value),
        0,
    )
    height = 0
    hide_cursor()
    try:
        with raw_mode():
            while True:
                lines = [colorize("◆", "36") + " " + colorize(title, "1")]
                if hint:
                    lines.append(colorize("│", "90") + "  " + colorize(hint, "90"))
                for i, (_unused, label) in enumerate(options):
                    if i == index:
                        lines.append(
                            colorize("│", "90")
                            + "  "
                            + colorize("●", "36")
                            + " "
                            + colorize(label, "1")
                        )
                    else:
                        lines.append(
                            colorize("│", "90") + "  " + colorize("○ " + label, "90")
                        )
                lines.append(
                    colorize("│", "90")
                    + "  "
                    + colorize(
                        "↑↓ move · enter select · q quit · esc/Ctrl-C quit", "90"
                    )
                )
                lines.append(colorize("└", "90"))
                height = frame(lines, height)
                key = read_key(True)
                if key in ("up", "k"):
                    index = (index - 1) % len(options)
                elif key in ("down", "j", "\t"):
                    index = (index + 1) % len(options)
                elif key.isdigit() and 1 <= int(key) <= len(options):
                    index = int(key) - 1
                elif key in ("\r", "\n"):
                    value, label = options[index]
                    collapse(height, title, label.split("  ")[0].strip())
                    return value, value != default_value
                elif key in ("\x03", "esc", "q"):
                    raise KeyboardInterrupt
    finally:
        show_cursor()


def ask_multiselect(
    title: str,
    options: Sequence[Tuple[str, str]],
    default_selected: Sequence[str],
    hint: str = "space toggles, enter confirms",
) -> Tuple[List[str], bool]:
    if not tty_active():
        while True:
            value, explicit = _prompt(
                title + " (comma list)", ",".join(default_selected)
            )
            if value.lower() == "q":
                raise KeyboardInterrupt
            picked = [v.strip().lower() for v in value.split(",") if v.strip()]
            valid = {v for v, _unused in options}
            if picked and all(v in valid for v in picked):
                return picked, explicit
            print("  Choose from: {}.".format(", ".join(sorted(valid))))

    selected = {value for value in default_selected}
    index = 0
    height = 0
    hide_cursor()
    try:
        with raw_mode():
            while True:
                lines = [colorize("◆", "36") + " " + colorize(title, "1")]
                lines.append(colorize("│", "90") + "  " + colorize(hint, "90"))
                for i, (value, label) in enumerate(options):
                    box = colorize("◼", "36") if value in selected else colorize("◻", "90")
                    cursor = colorize("❯", "36") if i == index else " "
                    text = colorize(label, "1") if i == index else colorize(label, "90")
                    lines.append(colorize("│", "90") + " " + cursor + box + " " + text)
                lines.append(
                    colorize("│", "90")
                    + "  "
                    + colorize(
                        "↑↓ move · space toggle · enter confirm · "
                        "q quit · esc/Ctrl-C quit",
                        "90",
                    )
                )
                lines.append(colorize("└", "90"))
                height = frame(lines, height)
                key = read_key(True)
                if key in ("up", "k"):
                    index = (index - 1) % len(options)
                elif key in ("down", "j", "\t"):
                    index = (index + 1) % len(options)
                elif key == " ":
                    value = options[index][0]
                    if value in selected:
                        selected.discard(value)
                    else:
                        selected.add(value)
                elif key in ("\r", "\n"):
                    if not selected:
                        continue
                    ordered = [v for v, _unused in options if v in selected]
                    collapse(height, title, ", ".join(ordered))
                    return ordered, set(ordered) != set(default_selected)
                elif key in ("\x03", "esc", "q"):
                    raise KeyboardInterrupt
    finally:
        show_cursor()


def ask_confirm(title: str, default: bool) -> Tuple[bool, bool]:
    if not tty_active():
        return _prompt_yes_no(title, default)
    value, changed = ask_select(
        title, [("yes", "Yes"), ("no", "No")], "yes" if default else "no"
    )
    result = value == "yes"
    return result, result != default


def ask_text(
    title: str, default: str, display_default: Optional[str] = None
) -> Tuple[str, bool]:
    """Line input, styled on a TTY. Enter keeps the default."""
    if not tty_active():
        value, explicit = _prompt(title, display_default or default)
        if display_default is not None and value == display_default:
            return default, False
        return value, explicit
    shown = display_default if display_default is not None else default
    raw, height = _ask_tty_line(title, shown, secret=False)
    value = raw.strip()
    if not value or (display_default is not None and value == shown):
        result, explicit = default, False
    else:
        result, explicit = value, True
    if not explicit:
        summary = shown
    elif display_default is not None:
        summary = "(updated)"
    else:
        summary = result
    collapse(height, title, str(summary) if summary else "(blank)")
    return result, explicit


def ask_secret(
    title: str, default: str, display_default: Optional[str] = None
) -> Tuple[str, bool]:
    shown = display_default if display_default is not None else ""
    if tty_active():
        raw, height = _ask_tty_line(title, shown, secret=True)
        value = raw.strip()
        collapse(height, title, "(updated)" if value else shown or "(blank)")
        return (value, True) if value else (default, False)
    prompt = "{} [{}]: ".format(title, shown) if shown else "{}: ".format(title)
    try:
        raw = getpass.getpass(prompt)
    except EOFError:
        raw = ""
    if "\x03" in raw:
        raise KeyboardInterrupt
    value = raw.strip()
    return (value, True) if value else (default, False)


def _ask_tty_line(title: str, shown_default: str, secret: bool) -> Tuple[str, int]:
    """Read an editable line in raw mode so abort keys remain observable."""
    entered: List[str] = []
    height = 0
    try:
        with raw_mode():
            while True:
                visible = "•" * len(entered) if secret else "".join(entered)
                default_hint = "[{}] ".format(shown_default) if shown_default else ""
                lines = [colorize("◆", "36") + " " + colorize(title, "1")]
                lines.append(
                    colorize("│", "90")
                    + "  "
                    + colorize(default_hint, "90")
                    + "> "
                    + visible
                )
                lines.append(
                    colorize("│", "90")
                    + "  "
                    + colorize("enter confirms · esc/Ctrl-C quit", "90")
                )
                lines.append(colorize("└", "90"))
                height = frame(lines, height)
                key = read_key(True)
                if key in ("\x03", "esc"):
                    raise KeyboardInterrupt
                if key in ("\r", "\n"):
                    return "".join(entered), height
                if key == "\x04":
                    return "".join(entered), height
                if key in ("\x7f", "\x08"):
                    if entered:
                        entered.pop()
                    continue
                if len(key) == 1 and key.isprintable():
                    entered.append(key)
    finally:
        show_cursor()


def _prompt(label: str, default: str) -> Tuple[str, bool]:
    try:
        value = input("{} [{}]: ".format(label, default))
    except EOFError:
        return default, False
    if "\x03" in value:
        raise KeyboardInterrupt
    value = value.strip()
    return (value, True) if value else (default, False)


def _prompt_choice(
    label: str, choices: Dict[str, str], default: str
) -> Tuple[str, bool]:
    while True:
        value, explicit = _prompt(label, default)
        if value.lower() == "q":
            raise KeyboardInterrupt
        selected = choices.get(value.lower())
        if selected is not None:
            return selected, explicit
        print("Please choose {}.".format(", ".join(choices)))


def _prompt_yes_no(label: str, default: bool) -> Tuple[bool, bool]:
    while True:
        value, explicit = _prompt(label, "Y/n" if default else "y/N")
        if not explicit:
            return default, False
        if value.lower() == "q":
            raise KeyboardInterrupt
        if value.lower() in ("y", "yes"):
            return True, True
        if value.lower() in ("n", "no"):
            return False, True
        print("Please enter yes or no.")


# Compatibility aliases. These names were imported through announce.py before
# the package split, so keep them indefinitely even though new code should use
# the descriptive public API above.
_tty_active = tty_active
_c = colorize
_raw_mode = raw_mode
_read_key = read_key
_frame = frame
_collapse = collapse
_hide_cursor = hide_cursor
_show_cursor = show_cursor


__all__ = [
    "_c",
    "_collapse",
    "_frame",
    "_hide_cursor",
    "_prompt",
    "_prompt_choice",
    "_prompt_yes_no",
    "_raw_mode",
    "_read_key",
    "_show_cursor",
    "_tty_active",
    "ask_confirm",
    "ask_multiselect",
    "ask_secret",
    "ask_select",
    "ask_text",
    "collapse",
    "colorize",
    "frame",
    "hide_cursor",
    "raw_mode",
    "read_key",
    "show_cursor",
    "termios",
    "tty",
    "tty_active",
]
