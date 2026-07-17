"""Removal of authentication material from diagnostics and archives."""

import re
import urllib.parse
from collections.abc import Mapping

_SECRET_KEYS = frozenset(
    {
        "authorization",
        "cookie",
        "password",
        "signature",
        "sig",
        "token",
        "x-amz-credential",
        "x-amz-signature",
    }
)
_TOKEN_PATTERN = re.compile(r"(?i)(bearer\s+)[^\s,;]+")


def url(value: str) -> str:
    """Redact every query parameter from a potentially signed URL."""
    parsed = urllib.parse.urlsplit(value)
    if not parsed.query:
        return value
    names = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = urllib.parse.urlencode(tuple((name, "REDACTED") for name, _ in names))
    return urllib.parse.urlunsplit(parsed._replace(query=redacted))


def headers(values: Mapping[str, str]) -> dict[str, str]:
    """Return headers with authentication values removed."""
    return {
        name: "REDACTED" if name.casefold() in _SECRET_KEYS else value
        for name, value in values.items()
    }


def text(value: str) -> str:
    """Redact recognizable bearer tokens from diagnostic text."""
    return _TOKEN_PATTERN.sub(r"\1REDACTED", value)
