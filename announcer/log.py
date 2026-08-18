"""Invocation log writing and backwards-compatible log parsing."""

import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence


LOG_MAX_BYTES = 512 * 1024
LOG_TAIL_BYTES = 256 * 1024
LOG_SCAN_BYTES = 65536
LOG_SCAN_ENTRIES = 200
RECENT_LINES = 5


@dataclass
class LogEntry:
    timestamp: str
    pane_id: str
    status: str
    action: str
    elapsed: float
    reasons: List[str] = field(default_factory=list)
    raw: str = ""


def parse_log_line(line: str) -> Optional[LogEntry]:
    """Parse an invocation record while ignoring tracebacks and future junk."""
    raw = line.rstrip("\n")
    if not raw.strip():
        return None
    # reasons= is the final documented field, and reason parts may contain
    # spaces, so split it off before tokenizing the fixed prefix.
    head, marker, tail = raw.partition(" reasons=")
    reasons: List[str] = []
    if marker:
        reasons = [part.strip() for part in tail.split(";") if part.strip()]
    tokens = head.split()
    if not any(token.startswith("action=") for token in tokens):
        return None
    timestamp = tokens[0] if tokens and "=" not in tokens[0] else ""
    pane_id = "-"
    status = "-"
    action = ""
    elapsed = 0.0
    for token in tokens[1:] if timestamp else tokens:
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        if key == "pane_id":
            pane_id = value
        elif key == "status":
            status = value
        elif key == "action":
            action = value
        elif key == "elapsed":
            try:
                elapsed = float(value)
            except (TypeError, ValueError):
                elapsed = 0.0
        elif key == "reasons" and not reasons:
            reasons = [part for part in value.split(";") if part]
    return LogEntry(
        timestamp=timestamp,
        pane_id=pane_id,
        status=status,
        action=action,
        elapsed=elapsed,
        reasons=reasons,
        raw=raw,
    )


def read_log(path: Path, limit: int = RECENT_LINES) -> List[LogEntry]:
    """Return up to ``limit`` entries, oldest first, from the log tail."""
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            start = max(0, size - LOG_SCAN_BYTES)
            handle.seek(start)
            blob = handle.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if start and lines:
        lines = lines[1:]
    entries: List[LogEntry] = []
    for line in lines:
        entry = parse_log_line(line)
        if entry is not None:
            entries.append(entry)
        if len(entries) >= LOG_SCAN_ENTRIES:
            entries = entries[-LOG_SCAN_ENTRIES:]
    if limit <= 0:
        return []
    return entries[-limit:]


def _log_field(value: str) -> str:
    return " ".join(value.split()) or "-"


def _trim_log(path: Path) -> None:
    try:
        if path.stat().st_size <= LOG_MAX_BYTES:
            return
        with path.open("rb") as handle:
            handle.seek(-min(LOG_TAIL_BYTES, path.stat().st_size), os.SEEK_END)
            tail = handle.read()
    except FileNotFoundError:
        return
    newline = tail.find(b"\n")
    tail = tail[newline + 1 :] if newline >= 0 else b""
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=str(path.parent),
            prefix="announcer.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(tail)
        os.replace(temporary_name, str(path))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def log_invocation(
    state_dir: Path,
    pane_id: str,
    status: str,
    action: str,
    elapsed: float,
    traceback_text: str = "",
    reasons: Optional[Sequence[str]] = None,
) -> None:
    import fcntl

    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    line = "{} pane_id={} status={} action={} elapsed={:.3f}".format(
        timestamp,
        _log_field(pane_id),
        _log_field(status),
        _log_field(action),
        elapsed,
    )
    if reasons:
        line += " reasons={}".format(
            ";".join(_log_field(str(reason)) for reason in reasons)
        )
    line += "\n"
    log_path = state_dir / "announcer.log"
    with (state_dir / "announcer-log.lock").open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            _trim_log(log_path)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                if traceback_text:
                    handle.write(traceback_text)
                    if not traceback_text.endswith("\n"):
                        handle.write("\n")
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "LOG_MAX_BYTES",
    "LOG_SCAN_BYTES",
    "LOG_SCAN_ENTRIES",
    "LOG_TAIL_BYTES",
    "LogEntry",
    "RECENT_LINES",
    "_log_field",
    "_trim_log",
    "log_invocation",
    "parse_log_line",
    "read_log",
]
