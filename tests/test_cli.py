"""Tests for the command-line interface."""

from pathlib import Path

import pytest

from kindertales_scraper import __version__, cli, credentials


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI reports the package version."""
    with pytest.raises(SystemExit, match="0"):
        cli.main(("--version",))

    assert capsys.readouterr().out == f"{__version__}\n"


def test_empty_invocation() -> None:
    """The placeholder accepts an empty invocation."""
    assert cli.main(()) == 0


@pytest.mark.parametrize("email_arguments", [("--email", "a@example.com"), ()])
def test_configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    email_arguments: tuple[str, ...],
) -> None:
    """Configure writes the file and obtains the account password."""
    path = tmp_path / "config.toml"
    prompted: list[str] = []
    monkeypatch.setattr("builtins.input", lambda _: "a@example.com")
    monkeypatch.setattr(credentials, "password", lambda email: prompted.append(email))
    assert cli.main(("configure", "--config", str(path), *email_arguments)) == 0
    assert 'email = "a@example.com"' in path.read_text(encoding="utf-8")
    assert prompted == ["a@example.com"]


def test_delete_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Credential deletion also removes cached browser state."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[account]\nemail="a@example.com"', encoding="utf-8")
    cache = tmp_path / ".cache" / "kindertales-scraper"
    cache.mkdir(parents=True)
    (cache / "session.json").write_text("secret", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    deleted: list[str] = []
    monkeypatch.setattr(credentials, "delete", deleted.append)
    assert cli.main(("credentials", "delete", "--config", str(config_path))) == 0
    assert deleted == ["a@example.com"]
    assert not (cache / "session.json").exists()
