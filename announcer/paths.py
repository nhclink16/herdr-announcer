"""Plugin config/state directory resolution."""

import os
import subprocess
from pathlib import Path
from typing import Tuple


PLUGIN_ID = "nhclink16.announcer"


def local_plugin_dirs() -> Tuple[Path, Path]:
    config_dir = (
        Path.home() / ".config" / "herdr" / "plugins" / "config" / PLUGIN_ID
    )
    state_dir = Path.home() / ".local" / "state" / "herdr" / "plugins" / PLUGIN_ID
    return config_dir, state_dir


def _resolve_dirs_without_env() -> Tuple[Path, Path]:
    """Locate plugin dirs for commands launched outside Herdr."""
    fallback_config, state_dir = local_plugin_dirs()
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
    return Path(config_dir) if config_dir else fallback_config, state_dir


def resolve_dirs() -> Tuple[Path, Path]:
    """Resolve each plugin directory independently, never to cwd."""
    config_value = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or ""
    state_value = os.environ.get("HERDR_PLUGIN_STATE_DIR") or ""
    if config_value and state_value:
        return Path(config_value), Path(state_value)
    fallback_config, fallback_state = _resolve_dirs_without_env()
    return (
        Path(config_value) if config_value else fallback_config,
        Path(state_value) if state_value else fallback_state,
    )


__all__ = [
    "PLUGIN_ID",
    "_resolve_dirs_without_env",
    "local_plugin_dirs",
    "resolve_dirs",
]
