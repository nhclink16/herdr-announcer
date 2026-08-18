"""Redaction helpers for commands shown in diagnostics and status screens."""

import re
from typing import List, Sequence, Tuple


_SENSITIVE_NAMES = {
    "api_key",
    "authorization",
    "auth_token",
    "access_token",
    "bearer_token",
    "password",
    "passwd",
    "secret",
    "token",
}
_HEADER_SECRET_RE = re.compile(
    r"(?i)(\b(?:authorization|x-api-key|api-key)\s*:\s*(?:bearer\s+)?)([^\s,;]+)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|password)=)([^&\s]+)"
)
_ASSIGNMENT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def mask_secret(value: str) -> str:
    """Keep a secret's tail recognizable without exposing the value."""
    if not value:
        return ""
    return "****{}".format(value[-4:]) if len(value) > 4 else "****"


def _is_sensitive_name(value: str) -> bool:
    normalized = value.lstrip("-").lower().replace("-", "_")
    return normalized in _SENSITIVE_NAMES or any(
        normalized.endswith(suffix)
        for suffix in ("_api_key", "_token", "_secret", "_password", "_passwd")
    )


def _redact_embedded(value: str) -> Tuple[str, List[str]]:
    secrets: List[str] = []

    def replace(match: "re.Match[str]") -> str:
        secret = match.group(2)
        secrets.append(secret)
        return match.group(1) + mask_secret(secret)

    redacted = _HEADER_SECRET_RE.sub(replace, value)
    redacted = _QUERY_SECRET_RE.sub(replace, redacted)
    return redacted, secrets


def _redact_command(command: Sequence[str]) -> Tuple[List[str], List[str]]:
    if not command:
        return [], []
    redacted: List[str] = []
    secrets: List[str] = []
    mask_next = False
    for index, argument in enumerate(command):
        if index == 0:
            redacted.append(argument)
            continue
        if mask_next:
            redacted.append(mask_secret(argument))
            secrets.append(argument)
            mask_next = False
            continue

        if "=" in argument:
            name, value = argument.split("=", 1)
            if name.startswith("-") or _ASSIGNMENT_NAME_RE.match(name):
                redacted.append("{}={}".format(name, mask_secret(value)))
                secrets.append(value)
                continue

        if argument.startswith("-"):
            redacted.append(argument)
            if _is_sensitive_name(argument):
                mask_next = True
            continue

        redacted.append(mask_secret(argument))
        secrets.append(argument)
    return redacted, secrets


def redact_command(command: Sequence[str]) -> List[str]:
    """Return command argv with credential-shaped values masked."""
    return _redact_command(command)[0]


def redact_command_text(text: str, command: Sequence[str]) -> str:
    """Remove secrets recognized in *command* from related diagnostic text."""
    redacted = text
    _, secrets = _redact_command(command)
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, mask_secret(secret))
    redacted, _ = _redact_embedded(redacted)
    return redacted


__all__ = ["mask_secret", "redact_command", "redact_command_text"]
