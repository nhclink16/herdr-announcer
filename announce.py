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
from announcer.fingerprints import *  # noqa: F401,F403
from announcer.herdr import *  # noqa: F401,F403
from announcer.log import *  # noqa: F401,F403
from announcer.paths import *  # noqa: F401,F403
from announcer.redact import mask_secret, redact_command, redact_command_text
from announcer.speech import *  # noqa: F401,F403
from announcer.snooze import *  # noqa: F401,F403
from announcer.summarize import *  # noqa: F401,F403
from announcer.tui import *  # noqa: F401,F403
from announcer.wizard import *  # noqa: F401,F403


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

    # Snooze is checked AFTER the status filter and BEFORE debounce: an event
    # nobody subscribed to keeps logging the truthful skipped-status, so
    # action=snoozed marks exactly the announcements the snooze silenced.
    if read_snooze(state_dir) > 0.0:
        return "snoozed"

    debounce_value = config.get("debounce_seconds")
    if isinstance(debounce_value, bool) or not isinstance(debounce_value, int):
        raise ValueError("debounce_seconds must be an integer")
    debounced, reservation = reserve_debounce(
        state_dir, pane_id, status, debounce_value
    )
    if debounced:
        return "debounced"

    try:
        name, workspace, active_pane_ids = get_context_with_active_panes(
            herdr_bin, pane_id, reasons
        )
        transcript = summary_transcript(
            get_transcript(herdr_bin, pane_id, reasons)
        )
        fingerprint = content_fingerprint(transcript)
        try:
            duplicate_content = is_duplicate_content(
                state_dir,
                pane_id,
                fingerprint,
                active_pane_ids=active_pane_ids,
            )
        except OSError as error:
            detail = str(error).strip()
            reasons.append(
                "fingerprint: {}".format(
                    detail[:120] if detail else error.__class__.__name__
                )
            )
            duplicate_content = False
        if status == "done" and duplicate_content:
            return "skipped-duplicate"
        generated_announcement, summary_backend = make_announcement(
            config, name, workspace, status, transcript, reasons
        )
        announcement = _sanitize_summary(generated_announcement)

        def deliver() -> str:
            if config.get("toast"):
                show_toast(herdr_bin, announcement, reasons)
            return speak(config, announcement, state_dir, reasons)

        try:
            delivered, backend, fingerprint_error = deliver_content_once(
                state_dir,
                pane_id,
                fingerprint,
                check_duplicate=status == "done",
                deliver=deliver,
            )
        except PlaybackLockTimeout:
            rollback_debounce(state_dir, pane_id, status, reservation)
            return "gave-up-waiting"
        if not delivered:
            return "skipped-duplicate"
        if fingerprint_error is not None:
            detail = str(fingerprint_error).strip()
            reasons.append(
                "fingerprint: {}".format(
                    detail[:120]
                    if detail
                    else fingerprint_error.__class__.__name__
                )
            )
    except Exception:
        rollback_debounce(state_dir, pane_id, status, reservation)
        raise
    return "announced+summary-{}+speak-{}".format(summary_backend, backend)


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
    detected = capabilities()
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
            value = mask_secret(str(value))
        elif key in ("summary_command", "speak_command") and isinstance(value, list):
            value = redact_command([str(argument) for argument in value])
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
        print("  {}: {}".format(name, "yes" if detected[name] else "no"))
    print(
        "unrecognized keys: {}".format(
            ", ".join(unknown_keys) if unknown_keys else "none"
        )
    )
    last_error = _read_last_error(state_dir)
    if last_error is None:
        print("last error: none")
    else:
        detail = _redact_configured_text(";".join(last_error[1]), config)
        print("last error: {} {}".format(last_error[0], detail))
    log_path = state_dir / "announcer.log"
    if log_path.exists():
        print("log (last 8 lines):")
        with log_path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()[-8:]
        for line in lines:
            print("  " + _redact_configured_text(line.rstrip("\n"), config))
    else:
        print("log: not found ({})".format(log_path))
    return 0


def _redact_configured_text(text: str, config: Dict[str, Any]) -> str:
    redacted = text
    for key in ("summary_command", "speak_command"):
        command = config.get(key)
        if isinstance(command, list) and all(
            isinstance(argument, str) for argument in command
        ):
            redacted = redact_command_text(redacted, command)
    api_key = str(config.get("elevenlabs_api_key") or "")
    if api_key:
        redacted = redacted.replace(api_key, mask_secret(api_key))
    return redacted


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

    config_dir, state_dir = resolve_dirs()

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
