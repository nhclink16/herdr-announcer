#!/usr/bin/env python3
"""Speak short announcements for Herdr agent status changes."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from announcer.config import *  # noqa: F401,F403
from announcer.herdr import *  # noqa: F401,F403
from announcer.speech import *  # noqa: F401,F403
from announcer.summarize import *  # noqa: F401,F403
from announcer.tui import *  # noqa: F401,F403
from announcer.wizard import *  # noqa: F401,F403


PLUGIN_ID = "nhclink16.announcer"
LOG_MAX_BYTES = 512 * 1024
LOG_TAIL_BYTES = 256 * 1024


def make_announcement(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
    reasons: Optional[List[str]] = None,
) -> Tuple[str, str]:
    mode = str(config.get("summary", "")).lower()
    if mode == "template":
        return template_summary(name, workspace, status), "template"

    generated = None
    if transcript:
        if mode == "codex":
            generated = codex_summary(
                config, name, workspace, status, transcript, reasons
            )
        elif mode == "command":
            generated = command_summary(
                config, name, workspace, status, transcript, reasons
            )
    if generated:
        return generated, mode

    fallback = str(config.get("summary_fallback", "template")).lower()
    if transcript and fallback == "codex" and mode != "codex":
        generated = codex_summary(
            config, name, workspace, status, transcript, reasons
        )
        if generated:
            return generated, "codex-fallback"

    return template_summary(name, workspace, status), "template"


def show_toast(
    herdr_bin: str, text: str, reasons: Optional[List[str]] = None
) -> None:
    """Best-effort Herdr toast; delivery follows the user's ui.toast config."""
    try:
        completed = subprocess.run(
            [herdr_bin, "notification", "show", text],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
        )
        if completed.returncode and reasons is not None:
            reasons.append("toast: exit-{}".format(completed.returncode))
    except (OSError, subprocess.SubprocessError) as error:
        if reasons is not None:
            detail = str(error).strip()
            reasons.append(
                "toast: {}".format(
                    detail[:120] if detail else error.__class__.__name__
                )
            )


def process_invocation(
    config_dir: Path,
    state_dir: Path,
    test_mode: bool,
    log_context: Dict[str, str],
    reasons: Optional[List[str]] = None,
) -> str:
    if reasons is None:
        reasons = []
    config = load_config(config_dir, reasons=reasons)
    herdr_bin = os.environ.get("HERDR_BIN_PATH") or "herdr"
    if test_mode:
        text = "Announcer is working"
        if config.get("toast"):
            show_toast(herdr_bin, text, reasons)
        try:
            backend = speak(config, text, state_dir, reasons)
        except PlaybackLockTimeout:
            return "gave-up-waiting"
        return "announced+{}".format(backend)

    raw_event = os.environ.get("HERDR_PLUGIN_EVENT_JSON")
    if raw_event is None:
        raw_event = sys.stdin.read()
    pane_id, status = parse_event(raw_event)
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

    try:
        name, workspace = get_context(herdr_bin, pane_id, reasons)
        transcript = get_transcript(herdr_bin, pane_id, reasons)
        generated_announcement, summary_backend = make_announcement(
            config, name, workspace, status, transcript, reasons
        )
        announcement = _sanitize_summary(generated_announcement)
        if config.get("toast"):
            show_toast(herdr_bin, announcement, reasons)
        try:
            backend = speak(config, announcement, state_dir, reasons)
        except PlaybackLockTimeout:
            rollback_debounce(state_dir, pane_id, status)
            return "gave-up-waiting"
    except Exception:
        rollback_debounce(state_dir, pane_id, status)
        raise
    return "announced+summary-{}+speak-{}".format(summary_backend, backend)


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


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=path.stem + ".",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(payload, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, str(path))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def persist_last_error(state_dir: Path, reasons: Sequence[str]) -> None:
    if not reasons:
        return
    timestamp = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    _write_json_atomic(
        state_dir / "last-error.json",
        {"timestamp": timestamp, "reasons": list(reasons)},
    )


