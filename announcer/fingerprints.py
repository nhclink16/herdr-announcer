"""Persist the last pane content that reached an announcement."""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional, Set, Tuple, TypeVar


FINGERPRINT_STATE_FILE = "announced-content.json"
DeliveryResult = TypeVar("DeliveryResult")


def content_fingerprint(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_content_fingerprints(path: Path) -> Dict[str, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        pane_id: fingerprint
        for pane_id, fingerprint in payload.items()
        if isinstance(pane_id, str) and isinstance(fingerprint, str)
    }


def _write_content_fingerprints(state_dir: Path, state: Dict[str, str]) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(state_dir),
            prefix="announced-content.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
        os.replace(temporary_name, str(state_dir / FINGERPRINT_STATE_FILE))
        temporary_name = ""
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def _active_content_fingerprints(
    state: Dict[str, str],
    pane_id: str,
    active_pane_ids: Optional[Set[str]],
) -> Dict[str, str]:
    if active_pane_ids is None:
        return state
    retained_pane_ids = set(active_pane_ids)
    retained_pane_ids.add(pane_id)
    return {
        active_pane_id: value
        for active_pane_id, value in state.items()
        if active_pane_id in retained_pane_ids
    }


def is_duplicate_content(
    state_dir: Path,
    pane_id: str,
    fingerprint: str,
    active_pane_ids: Optional[Set[str]] = None,
) -> bool:
    """Check committed content and prune panes absent from a live listing."""
    import fcntl

    with (state_dir / "announced-content.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = load_content_fingerprints(
                state_dir / FINGERPRINT_STATE_FILE
            )
            active_state = _active_content_fingerprints(
                state, pane_id, active_pane_ids
            )
            if active_state != state:
                state = active_state
                _write_content_fingerprints(state_dir, state)
            return state.get(pane_id) == fingerprint
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def deliver_content_once(
    state_dir: Path,
    pane_id: str,
    fingerprint: str,
    check_duplicate: bool,
    deliver: Callable[[], DeliveryResult],
    active_pane_ids: Optional[Set[str]] = None,
) -> Tuple[bool, Optional[DeliveryResult], Optional[OSError]]:
    """Serialize the final duplicate check, delivery, and committed record."""
    import fcntl

    with (state_dir / "announced-content.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = load_content_fingerprints(
                state_dir / FINGERPRINT_STATE_FILE
            )
            active_state = _active_content_fingerprints(
                state, pane_id, active_pane_ids
            )
            pruned = active_state != state
            state = active_state
            if check_duplicate and state.get(pane_id) == fingerprint:
                write_error = None
                if pruned:
                    try:
                        _write_content_fingerprints(state_dir, state)
                    except OSError as error:
                        write_error = error
                return False, None, write_error

            result = deliver()
            state[pane_id] = fingerprint
            try:
                _write_content_fingerprints(state_dir, state)
            except OSError as error:
                return True, result, error
            return True, result, None
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_content_fingerprint(
    state_dir: Path,
    pane_id: str,
    fingerprint: str,
) -> None:
    """Persist content only after its announcement was delivered."""
    import fcntl

    with (state_dir / "announced-content.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            state = load_content_fingerprints(
                state_dir / FINGERPRINT_STATE_FILE
            )
            state[pane_id] = fingerprint
            _write_content_fingerprints(state_dir, state)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


__all__ = [
    "FINGERPRINT_STATE_FILE",
    "content_fingerprint",
    "deliver_content_once",
    "is_duplicate_content",
    "load_content_fingerprints",
    "record_content_fingerprint",
]
