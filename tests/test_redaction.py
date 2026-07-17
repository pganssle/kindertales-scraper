"""Tests for secret redaction."""

import pytest

from kindertales_scraper import redaction


@pytest.mark.parametrize(
    ("original", "expected"),
    [
        ("https://example.test/path", "https://example.test/path"),
        (
            "https://example.test/path?sig=secret&x=1",
            "https://example.test/path?sig=REDACTED&x=REDACTED",
        ),
    ],
)
def test_redact_url(original: str, expected: str) -> None:
    """Signed URL query values are removed."""
    assert redaction.url(original) == expected


def test_redact_headers() -> None:
    """Authentication headers are removed case-insensitively."""
    assert redaction.headers({"Authorization": "Bearer x", "Accept": "json"}) == {
        "Authorization": "REDACTED",
        "Accept": "json",
    }


def test_redact_text() -> None:
    """Bearer tokens embedded in diagnostics are removed."""
    assert redaction.text("failed with Bearer abc.def") == "failed with Bearer REDACTED"
