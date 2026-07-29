#!/usr/bin/env python3
"""Speak short announcements for Herdr agent status changes."""

import json
import os
import platform
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import tomllib
except ImportError:  # Python 3.9 and 3.10
    tomllib = None  # type: ignore


DEFAULTS: Dict[str, Any] = {
    "announce": ["done", "blocked"],
    "debounce_seconds": 30,
    "summary": "codex",
    "summary_fallback": "template",
    "summary_first_activity_timeout_seconds": 5,
    "codex_model": "gpt-5.6-luna",
    "codex_effort": "low",
    "codex_timeout_seconds": 45,
    "summary_command": None,
    "summary_command_timeout_seconds": 60,
    "style": "announcement",
    "custom_prompt": "",
    "speak_command": None,
    "elevenlabs_api_key": "",
    "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
    "elevenlabs_model": "eleven_turbo_v2_5",
    "voice": "",
    "toast": False,
}

VALID_STATUSES = {"idle", "working", "blocked", "done", "unknown"}


def _strip_comment(line: str) -> str:
    """Remove a TOML comment without treating # inside strings as a comment."""
    quote = ""
    escaped = False
    result: List[str] = []
    for char in line:
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            result.append(char)
            escaped = True
            continue
        if char in ('"', "'"):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            result.append(char)
            continue
        if char == "#" and not quote:
            break
        result.append(char)
    return "".join(result).strip()


def _split_array(value: str) -> List[str]:
    inner = value[1:-1].strip()
    if not inner:
        return []
    items: List[str] = []
    current: List[str] = []
    quote = ""
    escaped = False
    for char in inner:
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and quote == '"':
            current.append(char)
            escaped = True
            continue
        if char in ('"', "'"):
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
            current.append(char)
            continue
        if char == "," and not quote:
            items.append(_parse_fallback_value("".join(current).strip()))
            current = []
            continue
        current.append(char)
    if quote:
        raise ValueError("unterminated string in array")
    if current or inner.endswith(","):
        item = "".join(current).strip()
        if item:
            items.append(_parse_fallback_value(item))
    if not all(isinstance(item, str) for item in items):
        raise ValueError("only arrays of strings are supported")
    return items


def _parse_fallback_value(value: str) -> Any:
    if value.startswith("[") and value.endswith("]"):
        return _split_array(value)
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return json.loads(value)
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("unsupported configuration value: {}".format(value)) from exc


def _load_tiny_toml(path: Path) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = _strip_comment(raw_line)
            if not line:
                continue
            if "=" not in line:
                raise ValueError("invalid config line {}".format(line_number))
            key, raw_value = line.split("=", 1)
            key = key.strip()
            raw_value = raw_value.strip()
            if not key or not raw_value:
                raise ValueError("invalid config line {}".format(line_number))
            parsed[key] = _parse_fallback_value(raw_value)
    return parsed


def load_config(config_dir: Path) -> Dict[str, Any]:
    config = dict(DEFAULTS)
    path = config_dir / "config.toml"
    if not path.exists():
        return config
    if tomllib is not None:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    else:
        loaded = _load_tiny_toml(path)
    for key in DEFAULTS:
        if key in loaded:
            config[key] = loaded[key]
    return config


def _find_string_for_key(value: Any, key: str) -> Optional[str]:
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, str):
            return candidate
        for child in value.values():
            found = _find_string_for_key(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_string_for_key(child, key)
            if found is not None:
                return found
    return None


def parse_event(raw_event: str) -> Tuple[str, str]:
    payload = json.loads(raw_event)
    pane_id = _find_string_for_key(payload, "pane_id")
    status_value = _find_string_for_key(payload, "agent_status")
    if not pane_id:
        raise ValueError("event payload has no string pane_id")
    if not status_value:
        raise ValueError("event payload has no string agent_status")
    status = status_value.lower()
    if status not in VALID_STATUSES:
        raise ValueError("event payload has invalid agent_status")
    return pane_id, status


def load_debounce_state(path: Path) -> Dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}


def is_debounced(
    state: Dict[str, Any], pane_id: str, status: str, now: float, seconds: int
) -> bool:
    previous = state.get(pane_id)
    if not isinstance(previous, dict) or previous.get("status") != status:
        return False
    timestamp = previous.get("ts")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return False
    return now - float(timestamp) <= seconds


def save_debounce_state(
    state_dir: Path, state: Dict[str, Any], pane_id: str, status: str, now: float
) -> None:
    state[pane_id] = {"status": status, "ts": now}
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(state_dir),
            prefix="last.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, str(state_dir / "last.json"))
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _run_text(command: Sequence[str], timeout: float = 15) -> str:
    completed = subprocess.run(
        list(command),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
    )
    return completed.stdout


