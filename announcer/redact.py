"""Redaction helpers for commands shown in diagnostics and status screens."""

import re
from pathlib import Path
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
_PREFIXED_CREDENTIAL_RE = re.compile(
    r"^(?:sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_-]{8,}|"
    r"xoxb-[A-Za-z0-9_-]{8,}|AKIA[A-Z0-9]{16})$"
)
_JWT_RE = re.compile(
    r"^[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}$"
)
_BEARER_RE = re.compile(r"(?i)^(bearer\s+)(\S+)$")
_HIGH_ENTROPY_MIN_LENGTH = 20
_SENSITIVE_SHORT_OPTIONS = {"-p"}
_SENSITIVE_OPTION_PREFIXES = tuple(
    sorted(
        {
            "--" + name.replace("_", separator)
            for name in _SENSITIVE_NAMES
            for separator in ("-", "_")
        },
        key=len,
        reverse=True,
    )
)


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


def _existing_path(value: str) -> bool:
    try:
        return Path(value).expanduser().exists()
    except (OSError, ValueError):
        return False


def _looks_high_entropy(value: str) -> bool:
    return (
        len(value) >= _HIGH_ENTROPY_MIN_LENGTH
        and not any(char.isspace() for char in value)
        and any(char.islower() for char in value)
        and any(char.isupper() for char in value)
        and any(char.isdigit() for char in value)
    )


def _redact_value(value: str) -> Tuple[str, List[str]]:
    if _existing_path(value):
        return value, []
    embedded, secrets = _redact_embedded(value)
    if secrets:
        return embedded, secrets
    bearer = _BEARER_RE.match(value)
    if bearer is not None:
        secret = bearer.group(2)
        return bearer.group(1) + mask_secret(secret), [secret]
    if any(char.isspace() for char in value):
        return value, []
    if (
        _PREFIXED_CREDENTIAL_RE.match(value)
        or _JWT_RE.match(value)
        or _looks_high_entropy(value)
    ):
        return mask_secret(value), [value]
    return value, []


def _attached_option_value(argument: str) -> Tuple[str, str]:
    """Split values joined to short or known-sensitive long options."""
    if (
        argument.startswith("-")
        and not argument.startswith("--")
        and len(argument) > 2
    ):
        return argument[:2], argument[2:]
    for prefix in _SENSITIVE_OPTION_PREFIXES:
        if argument.startswith(prefix) and len(argument) > len(prefix):
            return prefix, argument[len(prefix):]
    return "", ""


def _redact_command(command: Sequence[str]) -> Tuple[List[str], List[str]]:
    if not command:
        return [], []
    redacted: List[str] = []
    secrets: List[str] = []
    mask_next = False
    for argument in command:
        if mask_next:
            redacted.append(mask_secret(argument))
            secrets.append(argument)
            mask_next = False
            continue

        if "=" in argument:
            name, value = argument.split("=", 1)
            if name.startswith("-") or _ASSIGNMENT_NAME_RE.match(name):
                if _is_sensitive_name(name):
                    rendered, found = mask_secret(value), [value]
                else:
                    rendered, found = _redact_value(value)
                redacted.append("{}={}".format(name, rendered))
                secrets.extend(found)
                continue

        if argument.startswith("-"):
            prefix, value = _attached_option_value(argument)
            if value:
                if (
                    prefix in _SENSITIVE_SHORT_OPTIONS
                    or _is_sensitive_name(prefix)
                ):
                    rendered, found = mask_secret(value), [value]
                else:
                    rendered, found = _redact_value(value)
                redacted.append(prefix + rendered)
                secrets.extend(found)
                continue
            redacted.append(argument)
            if _is_sensitive_name(argument):
                mask_next = True
            continue

        rendered, found = _redact_value(argument)
        redacted.append(rendered)
        secrets.extend(found)
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
