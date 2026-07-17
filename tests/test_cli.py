"""Tests for the command-line interface."""

import pytest

from kindertales_scraper import __version__, cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI reports the package version."""
    with pytest.raises(SystemExit, match="0"):
        cli.main(("--version",))

    assert capsys.readouterr().out == f"{__version__}\n"


def test_empty_invocation() -> None:
    """The placeholder accepts an empty invocation."""
    assert cli.main(()) == 0