def _records_from_result(payload: Any, key: str) -> List[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result")
    if isinstance(result, dict):
        records = result.get(key)
        if isinstance(records, list):
            return [record for record in records if isinstance(record, dict)]
    records = payload.get(key)
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    return []


def get_context(herdr_bin: str, pane_id: str) -> Tuple[str, str]:
    try:
        agents_payload = json.loads(_run_text([herdr_bin, "agent", "list"]))
        agents = _records_from_result(agents_payload, "agents")
        record = next(
            (item for item in agents if item.get("pane_id") == pane_id), None
        )
        if record is None:
            return "an agent", ""
        name_value = record.get("name")
        kind_value = record.get("agent")
        name = (
            name_value
            if isinstance(name_value, str) and name_value
            else kind_value
            if isinstance(kind_value, str) and kind_value
            else "an agent"
        )
        workspace_id = record.get("workspace_id")
        if not isinstance(workspace_id, str) or not workspace_id:
            return name, ""
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return "an agent", ""

    try:
        workspaces_payload = json.loads(
            _run_text([herdr_bin, "workspace", "list"])
        )
        workspaces = _records_from_result(workspaces_payload, "workspaces")
        workspace = next(
            (
                item
                for item in workspaces
                if item.get("workspace_id") == workspace_id
                or item.get("id") == workspace_id
            ),
            None,
        )
        if workspace is None:
            return name, ""
        for key in ("label", "title", "name"):
            value = workspace.get(key)
            if isinstance(value, str) and value:
                return name, value
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        pass
    return name, ""


def _extract_read_text(raw_output: str) -> str:
    try:
        payload = json.loads(raw_output)
    except ValueError:
        return raw_output
    if isinstance(payload, str):
        return payload

    def find_text(value: Any) -> Optional[str]:
        if isinstance(value, dict):
            for key in ("text", "output", "content", "transcript"):
                candidate = value.get(key)
                if isinstance(candidate, str):
                    return candidate
                if isinstance(candidate, list) and all(
                    isinstance(item, str) for item in candidate
                ):
                    return "\n".join(candidate)
            for child in value.values():
                found = find_text(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_text(child)
                if found is not None:
                    return found
        return None

    return find_text(payload) or ""


def get_transcript(herdr_bin: str, pane_id: str) -> str:
    arguments = [
        pane_id,
        "--source",
        "recent-unwrapped",
        "--lines",
        "100",
    ]
    try:
        output = _run_text([herdr_bin, "agent", "read"] + arguments)
    except (OSError, subprocess.SubprocessError):
        try:
            output = _run_text([herdr_bin, "pane", "read"] + arguments)
        except (OSError, subprocess.SubprocessError):
            return ""
    return _extract_read_text(output)[-4000:]


def template_summary(name: str, workspace: str, status: str) -> str:
    location = " in {}".format(workspace) if workspace else ""
    if status == "done":
        return "{} finished{}.".format(name, location)
    if status == "blocked":
        return "{} needs your input{}.".format(name, location)
    return "{} is now {}{}.".format(name, status, location)


def _sanitize_summary(summary: str) -> str:
    summary = summary.translate(str.maketrans("", "", "`*_#'\""))
    words = summary.split()
    return " ".join(words[:40])


ANNOUNCEMENT_PROMPT = (
    "You are the voice announcer for a terminal multiplexer. "
    "An AI coding agent named '{agent}' in workspace '{workspace}' just "
    "changed state to '{status}'. Below is the tail of its terminal output. "
    "Write ONE natural spoken sentence (maximum 25 words) summarizing what "
    "happened, suitable for text-to-speech. Plain words only: no markdown, no "
    "code symbols, no file paths. Lead with the agent name. Reply with the "
    "sentence and nothing else."
)

SUMMARY_PROMPT = (
    "An AI coding agent named '{agent}' in workspace '{workspace}' just "
    "changed state to '{status}'. Below is the tail of its terminal output. "
    "Write ONE factual sentence (maximum 25 words) stating what the agent did "
    "and the outcome, suitable for text-to-speech. Plain words only: no "
    "markdown, no code symbols, no file paths. Reply with the sentence and "
    "nothing else."
)


def build_prompt(
    config: Dict[str, Any], name: str, workspace: str, status: str
) -> str:
    style = str(config.get("style", "announcement")).lower()
    custom = config.get("custom_prompt")
    if style == "custom" and isinstance(custom, str) and custom:
        template = custom
    elif style == "summary":
        template = SUMMARY_PROMPT
    else:
        template = ANNOUNCEMENT_PROMPT
    for placeholder, value in (
        ("{agent}", name),
        ("{workspace}", workspace),
        ("{status}", status),
    ):
        template = template.replace(placeholder, value)
    return template


def codex_summary(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
) -> Optional[str]:
    prompt = "{} --- terminal output --- {}".format(
        build_prompt(config, name, workspace, status), transcript
    )
    command = [
        "codex",
        "exec",
        "--json",
        "-m",
        str(config["codex_model"]),
        "-c",
        "model_reasoning_effort={}".format(config["codex_effort"]),
        # The transcript is untrusted agent output; never let a prompt-injected
        # transcript run tools, and don't persist these throwaway sessions.
        "--sandbox",
        "read-only",
        "--ephemeral",
        "--skip-git-repo-check",
        prompt,
    ]
    process = None
    stdout_messages: "queue.Queue[Optional[str]]" = queue.Queue()
    stderr_lines: List[str] = []
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        if process.stdout is None or process.stderr is None:
            return None
        threading.Thread(
            target=_read_stream_lines,
            args=(process.stdout, stdout_messages),
            daemon=True,
        ).start()
        threading.Thread(
            target=_drain_stream,
            args=(process.stderr, stderr_lines),
            daemon=True,
        ).start()
        output = _collect_codex_summary(
            stdout_messages,
            first_activity_timeout=float(
                config["summary_first_activity_timeout_seconds"]
            ),
            overall_timeout=float(config["codex_timeout_seconds"]),
        )
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, TimeoutError):
        return None
    finally:
        if process is not None:
            _stop_subprocess(process)
    if not output:
        return None
    sanitized = _sanitize_summary(output)
    return sanitized or None


CODEX_MODEL_ACTIVITY_ITEMS = {
    "agent_message",
    "reasoning",
    "command_execution",
    "mcp_tool_call",
    "web_search",
}


def _read_stream_lines(stream: Any, messages: "queue.Queue[Optional[str]]") -> None:
    try:
        for line in iter(stream.readline, ""):
            messages.put(line)
    finally:
        messages.put(None)


def _drain_stream(stream: Any, lines: List[str]) -> None:
    for line in iter(stream.readline, ""):
        if sum(len(item) for item in lines) < 4000:
            lines.append(line)


def _stop_subprocess(process: Any) -> None:
    if process.poll() is not None:
        return
    try:
        process.terminate()
    except OSError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            pass


def _collect_codex_summary(
    messages: "queue.Queue[Optional[str]]",
    first_activity_timeout: float,
    overall_timeout: float,
) -> Optional[str]:
    started_at = time.monotonic()
    first_activity_deadline = started_at + first_activity_timeout
    overall_deadline = started_at + overall_timeout
    activity_seen = False
    last_message = ""

    while True:
        now = time.monotonic()
        deadline = overall_deadline if activity_seen else min(
            first_activity_deadline, overall_deadline
        )
        remaining = deadline - now
        if remaining <= 0:
            if not activity_seen and deadline == first_activity_deadline:
                raise TimeoutError("Codex produced no model activity")
            raise TimeoutError("Codex summary timed out")
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty:
            if not activity_seen and time.monotonic() >= first_activity_deadline:
                raise TimeoutError("Codex produced no model activity")
            raise TimeoutError("Codex summary timed out")
        if line is None:
            return last_message or None
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        item = event.get("item")
        item_type = item.get("type") if isinstance(item, dict) else None
        if (
            event_type in ("item.started", "item.completed")
            and item_type in CODEX_MODEL_ACTIVITY_ITEMS
        ):
            activity_seen = True
        if event_type == "item.completed" and item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                last_message = text.strip()
        if event_type == "turn.completed":
            return last_message or None
        if event_type in ("turn.failed", "error"):
            return None


def command_summary(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
) -> Optional[str]:
    command_value = config.get("summary_command")
    if not isinstance(command_value, list) or not all(
        isinstance(argument, str) for argument in command_value
    ) or not command_value:
        return None
    substitutions = {
        "{agent}": name,
        "{workspace}": workspace,
        "{status}": status,
    }
    command = []
    for argument in command_value:
        for placeholder, value in substitutions.items():
            argument = argument.replace(placeholder, value)
        command.append(argument)
    environment = os.environ.copy()
    environment["HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS"] = str(
        config["summary_first_activity_timeout_seconds"]
    )
    environment["HERDR_SUMMARY_OVERALL_TIMEOUT_SECONDS"] = str(
        config["summary_command_timeout_seconds"]
    )
    try:
        completed = subprocess.run(
            command,
            check=True,
            input=transcript,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=float(config["summary_command_timeout_seconds"]),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    sanitized = _sanitize_summary(lines[-1])
    return sanitized or None


def make_announcement(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
) -> Tuple[str, str]:
    mode = str(config.get("summary", "")).lower()
    generated = None
    if transcript:
        if mode == "codex":
            generated = codex_summary(config, name, workspace, status, transcript)
        elif mode == "command":
            generated = command_summary(config, name, workspace, status, transcript)
    if generated:
        return generated, mode

    fallback = str(config.get("summary_fallback", "template")).lower()
    if transcript and fallback == "codex" and mode != "codex":
        generated = codex_summary(config, name, workspace, status, transcript)
        if generated:
            return generated, "codex-fallback"

    return template_summary(name, workspace, status), "template"


def check_and_record_debounce(
    state_dir: Path, pane_id: str, status: str, seconds: int
) -> bool:
    """Atomically check and record the announcement, so two hooks racing on
    the same event can't both pass the debounce window."""
    import fcntl

    with (state_dir / "debounce.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            now = time.time()
            state = load_debounce_state(state_dir / "last.json")
            if is_debounced(state, pane_id, status, now, seconds):
                return True
            save_debounce_state(state_dir, state, pane_id, status, now)
            return False
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def playback_lock(state_dir: Path) -> Iterator[None]:
    import fcntl

    with (state_dir / "speak.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_custom_speech(command_value: Any, text: str) -> str:
    if not isinstance(command_value, list) or not all(
        isinstance(argument, str) for argument in command_value
    ):
        raise ValueError("speak_command must be an argv array of strings")
    if not command_value:
        raise ValueError("speak_command must not be empty")
    used_placeholder = any("{text}" in argument for argument in command_value)
    command = [argument.replace("{text}", text) for argument in command_value]
    subprocess.run(
        command,
        check=True,
        input=None if used_placeholder else text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=60,
    )
    return "command"


def synthesize_elevenlabs(
    config: Dict[str, Any], text: str, state_dir: Path
) -> Path:
    voice_id = urllib.parse.quote(str(config["elevenlabs_voice_id"]), safe="")
    url = (
        "https://api.elevenlabs.io/v1/text-to-speech/{}"
        "?output_format=mp3_44100_128"
    ).format(voice_id)
    body = json.dumps(
        {"text": text, "model_id": str(config["elevenlabs_model"])}
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "xi-api-key": str(config["elevenlabs_api_key"]),
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        audio = response.read()
    if not audio:
        raise ValueError("ElevenLabs returned no audio")
    with tempfile.NamedTemporaryFile(
        mode="wb", dir=str(state_dir), suffix=".mp3", delete=False
    ) as handle:
        handle.write(audio)
        return Path(handle.name)


def play_audio_file(path: Path) -> str:
    if platform.system() == "Darwin":
        command = ["afplay", str(path)]
    elif shutil.which("mpv"):
        command = ["mpv", "--no-video", str(path)]
    elif shutil.which("ffplay"):
        command = ["ffplay", "-nodisp", "-autoexit", str(path)]
    else:
        raise FileNotFoundError("no MP3 player found")
    subprocess.run(
        command,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    return "elevenlabs"


def run_local_speech(config: Dict[str, Any], text: str) -> str:
    system = platform.system()
    if system == "Darwin":
        command = ["say"]
        voice = config.get("voice")
        if isinstance(voice, str) and voice:
            command.extend(["-v", voice])
        command.append(text)
        subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        return "say"
    if system == "Linux":
        try:
            subprocess.run(
                ["spd-say", "-w", text],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            return "spd-say"
        except (OSError, subprocess.SubprocessError):
            subprocess.run(
                ["espeak", text],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=60,
            )
            return "espeak"
    raise OSError("local text-to-speech is unsupported on {}".format(system))


def speak(config: Dict[str, Any], text: str, state_dir: Path) -> str:
    if config.get("speak_command") is not None:
        with playback_lock(state_dir):
            return run_custom_speech(config["speak_command"], text)

    if config.get("elevenlabs_api_key"):
        audio_path: Optional[Path] = None
        try:
            audio_path = synthesize_elevenlabs(config, text, state_dir)
            with playback_lock(state_dir):
                return play_audio_file(audio_path)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            pass
        finally:
            if audio_path is not None:
                try:
                    audio_path.unlink()
                except FileNotFoundError:
                    pass

    with playback_lock(state_dir):
        return run_local_speech(config, text)


def _capabilities() -> Dict[str, Optional[str]]:
    return {
        name: shutil.which(name)
        for name in ("codex", "claude", "say", "spd-say", "espeak")
    }


def show_status(config_dir: Path, state_dir: Path) -> int:
    config_path = config_dir / "config.toml"
    config = load_config(config_dir)
    capabilities = _capabilities()
    print("herdr-announcer status")
    print("config: {} ({})".format(
        config_path, "exists" if config_path.exists() else "missing"
    ))
    print("values:")
    for key in DEFAULTS:
        value = config[key]
        if key == "elevenlabs_api_key" and value:
            value = str(value)[:4] + "..."
        print("  {} = {}".format(key, json.dumps(value)))
    print("capabilities:")
    for name in ("codex", "claude", "say", "spd-say", "espeak"):
        print("  {}: {}".format(name, "yes" if capabilities[name] else "no"))
    log_path = state_dir / "announcer.log"
    if log_path.exists():
        print("log (last 8 lines):")
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-8:]
        for line in lines:
            print("  " + line.rstrip("\n"))
    else:
        print("log: not found ({})".format(log_path))
    return 0


# ---- clack-style interactive widgets (stdlib only, TTY with line fallback) --

try:
    import termios
    import tty
except ImportError:  # non-Unix
    termios = None  # type: ignore
    tty = None  # type: ignore
import select as _select_mod


def _tty_active() -> bool:
    return (
        termios is not None
        and sys.stdin.isatty()
        and sys.stdout.isatty()
    )


def _c(text: str, code: str) -> str:
    return "\x1b[{}m{}\x1b[0m".format(code, text)


@contextmanager
def _raw_mode() -> Iterator[None]:
    """Hold raw mode for a whole widget loop so buffered keys never get
    cooked-mode line buffering between reads."""
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _read_key() -> str:
    """Read one keypress; assumes raw mode. Arrows return 'up'/'down'."""
    fd = sys.stdin.fileno()
    ch = os.read(fd, 1).decode("utf-8", "ignore")
    if ch == "\x1b":
        ready, _unused, _unused2 = _select_mod.select([fd], [], [], 0.05)
        if not ready:
            return "esc"
        seq = os.read(fd, 2).decode("utf-8", "ignore")
        if seq in ("[A", "OA"):
            return "up"
        if seq in ("[B", "OB"):
            return "down"
        return "esc"
    return ch


def _frame(lines: List[str], previous_height: int) -> int:
    """Redraw a widget frame in place; returns the new frame height."""
    out = sys.stdout
    if previous_height:
        out.write("\x1b[{}A".format(previous_height))
    for line in lines:
        out.write("\r\x1b[2K" + line + "\n")
    out.flush()
    return len(lines)


def _collapse(height: int, title: str, answer: str) -> None:
    sys.stdout.write("\x1b[{}A".format(height))
    for _unused in range(height):
        sys.stdout.write("\r\x1b[2K\n")
    sys.stdout.write("\x1b[{}A".format(height))
    sys.stdout.write(
        "\r\x1b[2K" + _c("◇", "90") + " " + _c(title, "90")
        + _c(" · ", "90") + _c(answer, "36") + "\n"
    )
    sys.stdout.flush()


def _hide_cursor() -> None:
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()


def _show_cursor() -> None:
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()


def ask_select(
    title: str,
    options: Sequence[Tuple[str, str]],
    default_value: str,
    hint: str = "",
) -> Tuple[str, bool]:
    """options: (value, label). Returns (value, changed-from-default)."""
    if not _tty_active():
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
    _hide_cursor()
    try:
      with _raw_mode():
        while True:
            lines = [_c("◆", "36") + " " + _c(title, "1")]
            if hint:
                lines.append(_c("│", "90") + "  " + _c(hint, "90"))
            for i, (_unused, label) in enumerate(options):
                if i == index:
                    lines.append(
                        _c("│", "90") + "  "
                        + _c("●", "36") + " " + _c(label, "1")
                    )
                else:
                    lines.append(
                        _c("│", "90") + "  "
                        + _c("○ " + label, "90")
                    )
            lines.append(_c("└", "90"))
            height = _frame(lines, height)
            key = _read_key()
            if key in ("up", "k"):
                index = (index - 1) % len(options)
            elif key in ("down", "j", "\t"):
                index = (index + 1) % len(options)
            elif key.isdigit() and 1 <= int(key) <= len(options):
                index = int(key) - 1
            elif key in ("\r", "\n"):
                value, label = options[index]
                _collapse(height, title, label.split("  ")[0].strip())
                return value, value != default_value
            elif key == "\x03":
                raise KeyboardInterrupt
    finally:
        _show_cursor()


def ask_multiselect(
    title: str,
    options: Sequence[Tuple[str, str]],
    default_selected: Sequence[str],
    hint: str = "space toggles, enter confirms",
) -> Tuple[List[str], bool]:
    if not _tty_active():
        while True:
            value, explicit = _prompt(
                title + " (comma list)", ",".join(default_selected)
            )
            picked = [v.strip().lower() for v in value.split(",") if v.strip()]
            valid = {v for v, _unused in options}
            if picked and all(v in valid for v in picked):
                return picked, explicit
            print("  Choose from: {}.".format(", ".join(sorted(valid))))

    selected = {value for value in default_selected}
    index = 0
    height = 0
    _hide_cursor()
    try:
      with _raw_mode():
        while True:
            lines = [_c("◆", "36") + " " + _c(title, "1")]
            lines.append(_c("│", "90") + "  " + _c(hint, "90"))
            for i, (value, label) in enumerate(options):
                box = _c("◼", "36") if value in selected else _c("◻", "90")
                cursor = _c("❯", "36") if i == index else " "
                text = _c(label, "1") if i == index else _c(label, "90")
                lines.append(
                    _c("│", "90") + " " + cursor + box + " " + text
                )
            lines.append(_c("└", "90"))
            height = _frame(lines, height)
            key = _read_key()
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
                _collapse(height, title, ", ".join(ordered))
                return ordered, set(ordered) != set(default_selected)
            elif key == "\x03":
                raise KeyboardInterrupt
    finally:
        _show_cursor()


def ask_confirm(title: str, default: bool) -> Tuple[bool, bool]:
    if not _tty_active():
        return _prompt_yes_no(title, default)
    value, changed = ask_select(
        title,
        [("yes", "Yes"), ("no", "No")],
        "yes" if default else "no",
    )
    result = value == "yes"
    return result, result != default


def ask_text(
    title: str, default: str, display_default: Optional[str] = None
) -> Tuple[str, bool]:
    """Line input, styled on a TTY. Enter keeps the default."""
    if not _tty_active():
        value, explicit = _prompt(title, display_default or default)
        if display_default is not None and value == display_default:
            return default, False
        return value, explicit
    shown = display_default if display_default is not None else default
    print(_c("◆", "36") + " " + _c(title, "1"))
    try:
        raw = input(
            _c("│", "90") + "  " + _c("[{}]".format(shown), "90") + " > "
        )
    except EOFError:
        raw = ""
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
    _collapse(2, title, str(summary) if summary else "(blank)")
    return result, explicit


def _prompt(label: str, default: str) -> Tuple[str, bool]:
    try:
        value = input("{} [{}]: ".format(label, default))
    except EOFError:
        return default, False
    value = value.strip()
    return (value, True) if value else (default, False)


def _prompt_choice(
    label: str, choices: Dict[str, str], default: str
) -> Tuple[str, bool]:
    while True:
        value, explicit = _prompt(label, default)
        selected = choices.get(value.lower())
        if selected is not None:
            return selected, explicit
        print("Please choose {}.".format(", ".join(choices)))


def _prompt_yes_no(label: str, default: bool) -> Tuple[bool, bool]:
    while True:
        value, explicit = _prompt(label, "Y/n" if default else "y/N")
        if not explicit:
            return default, False
        if value.lower() in ("y", "yes"):
            return True, True
        if value.lower() in ("n", "no"):
            return False, True
        print("Please enter yes or no.")


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


def _load_raw_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if tomllib is not None:
            with path.open("rb") as handle:
                return tomllib.load(handle)
        return _load_tiny_toml(path)
    except (OSError, ValueError):
        return {}


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


def _write_config(
    path: Path, config: Dict[str, Any], explicitly_chosen: Sequence[str]
) -> None:
    lines = _config_lines(config, explicitly_chosen)
    # Carry unknown keys forward so future plugin versions' settings survive.
    extras = {
        key: value
        for key, value in _load_raw_config(path).items()
        if key not in DEFAULTS
    }
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
            for line in lines:
                handle.write(line + "\n")
            for key, value in extras.items():
                try:
                    handle.write("{} = {}\n".format(key, _toml_value(value)))
                except ValueError:
                    pass
        os.replace(temporary_name, str(path))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _claude_summary_command() -> List[str]:
    return [
        "claude",
        "-p",
        "--model",
        "haiku",
        (
            "One spoken sentence, maximum 25 words, plain words only, no "
            "markdown or file paths, leading with the agent name {agent} "
            "which just changed to {status} in workspace {workspace}: "
            "summarize the agent terminal output on stdin for a voice "
            "announcement. Reply with only the sentence."
        ),
    ]


def _setup_wizard(config_dir: Path, state_dir: Path) -> int:
    config_path = config_dir / "config.toml"
    existed = config_path.exists()
    config = load_config(config_dir)
    chosen: List[str] = []
    capabilities = _capabilities()
    fancy = _tty_active()

    print(_c("herdr-announcer setup", "1") if fancy else "herdr-announcer setup")
    print("Config: {}".format(config_path))
    print("{} Ctrl-C exits without writing anything.".format(
        "Arrows move, Enter confirms." if fancy
        else "Enter keeps the value in [brackets]."
    ))
    print()

    # 1 -- states
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

    # 2 -- summarizer
    has_custom_summary = bool(config.get("summary_command"))
    options: List[Tuple[str, str]] = []
    if capabilities["codex"]:
        options.append(("codex", "Codex - one-sentence summary via codex exec"))
    if has_custom_summary:
        options.append(("command", "Custom - keep your current summary command"))
    elif capabilities["claude"]:
        options.append(("command", "Claude Code - one-sentence summary via claude -p"))
    options.append(("template", "None - instant fixed phrasing, no LLM"))
    current_summary = str(config.get("summary", ""))
    if current_summary not in {value for value, _unused in options}:
        current_summary = options[0][0]
    summary, changed = ask_select(
        "Who writes the summary sentence?", options, current_summary
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

    # 3 -- style
    if summary != "template":
        style, changed = ask_select(
            "How should it sound?",
            [
                ("announcement", 'Announcer - "Builder finished the work and tests passed."'),
                ("summary", "Factual - plain report, no radio voice"),
                ("custom", "Custom - write your own prompt"),
            ],
            str(config["style"]) if str(config["style"]) in
            ("announcement", "summary", "custom") else "announcement",
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

    # 4 -- voice
    local_names = [
        name for name in ("say", "spd-say", "espeak") if capabilities[name]
    ]
    detected = ", ".join(local_names) if local_names else "nothing detected!"
    backend, backend_changed = ask_select(
        "Where should the voice come out?",
        [
            ("keep", "Keep current voice settings"),
            ("local", "This machine - built-in text-to-speech ({})".format(detected)),
            ("elevenlabs", "ElevenLabs - natural voice, needs an API key"),
            ("custom", "Custom command - ssh somewhere, ntfy push, any script"),
        ],
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
        masked = current_key[:4] + "..." if current_key else ""
        api_key, key_explicit = ask_text(
            "ElevenLabs API key", current_key, display_default=masked
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
            except ValueError as exc:
                print("Invalid command: {}".format(exc))
                continue
            if command:
                config["speak_command"] = command
                config["elevenlabs_api_key"] = ""
                chosen.extend(("speak_command", "elevenlabs_api_key"))
                break
            if not explicit:
                print("No command given - keeping current voice settings.")
                break

    # 5 -- toast
    toast, changed = ask_confirm(
        "Also show each announcement as a Herdr notification? "
        "(reaches you over SSH)",
        bool(config["toast"]),
    )
    config["toast"] = toast
    if changed:
        chosen.append("toast")

    # 6 -- debounce
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

    # preview + confirm
    preview = _config_lines(config, chosen)
    print()
    print("About to write {}:".format(config_path))
    if preview:
        for line in preview:
            print("  " + line)
    else:
        print("  (empty file - everything matches the defaults)")
    if existed:
        print("Your current file will be kept as config.toml.bak.")
    write_now, _unused = ask_confirm("Write it?", True)
    if not write_now:
        print("Nothing written.")
        return 0
    _write_config(config_path, config, chosen)

    test_voice, _unused = ask_confirm("Test the voice now?", True)
    if test_voice:
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            backend_used = speak(
                load_config(config_dir), "Announcer is configured", state_dir
            )
            print("Spoke via: {}".format(backend_used))
        except Exception as exc:
            print("Voice test failed: {}".format(exc))
    print()
    print("Done. Re-run this wizard anytime; the file is safe to hand-edit too.")
    return 0


def run_setup(config_dir: Path, state_dir: Path) -> int:
    config_path = config_dir / "config.toml"
    backup_path = config_path.with_name("config.toml.bak")
    original = config_path.read_bytes() if config_path.exists() else None
    old_backup = backup_path.read_bytes() if backup_path.exists() else None
    try:
        return _setup_wizard(config_dir, state_dir)
    except KeyboardInterrupt:
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


def show_toast(herdr_bin: str, text: str) -> None:
    """Best-effort Herdr toast; delivery follows the user's ui.toast config."""
    try:
        subprocess.run(
            [herdr_bin, "notification", "show", text],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def process_invocation(
    config_dir: Path,
    state_dir: Path,
    test_mode: bool,
    log_context: Dict[str, str],
) -> str:
    config = load_config(config_dir)
    herdr_bin = os.environ.get("HERDR_BIN_PATH") or "herdr"
    if test_mode:
        text = "Announcer is working"
        if config.get("toast"):
            show_toast(herdr_bin, text)
        backend = speak(config, text, state_dir)
        return "announced+{}".format(backend)

    pane_id, status = parse_event(os.environ.get("HERDR_PLUGIN_EVENT_JSON", ""))
    log_context["pane_id"] = pane_id
    log_context["status"] = status

    configured_statuses = config.get("announce")
    if not isinstance(configured_statuses, list):
        raise ValueError("announce must be an array of strings")
    announce_statuses = {
        value.lower() for value in configured_statuses if isinstance(value, str)
    }
    if status not in announce_statuses:
        return "skipped-status"

    debounce_value = config.get("debounce_seconds")
    if isinstance(debounce_value, bool) or not isinstance(debounce_value, int):
        raise ValueError("debounce_seconds must be an integer")
    if check_and_record_debounce(state_dir, pane_id, status, debounce_value):
        return "debounced"

    name, workspace = get_context(herdr_bin, pane_id)
    transcript = get_transcript(herdr_bin, pane_id)
    generated_announcement, summary_backend = make_announcement(
        config, name, workspace, status, transcript
    )
    announcement = _sanitize_summary(generated_announcement)
    if config.get("toast"):
        show_toast(herdr_bin, announcement)
    backend = speak(config, announcement, state_dir)
    return "announced+summary-{}+speak-{}".format(summary_backend, backend)


def _log_field(value: str) -> str:
    return " ".join(value.split()) or "-"


def log_invocation(
    state_dir: Path,
    pane_id: str,
    status: str,
    action: str,
    elapsed: float,
    traceback_text: str = "",
) -> None:
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = (
        "{} pane_id={} status={} action={} elapsed={:.3f}\n".format(
            timestamp,
            _log_field(pane_id),
            _log_field(status),
            _log_field(action),
            elapsed,
        )
    )
    with (state_dir / "announcer.log").open("a", encoding="utf-8") as handle:
        handle.write(line)
        if traceback_text:
            handle.write(traceback_text)
            if not traceback_text.endswith("\n"):
                handle.write("\n")


PLUGIN_ID = "nhclink16.announcer"


def _resolve_dirs_without_env() -> Tuple[Path, Path]:
    """Locate plugin dirs when run from a plain terminal (no Herdr env)."""
    config_dir = ""
    try:
        result = subprocess.run(
            [os.environ.get("HERDR_BIN_PATH") or "herdr",
             "plugin", "config-dir", PLUGIN_ID],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            config_dir = result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if not config_dir:
        config_dir = str(
            Path.home() / ".config" / "herdr" / "plugins" / "config" / PLUGIN_ID
        )
    state_dir = Path.home() / ".local" / "state" / "herdr" / "plugins" / PLUGIN_ID
    return Path(config_dir), state_dir


def main() -> int:
    started = time.monotonic()
    test_mode = len(sys.argv) == 2 and sys.argv[1] == "--test"
    config_dir = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ".")
    state_dir = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or ".")
    if sys.argv[1:2] in (["setup"], ["status"]) and not os.environ.get(
        "HERDR_PLUGIN_CONFIG_DIR"
    ):
        config_dir, state_dir = _resolve_dirs_without_env()
    if len(sys.argv) == 2 and sys.argv[1] == "status":
        try:
            return show_status(config_dir, state_dir)
        except Exception as exc:
            sys.stderr.write("announcer error: {}\n".format(exc))
            return 1
    if len(sys.argv) == 2 and sys.argv[1] == "setup":
        try:
            return run_setup(config_dir, state_dir)
        except Exception as exc:
            sys.stderr.write("announcer error: {}\n".format(exc))
            return 1
    log_context = {
        "pane_id": "-",
        "status": "test" if test_mode else "-",
    }
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        action = process_invocation(
            config_dir, state_dir, test_mode, log_context
        )
        log_invocation(
            state_dir,
            log_context["pane_id"],
            log_context["status"],
            action,
            time.monotonic() - started,
        )
        return 0
    except Exception as exc:
        trace = traceback.format_exc()
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            log_invocation(
                state_dir,
                log_context["pane_id"],
                log_context["status"],
                "error",
                time.monotonic() - started,
                trace,
            )
        except Exception:
            pass
        sys.stderr.write("announcer error: {}\n".format(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
