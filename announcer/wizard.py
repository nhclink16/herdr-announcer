"""Interactive setup wizard and configuration writer."""

import json
import os
import platform
import shlex
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, MutableMapping, Optional, Sequence, Set, Tuple

from .config import DEFAULTS, _load_tiny_toml, load_config, tomllib
from .speech import capabilities, speak
from .summarize import ANNOUNCEMENT_PROMPT
from .tui import (
    ask_confirm,
    ask_multiselect,
    ask_secret,
    ask_select,
    ask_text,
    colorize,
    tty_active,
)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[{}]".format(", ".join(json.dumps(item) for item in value))
    raise ValueError("cannot write configuration value")


def load_raw_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if tomllib is not None:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        return _load_tiny_toml(path)
    except (OSError, ValueError):
        return {}


@contextmanager
def config_lock(path: Path) -> Iterator[None]:
    """Serialize config read-modify-write transactions across processes."""
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name("config.toml.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _assignment_separator(line: str) -> int:
    quote = ""
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            escaped = False
            continue
        if char == "\\" and quote == '"':
            escaped = True
            continue
        if char in ('"', "'"):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            continue
        if char == "#" and not quote:
            return -1
        if char == "=" and not quote:
            return index
    return -1


def _known_key(raw_key: str) -> Optional[str]:
    key = raw_key.strip()
    if key in DEFAULTS:
        return key
    if len(key) < 2 or key[0] != key[-1] or key[0] not in ('"', "'"):
        return None
    if key[0] == "'":
        decoded = key[1:-1]
    else:
        decoded_parts: List[str] = []
        content = key[1:-1]
        index = 0
        escapes = {
            '"': '"',
            "\\": "\\",
            "b": "\b",
            "t": "\t",
            "n": "\n",
            "f": "\f",
            "r": "\r",
        }
        while index < len(content):
            if content[index] != "\\":
                decoded_parts.append(content[index])
                index += 1
                continue
            index += 1
            if index >= len(content):
                return None
            escape = content[index]
            if escape in escapes:
                decoded_parts.append(escapes[escape])
                index += 1
                continue
            if escape not in ("u", "U"):
                return None
            digits = 4 if escape == "u" else 8
            encoded = content[index + 1:index + 1 + digits]
            if len(encoded) != digits:
                return None
            try:
                decoded_parts.append(chr(int(encoded, 16)))
            except (ValueError, OverflowError):
                return None
            index += digits + 1
        decoded = "".join(decoded_parts)
    return decoded if decoded in DEFAULTS else None


def _toml_value_end(text: str, start: int) -> int:
    """Return the end of one TOML value, including its final newline."""
    square_depth = 0
    curly_depth = 0
    quote = ""
    triple = False
    escaped = False
    comment = False
    index = start
    while index < len(text):
        char = text[index]
        if comment:
            if char == "\n":
                comment = False
                if square_depth == 0 and curly_depth == 0:
                    return index + 1
            index += 1
            continue
        if quote:
            if escaped:
                escaped = False
                index += 1
                continue
            if quote == '"' and char == "\\":
                escaped = True
                index += 1
                continue
            if triple and char == quote:
                run_end = index
                while run_end < len(text) and text[run_end] == quote:
                    run_end += 1
                if run_end - index >= 3:
                    quote = ""
                    triple = False
                index = run_end
                continue
            if not triple and char == quote:
                quote = ""
                index += 1
                continue
            index += 1
            continue
        if char == "#":
            comment = True
            index += 1
            continue
        if text.startswith('\"\"\"', index) or text.startswith("'''", index):
            quote = char
            triple = True
            index += 3
            continue
        if char in ('"', "'"):
            quote = char
            index += 1
            continue
        if char == "[":
            square_depth += 1
        elif char == "]" and square_depth:
            square_depth -= 1
        elif char == "{":
            curly_depth += 1
        elif char == "}" and curly_depth:
            curly_depth -= 1
        elif char == "\n" and square_depth == 0 and curly_depth == 0:
            return index + 1
        index += 1
    return len(text)


def _known_assignment_spans(text: str) -> Dict[str, List[Tuple[int, int]]]:
    spans: Dict[str, List[Tuple[int, int]]] = {}
    offset = 0
    while offset < len(text):
        newline = text.find("\n", offset)
        line_end = len(text) if newline < 0 else newline + 1
        line = text[offset:line_end]
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            offset = line_end
            continue
        if stripped.startswith("["):
            break
        separator = _assignment_separator(line)
        if separator < 0:
            offset = line_end
            continue
        end = _toml_value_end(text, offset + separator + 1)
        key = _known_key(line[:separator])
        if key is not None:
            spans.setdefault(key, []).append((offset, end))
        offset = end
    return spans


def _top_level_known_keys(path: Path) -> Set[str]:
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8")
    return set(_known_assignment_spans(text))


def _rewrite_config_text(
    original: str,
    config: Dict[str, Any],
    keys_to_write: Sequence[str],
    keys_to_replace: Sequence[str],
) -> str:
    spans = _known_assignment_spans(original)
    removed = [
        span
        for key in keys_to_replace
        for span in spans.get(key, [])
    ]
    removed.sort()
    pieces: List[str] = []
    cursor = 0
    for start, end in removed:
        pieces.append(original[cursor:start])
        cursor = end
    pieces.append(original[cursor:])
    preserved = "".join(pieces)
    write_keys = set(keys_to_write)
    lines = [
        "{} = {}".format(key, _toml_value(config[key]))
        for key in DEFAULTS
        if key in write_keys and config.get(key) is not None
    ]
    prefix = "".join(line + "\n" for line in lines)
    return prefix + preserved


def _config_lines(
    config: Dict[str, Any], explicitly_chosen: Sequence[str]
) -> List[str]:
    chosen = set(explicitly_chosen)
    return [
        "{} = {}".format(key, _toml_value(config[key]))
        for key in DEFAULTS
        if config.get(key) is not None
        and (key in chosen or config.get(key) != DEFAULTS[key])
    ]


def _write_config_unlocked(
    path: Path,
    config: Dict[str, Any],
    keys_to_write: Sequence[str],
    keys_to_replace: Optional[Sequence[str]] = None,
) -> None:
    replace = keys_to_write if keys_to_replace is None else keys_to_replace
    original = path.read_text(encoding="utf-8") if path.exists() else ""
    rewritten = _rewrite_config_text(original, config, keys_to_write, replace)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(str(path), str(path.with_name("config.toml.bak")))
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix="config.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(rewritten)
        os.replace(temporary_name, str(path))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass
    print("Note: wizard writes do not preserve comments from hand-edited files.")


def write_config(
    path: Path, config: Dict[str, Any], explicitly_chosen: Sequence[str]
) -> None:
    """Atomically rebase chosen keys while preserving unknown TOML text."""
    with config_lock(path):
        chosen = [
            key
            for key in explicitly_chosen
            if key in DEFAULTS and key in config
        ]
        _write_config_unlocked(path, config, chosen)


def _claude_summary_command() -> List[str]:
    return [
        "claude",
        "-p",
        "--model",
        "haiku",
        ANNOUNCEMENT_PROMPT + " The terminal output is provided on stdin.",
    ]


def _mask_secret(value: str) -> str:
    return "****{}".format(value[-4:]) if value else ""


def _preview_line(line: str) -> str:
    if not line.startswith("elevenlabs_api_key = "):
        return line
    try:
        value = json.loads(line.split("=", 1)[1].strip())
    except (TypeError, ValueError):
        value = ""
    return "elevenlabs_api_key = {}".format(json.dumps(_mask_secret(str(value))))


def _setup_wizard(
    config_dir: Path,
    state_dir: Path,
    write_state: Optional[MutableMapping[str, bool]] = None,
) -> int:
    config_path = config_dir / "config.toml"
    existed = config_path.exists()
    config = load_config(config_dir)
    chosen: List[str] = []
    detected = capabilities()
    fancy = tty_active()

    print(colorize("herdr-announcer setup", "1") if fancy else "herdr-announcer setup")
    print("Config: {}".format(config_path))
    print(
        "{} Ctrl-C exits without writing anything.".format(
            "Arrows move, Enter confirms."
            if fancy
            else "Enter keeps the value in [brackets]."
        )
    )
    print()

    states, changed = ask_multiselect(
        "When should it speak?",
        [
            ("done", "done - an agent finished work you weren't watching"),
            ("blocked", "blocked - an agent is waiting on your input"),
            ("idle", "idle - an agent settled while you were watching"),
            ("working", "working - an agent started doing something (chatty)"),
            ("unknown", "unknown - unrecognized agent activity (chatty)"),
        ],
        [str(item) for item in config["announce"]],
    )
    config["announce"] = states
    if changed:
        chosen.append("announce")

    has_custom_summary = bool(config.get("summary_command"))
    summary_options: List[Tuple[str, str]] = []
    if detected["codex"]:
        summary_options.append(
            ("codex", "Codex - one-sentence summary via codex exec")
        )
    if has_custom_summary:
        summary_options.append(
            ("command", "Custom - keep your current summary command")
        )
    elif detected["claude"]:
        summary_options.append(
            ("command", "Claude Code - one-sentence summary via claude -p")
        )
    summary_options.append(("template", "None - instant fixed phrasing, no LLM"))
    current_summary = str(config.get("summary", ""))
    if current_summary not in {value for value, _unused in summary_options}:
        current_summary = summary_options[0][0]
    summary, changed = ask_select(
        "Who writes the summary sentence?", summary_options, current_summary
    )
    config["summary"] = summary
    if changed:
        chosen.append("summary")
    if summary == "command":
        if not has_custom_summary:
            config["summary_command"] = _claude_summary_command()
        chosen.append("summary_command")
    if summary == "codex":
        model, explicit = ask_text("Codex model", str(config["codex_model"]))
        config["codex_model"] = model
        if explicit:
            chosen.append("codex_model")
        effort, changed = ask_select(
            "Codex reasoning effort",
            [
                ("low", "low - fast, plenty for a one-line summary"),
                ("medium", "medium - a touch more careful"),
                ("high", "high - slow, rarely worth it here"),
            ],
            str(config["codex_effort"]),
        )
        config["codex_effort"] = effort
        if changed:
            chosen.append("codex_effort")

    if summary != "template":
        style, changed = ask_select(
            "How should it sound?",
            [
                (
                    "announcement",
                    'Announcer - "Builder finished the work and tests passed."',
                ),
                ("summary", "Factual - plain report, no radio voice"),
                ("custom", "Custom - write your own prompt"),
            ],
            str(config["style"])
            if str(config["style"]) in ("announcement", "summary", "custom")
            else "announcement",
        )
        config["style"] = style
        if changed:
            chosen.append("style")
        if style == "custom":
            while True:
                prompt, explicit = ask_text(
                    "Prompt template ({agent} {workspace} {status} substituted; "
                    "transcript appended)",
                    str(config["custom_prompt"]),
                )
                if prompt:
                    config["custom_prompt"] = prompt
                    chosen.append("custom_prompt")
                    break
                if not explicit:
                    print("No template given - keeping announcement style.")
                    config["style"] = "announcement"
                    break

    local_names = [
        name
        for name in ("say", "spd-say", "espeak-ng", "espeak")
        if detected[name]
    ]
    detected = ", ".join(local_names) if local_names else "nothing detected!"
    voice_options = []
    if existed:
        voice_options.append(("keep", "Keep current voice settings"))
    voice_options.extend(
        [
            (
                "local",
                "This machine - built-in text-to-speech ({})".format(detected),
            ),
            ("elevenlabs", "ElevenLabs - natural voice, needs an API key"),
            ("custom", "Custom command - ssh somewhere, ntfy push, any script"),
        ]
    )
    backend, backend_changed = ask_select(
        "Where should the voice come out?",
        voice_options,
        "keep" if existed else "local",
    )
    if backend == "local":
        config["speak_command"] = None
        config["elevenlabs_api_key"] = ""
        chosen.extend(("speak_command", "elevenlabs_api_key"))
        if platform.system() == "Darwin":
            voice, explicit = ask_text(
                "macOS voice name (blank = system voice)", str(config["voice"])
            )
            config["voice"] = voice
            if explicit:
                chosen.append("voice")
    elif backend == "elevenlabs":
        current_key = str(config["elevenlabs_api_key"])
        api_key, key_explicit = ask_secret(
            "ElevenLabs API key", current_key, display_default=_mask_secret(current_key)
        )
        if not api_key:
            print("No key entered - ElevenLabs stays inactive; local TTS will be used.")
        voice_id, voice_explicit = ask_text(
            "ElevenLabs voice id", str(config["elevenlabs_voice_id"])
        )
        model, model_explicit = ask_text(
            "ElevenLabs model", str(config["elevenlabs_model"])
        )
        config["speak_command"] = None
        config["elevenlabs_api_key"] = api_key
        config["elevenlabs_voice_id"] = voice_id
        config["elevenlabs_model"] = model
        chosen.extend(("speak_command", "elevenlabs_api_key"))
        if voice_explicit:
            chosen.append("elevenlabs_voice_id")
        if model_explicit:
            chosen.append("elevenlabs_model")
        if key_explicit:
            chosen.append("elevenlabs_api_key")
    elif backend == "custom":
        current = config.get("speak_command")
        current_line = shlex.join(current) if isinstance(current, list) else ""
        while True:
            line, explicit = ask_text(
                "Command ({text} substituted, or announcement on stdin)",
                current_line,
            )
            try:
                command = shlex.split(line)
            except ValueError as error:
                print("Invalid command: {}".format(error))
                continue
            if command:
                config["speak_command"] = command
                config["elevenlabs_api_key"] = ""
                chosen.extend(("speak_command", "elevenlabs_api_key"))
                break
            if not explicit:
                print("No command given - keeping current voice settings.")
                break

    toast, changed = ask_confirm(
        "Also show each announcement as a Herdr notification? "
        "(reaches you over SSH)",
        bool(config["toast"]),
    )
    config["toast"] = toast
    if changed:
        chosen.append("toast")

    while True:
        debounce, explicit = ask_text(
            "Ignore repeats within how many seconds?",
            str(config["debounce_seconds"]),
        )
        try:
            debounce_value = int(debounce)
            if debounce_value < 0:
                raise ValueError
        except ValueError:
            print("Please enter a non-negative integer.")
            continue
        config["debounce_seconds"] = debounce_value
        if explicit:
            chosen.append("debounce_seconds")
        break

    preview = _config_lines(config, chosen)
    print()
    print("About to write {}:".format(config_path))
    if preview:
        for line in preview:
            print("  " + _preview_line(line))
    else:
        print("  (empty file - everything matches the defaults)")
    if existed:
        print("Your current file will be kept as config.toml.bak.")
    write_now, _unused = ask_confirm("Write it?", True)
    if not write_now:
        print("Nothing written.")
        return 0
    write_config(config_path, config, chosen)
    if write_state is not None:
        write_state["written"] = True

    test_voice, _unused = ask_confirm("Test the voice now?", True)
    if test_voice:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            backend_used = speak(
                load_config(config_dir), "Announcer is configured", state_dir
            )
            print("Spoke via: {}".format(backend_used))
        except Exception as error:
            print("Voice test failed: {}".format(error))
    print()
    print("Done. Re-run this wizard anytime; the file is safe to hand-edit too.")
    return 0


def run_setup(config_dir: Path, state_dir: Path) -> int:
    config_path = config_dir / "config.toml"
    backup_path = config_path.with_name("config.toml.bak")
    original = config_path.read_bytes() if config_path.exists() else None
    old_backup = backup_path.read_bytes() if backup_path.exists() else None
    write_state = {"written": False}
    try:
        return _setup_wizard(config_dir, state_dir, write_state)
    except KeyboardInterrupt:
        if write_state["written"]:
            print("\nsetup aborted after write; config was kept")
            return 130
        if original is None:
            try:
                config_path.unlink()
            except FileNotFoundError:
                pass
        elif not config_path.exists() or config_path.read_bytes() != original:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_bytes(original)
        if old_backup is None:
            try:
                backup_path.unlink()
            except FileNotFoundError:
                pass
        elif not backup_path.exists() or backup_path.read_bytes() != old_backup:
            backup_path.write_bytes(old_backup)
        print("\nsetup aborted, nothing written")
        return 130


# Compatibility aliases retained for callers that imported these helpers from
# announce.py or announcer.wizard before they gained public names.
_load_raw_config = load_raw_config


def _write_config(
    path: Path, config: Dict[str, Any], explicitly_chosen: Sequence[str]
) -> None:
    """Preserve the pre-0.9.0 private writer's full-config semantics."""
    with config_lock(path):
        chosen = {
            key
            for key in explicitly_chosen
            if key in DEFAULTS and key in config
        }
        chosen.update(
            key
            for key in DEFAULTS
            if key in config and config.get(key) != DEFAULTS[key]
        )
        replace = chosen | _top_level_known_keys(path)
        _write_config_unlocked(path, config, sorted(chosen), sorted(replace))


__all__ = [
    "_claude_summary_command",
    "_config_lines",
    "_load_raw_config",
    "_mask_secret",
    "_preview_line",
    "_setup_wizard",
    "_toml_value",
    "_write_config",
    "config_lock",
    "load_raw_config",
    "run_setup",
    "write_config",
]