def _read_last_error(state_dir: Path) -> Optional[Tuple[str, List[str]]]:
    try:
        with (state_dir / "last-error.json").open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("timestamp"), str):
        return None
    reasons = payload.get("reasons")
    if not isinstance(reasons, list) or not all(
        isinstance(reason, str) for reason in reasons
    ):
        return None
    return payload["timestamp"], reasons


def show_status(config_dir: Path, state_dir: Path) -> int:
    config_path = config_dir / "config.toml"
    unknown_keys: List[str] = []
    config = load_config(config_dir, unknown_keys=unknown_keys)
    capabilities = _capabilities()
    print("herdr-announcer status")
    print(
        "config: {} ({})".format(
            config_path, "exists" if config_path.exists() else "missing"
        )
    )
    print("state: {}".format(state_dir))
    print("values:")
    for key in DEFAULTS:
        value = config[key]
        if key == "elevenlabs_api_key" and value:
            value = str(value)[:4] + "..."
        print("  {} = {}".format(key, json.dumps(value)))
    print("capabilities:")
    for name in (
        "codex",
        "claude",
        "say",
        "spd-say",
        "espeak-ng",
        "espeak",
        "mpv",
        "ffplay",
        "afplay",
        "paplay",
        "pw-play",
        "aplay",
    ):
        print("  {}: {}".format(name, "yes" if capabilities[name] else "no"))
    print(
        "unrecognized keys: {}".format(
            ", ".join(unknown_keys) if unknown_keys else "none"
        )
    )
    last_error = _read_last_error(state_dir)
    if last_error is None:
        print("last error: none")
    else:
        print("last error: {} {}".format(last_error[0], ";".join(last_error[1])))
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


def _resolve_dirs_without_env() -> Tuple[Path, Path]:
    """Locate plugin dirs when run from a plain terminal (no Herdr env)."""
    config_dir = ""
    try:
        result = subprocess.run(
            [
                os.environ.get("HERDR_BIN_PATH") or "herdr",
                "plugin",
                "config-dir",
                PLUGIN_ID,
            ],
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


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="test the configured voice")
    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("setup", help="run the setup wizard")
    subcommands.add_parser("status", help="show configuration and diagnostics")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    started = time.monotonic()
    parser = _argument_parser()
    arguments = parser.parse_args(list(argv) if argv is not None else None)
    if arguments.test and arguments.command:
        parser.error("--test cannot be combined with a subcommand")
    if not arguments.test and arguments.command is None and sys.stdin.isatty():
        parser.print_usage(sys.stderr)
        return 2

    config_dir = Path(os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ".")
    state_dir = Path(os.environ.get("HERDR_PLUGIN_STATE_DIR") or ".")
    if (arguments.test or arguments.command in ("setup", "status")) and not os.environ.get(
        "HERDR_PLUGIN_CONFIG_DIR"
    ):
        config_dir, state_dir = _resolve_dirs_without_env()

    if arguments.command == "status":
        try:
            return show_status(config_dir, state_dir)
        except Exception as error:
            sys.stderr.write("announcer error: {}\n".format(error))
            return 1
    if arguments.command == "setup":
        try:
            return run_setup(config_dir, state_dir)
        except Exception as error:
            sys.stderr.write("announcer error: {}\n".format(error))
            return 1

    test_mode = bool(arguments.test)
    log_context = {"pane_id": "-", "status": "test" if test_mode else "-"}
    reasons: List[str] = []
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        action = process_invocation(
            config_dir, state_dir, test_mode, log_context, reasons
        )
        persist_last_error(state_dir, reasons)
        log_invocation(
            state_dir,
            log_context["pane_id"],
            log_context["status"],
            action,
            time.monotonic() - started,
            reasons=reasons,
        )
        return 0
    except Exception as error:
        trace = traceback.format_exc()
        detail = str(error).strip()
        reasons.append(
            "error: {}".format(
                detail[:120] if detail else error.__class__.__name__
            )
        )
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            persist_last_error(state_dir, reasons)
            log_invocation(
                state_dir,
                log_context["pane_id"],
                log_context["status"],
                "error",
                time.monotonic() - started,
                trace,
                reasons,
            )
        except Exception:
            pass
        sys.stderr.write("announcer error: {}\n".format(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
