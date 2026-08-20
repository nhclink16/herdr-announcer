"""Announcement prompt and summary backends."""

import json
import math
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from .deadline import TwoPhaseDeadline, drain_lines, read_lines, stop_subprocess
from .redact import redact_command_text


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

CODEX_MODEL_ACTIVITY_ITEMS = {
    "agent_message",
    "reasoning",
    "command_execution",
    "mcp_tool_call",
    "web_search",
}

CODEX_STATUS_LINE = re.compile(
    r"(?:\bContext \d{1,3}% used\b|\b\d{1,3}% context left\b|"
    r"\bweekly \d{1,3}% left\b)",
    re.IGNORECASE,
)
CODEX_PROGRESS_TIMER = re.compile(
    r"\((?:(?:\d+h )?\d+m )?\d+s(?= [·•] esc to interrupt\))"
)


def summary_transcript(raw_transcript: str) -> str:
    """Return stable terminal content for both summarization and dedupe."""
    content_lines = []
    for line in raw_transcript.splitlines():
        if CODEX_STATUS_LINE.search(line):
            continue
        content_lines.append(CODEX_PROGRESS_TIMER.sub("(<elapsed>", line))
    return "\n".join(content_lines).rstrip()


def _positive_finite_timeout(value: Any) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than zero")
    return timeout


def template_summary(name: str, workspace: str, status: str) -> str:
    location = " in {}".format(workspace) if workspace else ""
    if status == "done":
        return "{} finished{}.".format(name, location)
    if status == "blocked":
        return "{} needs your input{}.".format(name, location)
    return "{} is now {}{}.".format(name, status, location)


def _sanitize_summary(summary: str) -> str:
    allowed = " ,.!?-"
    cleaned = "".join(
        char if char.isalpha() or char.isdigit() or char in allowed else " "
        for char in summary
    )
    return " ".join(cleaned.split()[:40])


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


def _read_stream_lines(stream: Any, messages: Any) -> None:
    read_lines(stream, messages)


def _drain_stream(stream: Any, lines: List[str]) -> None:
    drain_lines(stream, lines)


def _stop_subprocess(process: Any) -> None:
    stop_subprocess(process)


def _last_stderr(lines: List[str]) -> str:
    nonempty = [line.strip() for line in lines if line.strip()]
    return nonempty[-1][:120] if nonempty else ""


def _append_codex_reason(
    reasons: Optional[List[str]], cause: str, stderr_lines: List[str]
) -> None:
    if reasons is None:
        return
    reason = "codex: {}".format(cause)
    detail = _last_stderr(stderr_lines)
    if detail:
        reason += " " + detail
    reasons.append(reason)


def _collect_codex_summary(
    messages: Any,
    first_activity_deadline: float,
    completion_timeout: float,
    reasons: Optional[List[str]] = None,
) -> Optional[str]:
    deadline = TwoPhaseDeadline(
        first_activity_deadline=first_activity_deadline,
        completion_timeout=completion_timeout,
        first_timeout_message="Codex produced no model activity",
        completion_timeout_message="Codex summary timed out",
    )
    last_message = ""

    while True:
        line = deadline.get(messages)
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
            deadline.record_activity()
        if event_type == "item.completed" and item_type == "agent_message":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                last_message = text.strip()
        if event_type == "turn.completed":
            return last_message or None
        if event_type in ("turn.failed", "error"):
            if reasons is not None:
                reasons.append("codex: {}".format(event_type))
            return None


def codex_summary(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
    reasons: Optional[List[str]] = None,
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
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        prompt,
    ]
    process = None
    stdout_messages = queue.Queue()
    stderr_lines: List[str] = []
    output = None
    cause = ""
    try:
        first_activity_timeout = _positive_finite_timeout(
            config["summary_first_activity_timeout_seconds"]
        )
        completion_timeout = _positive_finite_timeout(
            config["codex_timeout_seconds"]
        )
        first_activity_deadline = time.monotonic() + first_activity_timeout
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
            cause = "missing-stream"
        else:
            threading.Thread(
                target=read_lines,
                args=(process.stdout, stdout_messages),
                daemon=True,
            ).start()
            threading.Thread(
                target=drain_lines,
                args=(process.stderr, stderr_lines),
                daemon=True,
            ).start()
            before = len(reasons) if reasons is not None else 0
            output = _collect_codex_summary(
                stdout_messages,
                first_activity_deadline=first_activity_deadline,
                completion_timeout=completion_timeout,
                reasons=reasons,
            )
            if not output and (reasons is None or len(reasons) == before):
                cause = "no-summary"
    except TimeoutError as error:
        cause = (
            "timeout-first-activity"
            if "no model activity" in str(error)
            else "timeout-completion"
        )
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        detail = str(error).strip()
        cause = detail[:120] if detail else error.__class__.__name__
    finally:
        if process is not None:
            stop_subprocess(process)
    if cause:
        _append_codex_reason(reasons, cause, stderr_lines)
    elif not output and reasons is not None and reasons:
        detail = _last_stderr(stderr_lines)
        if detail and reasons[-1].startswith("codex:"):
            reasons[-1] = "{} {}".format(reasons[-1], detail)
    if not output:
        return None
    sanitized = _sanitize_summary(output)
    if not sanitized:
        _append_codex_reason(reasons, "empty-after-sanitize", stderr_lines)
    return sanitized or None


def command_summary(
    config: Dict[str, Any],
    name: str,
    workspace: str,
    status: str,
    transcript: str,
    reasons: Optional[List[str]] = None,
) -> Optional[str]:
    command_value = config.get("summary_command")
    if not isinstance(command_value, list) or not all(
        isinstance(argument, str) for argument in command_value
    ) or not command_value:
        if reasons is not None:
            reasons.append("command: invalid-command")
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
        first_activity_timeout = _positive_finite_timeout(
            config["summary_first_activity_timeout_seconds"]
        )
        completion_timeout = _positive_finite_timeout(
            config["summary_command_timeout_seconds"]
        )
        command_timeout = first_activity_timeout + completion_timeout + 2.0
        completed = subprocess.run(
            command,
            check=True,
            input=transcript,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            timeout=command_timeout,
        )
    except subprocess.TimeoutExpired:
        if reasons is not None:
            reasons.append("command: timeout")
        return None
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        if reasons is not None:
            detail = redact_command_text(str(error), command).strip()
            reasons.append(
                "command: {}".format(
                    detail[:120] if detail else error.__class__.__name__
                )
            )
        return None
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        if reasons is not None:
            detail = completed.stderr.strip().splitlines()
            safe_detail = redact_command_text(detail[-1], command) if detail else ""
            suffix = " " + safe_detail[:120] if safe_detail else ""
            reasons.append("command: no-output{}".format(suffix))
        return None
    sanitized = _sanitize_summary(lines[-1])
    if not sanitized and reasons is not None:
        reasons.append("command: empty-after-sanitize")
    return sanitized or None


__all__ = [
    "ANNOUNCEMENT_PROMPT",
    "CODEX_MODEL_ACTIVITY_ITEMS",
    "CODEX_PROGRESS_TIMER",
    "CODEX_STATUS_LINE",
    "SUMMARY_PROMPT",
    "_collect_codex_summary",
    "_drain_stream",
    "_read_stream_lines",
    "_sanitize_summary",
    "_stop_subprocess",
    "_positive_finite_timeout",
    "build_prompt",
    "codex_summary",
    "command_summary",
    "summary_transcript",
    "template_summary",
]
