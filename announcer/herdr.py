"""Herdr CLI and event helpers."""

import json
import subprocess
from typing import Any, Dict, List, Optional, Sequence, Tuple


VALID_STATUSES = {"idle", "working", "blocked", "done", "unknown"}


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
    pane_id = None
    status_value = None
    # Herdr 0.7.0 emits pane.agent_status_changed with these top-level fields.
    if isinstance(payload, dict):
        top_pane = payload.get("pane_id")
        top_status = payload.get("agent_status")
        if isinstance(top_pane, str):
            pane_id = top_pane
        if isinstance(top_status, str):
            status_value = top_status
    if pane_id is None:
        pane_id = _find_string_for_key(payload, "pane_id")
    if status_value is None:
        status_value = _find_string_for_key(payload, "agent_status")
    if not pane_id:
        raise ValueError("event payload has no string pane_id")
    if not status_value:
        raise ValueError("event payload has no string agent_status")
    status = status_value.lower()
    if status not in VALID_STATUSES:
        raise ValueError("event payload has invalid agent_status")
    return pane_id, status


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


def _short_error(error: BaseException) -> str:
    detail = str(error).strip()
    return detail[:120] if detail else error.__class__.__name__


def get_context(
    herdr_bin: str, pane_id: str, reasons: Optional[List[str]] = None
) -> Tuple[str, str]:
    try:
        agents_payload = json.loads(_run_text([herdr_bin, "agent", "list"]))
        agents = _records_from_result(agents_payload, "agents")
        record = next(
            (item for item in agents if item.get("pane_id") == pane_id), None
        )
        if record is None:
            if reasons is not None:
                reasons.append("herdr: pane-not-found")
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
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        if reasons is not None:
            reasons.append("herdr-agent: {}".format(_short_error(error)))
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
            if reasons is not None:
                reasons.append("herdr: workspace-not-found")
            return name, ""
        for key in ("label", "title", "name"):
            value = workspace.get(key)
            if isinstance(value, str) and value:
                return name, value
    except (OSError, ValueError, TypeError, subprocess.SubprocessError) as error:
        if reasons is not None:
            reasons.append("herdr-workspace: {}".format(_short_error(error)))
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


def get_transcript(
    herdr_bin: str, pane_id: str, reasons: Optional[List[str]] = None
) -> str:
    arguments = [pane_id, "--source", "recent-unwrapped", "--lines", "100"]
    try:
        output = _run_text([herdr_bin, "agent", "read"] + arguments)
    except (OSError, subprocess.SubprocessError):
        try:
            output = _run_text([herdr_bin, "pane", "read"] + arguments)
        except (OSError, subprocess.SubprocessError) as error:
            if reasons is not None:
                reasons.append("herdr-read: {}".format(_short_error(error)))
            return ""
    return _extract_read_text(output)[-4000:]


__all__ = [
    "VALID_STATUSES",
    "_extract_read_text",
    "_find_string_for_key",
    "_records_from_result",
    "_run_text",
    "get_context",
    "get_transcript",
    "parse_event",
]
