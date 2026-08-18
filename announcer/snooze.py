"""Shared snooze state and duration handling."""

import json
import math
import os
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


SNOOZE_STEPS = ("5m", "30m", "2h", "tomorrow", "off")
SNOOZE_HOUR = 8


def snooze_path(state_dir: Path) -> Path:
    return Path(state_dir) / "snooze.json"


def read_snooze(state_dir: Path, now: Optional[float] = None) -> float:
    """Return the active epoch deadline, or zero for off/expired/corrupt."""
    moment = time.time() if now is None else now
    try:
        with snooze_path(state_dir).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        until = float(payload.get("until") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0
    if not math.isfinite(until):
        return 0.0
    return until if until > moment else 0.0


def snooze_active(state_dir: Path, now: Optional[float] = None) -> bool:
    return read_snooze(state_dir, now) > 0.0


def write_snooze(state_dir: Path, until: float) -> None:
    """Atomically write the stable ``{"until": epoch}`` state shape."""
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(directory),
            prefix="snooze.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump({"until": float(until)}, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary_name, str(snooze_path(directory)))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def next_morning(now: Optional[float] = None) -> float:
    moment = time.time() if now is None else now
    local = datetime.fromtimestamp(moment)
    target = local.replace(hour=SNOOZE_HOUR, minute=0, second=0, microsecond=0)
    if target <= local:
        target = (local + timedelta(days=1)).replace(
            hour=SNOOZE_HOUR, minute=0, second=0, microsecond=0
        )
    return target.timestamp()


def parse_duration(spec: str) -> Optional[float]:
    text = str(spec).strip().lower()
    if not text:
        return None
    if text in ("off", "0", "none"):
        return 0.0
    units = {"s": 1, "m": 60, "h": 3600}
    multiplier = units.get(text[-1])
    if multiplier is None:
        multiplier = 1
        number = text
    else:
        number = text[:-1]
    if not number.isdigit():
        return None
    return float(int(number) * multiplier)


def set_snooze(
    state_dir: Path, spec: str, now: Optional[float] = None
) -> Optional[float]:
    moment = time.time() if now is None else now
    if str(spec).strip().lower() == "tomorrow":
        until = next_morning(moment)
        write_snooze(state_dir, until)
        return until
    seconds = parse_duration(spec)
    if seconds is None:
        return None
    if seconds <= 0:
        write_snooze(state_dir, 0.0)
        return 0.0
    until = moment + seconds
    write_snooze(state_dir, until)
    return until


def format_snooze_remaining(
    until: float, now: Optional[float] = None
) -> str:
    moment = time.time() if now is None else now
    try:
        remaining = float(until) - moment
    except (TypeError, ValueError):
        return "off"
    if remaining <= 0:
        return "off"
    total = int(remaining)
    if total >= 3600:
        return "{}h {:02d}m left".format(total // 3600, (total % 3600) // 60)
    if total >= 60:
        return "{}m {:02d}s left".format(total // 60, total % 60)
    return "{}s left".format(total)


def snooze_step(until: float, now: Optional[float] = None) -> str:
    moment = time.time() if now is None else now
    try:
        remaining = float(until) - moment
    except (TypeError, ValueError):
        return "off"
    if not math.isfinite(remaining) or remaining <= 0:
        return "off"
    try:
        stamp = time.localtime(float(until))
    except (OSError, OverflowError, ValueError):
        stamp = None
    if (
        stamp is not None
        and stamp.tm_hour == SNOOZE_HOUR
        and stamp.tm_min == 0
        and stamp.tm_sec == 0
    ):
        return "tomorrow"
    if remaining <= 300:
        return "5m"
    if remaining <= 1800:
        return "30m"
    return "2h"


def next_snooze_step(until: float, now: Optional[float] = None) -> str:
    position = SNOOZE_STEPS.index(snooze_step(until, now))
    return SNOOZE_STEPS[(position + 1) % len(SNOOZE_STEPS)]


def snooze_target_label(until: float) -> str:
    try:
        return "until " + time.strftime("%H:%M", time.localtime(float(until)))
    except (OSError, OverflowError, TypeError, ValueError):
        return "until tomorrow"


def snooze_label(until: float, now: Optional[float] = None) -> str:
    step = snooze_step(until, now)
    if step == "off":
        return "off"
    head = snooze_target_label(until) if step == "tomorrow" else step
    return head + " · " + format_snooze_remaining(until, now)


def snooze_message(until: float, now: Optional[float] = None) -> str:
    step = snooze_step(until, now)
    if step == "off":
        return "snooze off"
    if step == "tomorrow":
        return "snoozed " + snooze_target_label(until)
    return "snoozed · " + format_snooze_remaining(until, now)


__all__ = [
    "SNOOZE_HOUR",
    "SNOOZE_STEPS",
    "format_snooze_remaining",
    "next_morning",
    "next_snooze_step",
    "parse_duration",
    "read_snooze",
    "set_snooze",
    "snooze_active",
    "snooze_label",
    "snooze_message",
    "snooze_path",
    "snooze_step",
    "snooze_target_label",
    "write_snooze",
]
