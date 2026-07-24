#!/usr/bin/env python3
"""Speak short announcements for Herdr agent status changes."""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
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
    "codex_model": "gpt-5.6-luna",
    "codex_effort": "low",
    "codex_timeout_seconds": 45,
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


def codex_summary(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
) -> Optional[str]:
    prompt = (
        "You are the voice announcer for a terminal multiplexer. "
        "An AI coding agent named '{}' in workspace '{}' just changed state "
        "to '{}'. Below is the tail of its terminal output. Write ONE natural "
        "spoken sentence (maximum 25 words) summarizing what happened, suitable "
        "for text-to-speech. Plain words only: no markdown, no code symbols, no "
        "file paths. Lead with the agent name. Reply with the sentence and "
        "nothing else. --- terminal output --- {}"
    ).format(name, workspace, status, transcript)
    command = [
        "codex",
        "exec",
        "-m",
        str(config["codex_model"]),
        "-c",
        "model_reasoning_effort={}".format(config["codex_effort"]),
        "--skip-git-repo-check",
        prompt,
    ]
    try:
        output = _run_text(command, timeout=float(config["codex_timeout_seconds"]))
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in output.splitlines() if line.strip()]
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
) -> str:
    if str(config.get("summary", "")).lower() == "codex" and transcript:
        generated = codex_summary(config, name, workspace, status, transcript)
        if generated:
            return generated
    return template_summary(name, workspace, status)


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
    now = time.time()
    debounce_path = state_dir / "last.json"
    debounce_state = load_debounce_state(debounce_path)
    if is_debounced(debounce_state, pane_id, status, now, debounce_value):
        return "debounced"

    name, workspace = get_context(herdr_bin, pane_id)
    transcript = get_transcript(herdr_bin, pane_id)
    announcement = make_announcement(
        config, name, workspace, status, transcript
    )
    if config.get("toast"):
        show_toast(herdr_bin, announcement)
    save_debounce_state(
        state_dir, debounce_state, pane_id, status, time.time()
    )
    backend = speak(config, announcement, state_dir)
    return "announced+{}".format(backend)


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


def main() -> int:
    started = time.monotonic()
    test_mode = len(sys.argv) == 2 and sys.argv[1] == "--test"
    config_dir = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ".")
    state_dir = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or ".")
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
