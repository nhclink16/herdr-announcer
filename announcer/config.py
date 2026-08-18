"""Configuration loading for herdr-announcer."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

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


def _load_tiny_toml(
    path: Path, reasons: Optional[List[str]] = None
) -> Dict[str, Any]:
    parsed: Dict[str, Any] = {}
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = _strip_comment(raw_line)
            if not line:
                continue
            try:
                if "=" not in line:
                    raise ValueError
                key, raw_value = line.split("=", 1)
                key = key.strip()
                raw_value = raw_value.strip()
                if not key or not raw_value:
                    raise ValueError
                parsed[key] = _parse_fallback_value(raw_value)
            except (TypeError, ValueError):
                if reasons is not None:
                    reasons.append("config: skipped line {}".format(line_number))
    return parsed


def load_config(
    config_dir: Path,
    reasons: Optional[List[str]] = None,
    unknown_keys: Optional[List[str]] = None,
) -> Dict[str, Any]:
    config = dict(DEFAULTS)
    path = config_dir / "config.toml"
    if not path.exists():
        return config
    if tomllib is not None:
        with path.open("rb") as handle:
            loaded = tomllib.load(handle)
    else:
        loaded = _load_tiny_toml(path, reasons)
    if unknown_keys is not None:
        unknown_keys.extend(sorted(key for key in loaded if key not in DEFAULTS))
    for key in DEFAULTS:
        if key in loaded:
            config[key] = loaded[key]
    return config


__all__ = [
    "DEFAULTS",
    "_load_tiny_toml",
    "_parse_fallback_value",
    "_split_array",
    "_strip_comment",
    "load_config",
    "tomllib",
]
