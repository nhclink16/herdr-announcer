"""Config-file editing helpers shared by non-interactive frontends."""

import io
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import load_config
from .wizard import write_config


STATE_ORDER = ("done", "blocked", "idle", "working", "unknown")


def config_path(config_dir: Path) -> Path:
    return Path(config_dir) / "config.toml"


def write_config_keys(
    config_dir: Path, updates: Dict[str, Any]
) -> Dict[str, Any]:
    """Merge updates into the on-disk config without dropping unknown keys."""
    path = config_path(config_dir)
    config = load_config(config_dir)
    config.update(updates)
    with redirect_stdout(io.StringIO()):
        write_config(path, config, sorted(updates))
    return load_config(config_dir)


def load_config_safely(config_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        return load_config(config_dir)
    except Exception:
        return None


def announce_states(config: Dict[str, Any]) -> List[str]:
    value = config.get("announce")
    if not isinstance(value, list):
        return []
    picked = {
        item.lower()
        for item in value
        if isinstance(item, str) and item.lower() in STATE_ORDER
    }
    return [state for state in STATE_ORDER if state in picked]


__all__ = [
    "STATE_ORDER",
    "announce_states",
    "config_path",
    "load_config_safely",
    "write_config_keys",
]
