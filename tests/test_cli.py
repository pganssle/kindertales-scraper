"""Tests for the command-line interface."""

import datetime as dt
from pathlib import Path

import pytest

from kindertales_scraper import (
    __version__,
    center_setup,
    cli,
    config,
    credentials,
    names,
    sync,
    verify,
)


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


def test_sync_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The sync CLI passes bounds and modes to the asynchronous runner."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[account]\nemail="a@example.com"', encoding="utf-8")
    observed: list[tuple[sync.Bounds, bool, bool, bool]] = []

    async def run(
        _settings: object,
        bounds: sync.Bounds,
        *,
        dry_run: bool,
        headed: bool,
        refresh_existing: bool,
    ) -> sync.SyncSummary:
        observed.append((bounds, dry_run, headed, refresh_existing))
        return sync.SyncSummary(2, 3, 4, dry_run)

    monkeypatch.setattr(sync, "run_configured", run)
    assert (
        cli.main(
            (
                "sync",
                "--config",
                str(config_path),
                "--from",
                "2026-07-01",
                "--through",
                "2026-07-02",
                "--dry-run",
                "--headed",
                "--refresh",
            )
        )
        == 0
    )
    assert observed == [
        (sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 2)), True, True, True)
    ]
    assert capsys.readouterr().out == (
        "2 children, 3 activities, 4 media, 0 records (dry run)\n"
    )


def test_sync_exits_after_printing_manual_name_configuration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Manual preferred-name configuration exits without a traceback."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[account]\nemail="a@example.com"', encoding="utf-8")

    async def run(*_args: object, **_kwargs: object) -> sync.SyncSummary:
        raise names.NameConfigurationRequiredError

    monkeypatch.setattr(sync, "run_configured", run)
    assert cli.main(("sync", "--config", str(config_path))) == 2


def test_configure_centers_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The center setup command passes configuration and browser mode."""
    path = tmp_path / "config.toml"
    path.write_text('[account]\nemail="a@example.com"', encoding="utf-8")
    calls: list[tuple[Path | None, bool]] = []

    async def run(settings: object, *, headed: bool) -> None:
        assert isinstance(settings, config.Config)
        calls.append((settings.source_path, headed))

    monkeypatch.setattr(center_setup, "run_configured", run)
    assert cli.main(("configure-centers", "--config", str(path), "--headed")) == 0
    assert calls == [(path, True)]


def test_configure_centers_honors_manual_name_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Center discovery exits cleanly when names will be edited manually."""
    path = tmp_path / "config.toml"
    path.write_text('[account]\nemail="a@example.com"', encoding="utf-8")

    async def run(*_args: object, **_kwargs: object) -> None:
        raise names.NameConfigurationRequiredError

    monkeypatch.setattr(center_setup, "run_configured", run)
    assert cli.main(("configure-centers", "--config", str(path))) == 2


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (
            verify.VerificationReport(2, (), 3),
            (0, "verified 2 media files and 3 records and 0 documents\n"),
        ),
        (
            verify.VerificationReport(
                1,
                (
                    verify.VerificationIssue("media-1", "hash mismatch"),
                    verify.VerificationIssue(None, "database mismatch"),
                ),
            ),
            (1, "media-1: hash mismatch\ndatabase mismatch\n"),
        ),
    ],
)
def test_verify_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    report: verify.VerificationReport,
    expected: tuple[int, str],
) -> None:
    """The verify CLI reports success and individual integrity failures."""
    config_path = tmp_path / "config.toml"
    config_path.write_text('[account]\nemail="a@example.com"', encoding="utf-8")
    monkeypatch.setattr(verify.ArchiveVerifier, "run", lambda _self: report)
    expected_code, expected_output = expected
    assert cli.main(("verify", "--config", str(config_path))) == expected_code
    assert capsys.readouterr().out == expected_output
