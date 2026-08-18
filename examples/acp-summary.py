#!/usr/bin/env python3
"""One-shot ACP summarizer.

Example:
    echo "transcript" | ./acp-summary.py "Summarize in one sentence"
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
try:
    from announcer.deadline import TwoPhaseDeadline, read_lines, stop_subprocess
except ImportError as error:
    print(
        "acp-summary: expected announcer/ beside examples/ ({})".format(error),
        file=sys.stderr,
    )
    raise SystemExit(1)


COMMAND = ["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.70.0"]
MODEL_ACTIVITY_UPDATES = {
    "agent_message_chunk",
    "agent_thought_chunk",
    "plan",
    "tool_call",
}


def timeout_from_environment(name, default):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def send_message(process, message):
    if process.stdin is None:
        raise RuntimeError("ACP adapter stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_stdout(stream, messages):
    read_lines(stream, messages)


def request_error(message):
    error = message.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return "ACP request failed"


def wait_for_response(
    process,
    messages,
    response_id,
    deadline=None,
    chunks=None,
    first_activity_deadline=None,
    completion_timeout=None,
):
    timer = TwoPhaseDeadline(
        first_activity_deadline=first_activity_deadline,
        completion_timeout=completion_timeout,
        deadline=deadline,
        first_timeout_message="ACP model produced no activity",
        completion_timeout_message="ACP request timed out",
    )
    while True:
        line = timer.get(messages)

        if line is None:
            raise RuntimeError("ACP adapter closed stdout")
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(message, dict):
            continue

        if "method" in message and "id" in message:
            send_message(
                process,
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "not supported"},
                },
            )
            continue

        if message.get("id") == response_id:
            if "error" in message:
                raise RuntimeError(request_error(message))
            if "result" not in message:
                raise RuntimeError("malformed ACP response")
            return message["result"]

        if chunks is not None and message.get("method") == "session/update":
            params = message.get("params")
            update = params.get("update") if isinstance(params, dict) else None
            update_type = (
                update.get("sessionUpdate") if isinstance(update, dict) else None
            )
            if update_type in MODEL_ACTIVITY_UPDATES:
                timer.record_activity()
            if isinstance(update, dict) and update_type == "agent_message_chunk":
                content = update.get("content")
                text = content.get("text") if isinstance(content, dict) else None
                if isinstance(text, str):
                    chunks.append(text)


def stop_process(process, clean_exit, deadline):
    stop_subprocess(process, clean_exit=clean_exit, deadline=deadline)


def initialize(process, messages, deadline):
    send_message(
        process,
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": 1,
                "capabilities": {},
                "info": {
                    "name": "acp-summary",
                    "title": "ACP Summary",
                    "version": "1.0.0",
                },
            },
        },
    )
    wait_for_response(process, messages, 0, deadline)


def new_session(process, messages, deadline, cwd):
    send_message(
        process,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/new",
            "params": {
                "cwd": cwd,
                "mcpServers": [],
                "_meta": {
                    "disableBuiltInTools": True,
                    "claudeCode": {
                        "options": {
                            "settingSources": [],
                            "mcpServers": {},
                        }
                    },
                },
            },
        },
    )
    result = wait_for_response(process, messages, 1, deadline)
    if not isinstance(result, dict):
        raise RuntimeError("session/new returned an invalid result")
    session_id = result.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("session/new returned no sessionId")
    return session_id


def prompt_session(
    process,
    messages,
    session_id,
    text,
    first_activity_deadline,
    completion_timeout,
):
    send_message(
        process,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {
                "sessionId": session_id,
                "prompt": [{"type": "text", "text": text}],
            },
        },
    )
    chunks = []
    wait_for_response(
        process,
        messages,
        2,
        None,
        chunks,
        first_activity_deadline=first_activity_deadline,
        completion_timeout=completion_timeout,
    )
    return " ".join("".join(chunks).split())


def start_adapter():
    environment = os.environ.copy()
    environment.setdefault("ANTHROPIC_MODEL", "claude-sonnet-5")
    return subprocess.Popen(
        COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        # stdout is the ACP transport. Keep adapter diagnostics on the
        # wrapper's separate stderr stream so callers can report failures.
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )


def generate(instruction, stdin_text):
    overall_timeout = timeout_from_environment(
        "HERDR_SUMMARY_OVERALL_TIMEOUT_SECONDS", 90.0
    )
    first_activity_timeout = timeout_from_environment(
        "HERDR_SUMMARY_FIRST_ACTIVITY_TIMEOUT_SECONDS", 5.0
    )
    first_activity_deadline = time.monotonic() + first_activity_timeout
    temp_dir = tempfile.mkdtemp()
    process = None
    prompt_finished = False
    try:
        process = start_adapter()
        if process.stdout is None:
            raise RuntimeError("ACP adapter stdout is unavailable")
        messages = queue.Queue()
        threading.Thread(
            target=read_stdout, args=(process.stdout, messages), daemon=True
        ).start()

        initialize(process, messages, first_activity_deadline)
        session_id = new_session(
            process, messages, first_activity_deadline, temp_dir
        )
        prompt_text = instruction + "\n\n--- input ---\n" + stdin_text
        reply = prompt_session(
            process,
            messages,
            session_id,
            prompt_text,
            first_activity_deadline,
            overall_timeout,
        )
        prompt_finished = True
        return reply
    finally:
        if process is not None:
            stop_process(
                process,
                prompt_finished,
                time.monotonic() + 1.0,
            )
        shutil.rmtree(temp_dir)


def main():
    if len(sys.argv) != 2:
        print("usage: acp-summary.py INSTRUCTION", file=sys.stderr)
        return 1
    try:
        reply = generate(sys.argv[1], sys.stdin.read())
        print(reply)
        return 0
    except Exception as error:
        print("acp-summary: {}".format(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
