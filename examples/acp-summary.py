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


COMMAND = ["npx", "-y", "@agentclientprotocol/claude-agent-acp"]
TIMEOUT_SECONDS = 90.0


def send_message(process, message):
    if process.stdin is None:
        raise RuntimeError("ACP adapter stdin is unavailable")
    process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    process.stdin.flush()


def read_stdout(stream, messages):
    try:
        for line in iter(stream.readline, ""):
            messages.put(line)
    finally:
        messages.put(None)


def request_error(message):
    error = message.get("error")
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"]
    return "ACP request failed"


def wait_for_response(process, messages, response_id, deadline, chunks=None):
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("ACP request timed out")
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty:
            raise TimeoutError("ACP request timed out")

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
            if (
                isinstance(update, dict)
                and update.get("sessionUpdate") == "agent_message_chunk"
            ):
                content = update.get("content")
                text = content.get("text") if isinstance(content, dict) else None
                if isinstance(text, str):
                    chunks.append(text)


def stop_process(process, clean_exit, deadline):
    if clean_exit and process.stdin is not None:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    if process.poll() is not None:
        return

    if clean_exit:
        grace = min(1.0, max(0.0, deadline - time.monotonic()))
        if grace:
            try:
                process.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                pass

    if time.monotonic() >= deadline:
        process.kill()
        return
    process.terminate()
    termination_grace = max(0.0, min(0.5, deadline - time.monotonic()))
    if not termination_grace:
        process.kill()
        return
    try:
        process.wait(timeout=termination_grace)
        return
    except subprocess.TimeoutExpired:
        process.kill()


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
            "params": {"cwd": cwd, "mcpServers": []},
        },
    )
    result = wait_for_response(process, messages, 1, deadline)
    if not isinstance(result, dict):
        raise RuntimeError("session/new returned an invalid result")
    session_id = result.get("sessionId")
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("session/new returned no sessionId")
    return session_id


def prompt_session(process, messages, deadline, session_id, text):
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
    wait_for_response(process, messages, 2, deadline, chunks)
    return " ".join("".join(chunks).split())


def start_adapter():
    environment = os.environ.copy()
    environment.setdefault("ANTHROPIC_MODEL", "claude-sonnet-5")
    return subprocess.Popen(
        COMMAND,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=environment,
    )


def generate(instruction, stdin_text):
    deadline = time.monotonic() + TIMEOUT_SECONDS
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

        initialize(process, messages, deadline)
        session_id = new_session(process, messages, deadline, temp_dir)
        prompt_text = instruction + "\n\n--- input ---\n" + stdin_text
        reply = prompt_session(
            process, messages, deadline, session_id, prompt_text
        )
        prompt_finished = True
        return reply
    finally:
        if process is not None:
            stop_process(process, prompt_finished, deadline)
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
