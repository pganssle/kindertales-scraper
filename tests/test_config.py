"""Tests for application configuration."""

from pathlib import Path

import pytest

from kindertales_scraper import config


def test_load_complete_configuration(tmp_path: Path) -> None:
    """All supported sections are represented in the typed model."""
    path = tmp_path / "config.toml"
    path.write_text(
        """
[account]
email = "parent@example.com"
[authentication]
allow_plaintext_session_cache = true
cache_directory = "private-cache"
[archive]
directory = "export"
folder_format = "{child_name}"
folder_frequency = "monthly"
filename_format = "{child_name}_{timestamp:%Y%m%d}_{sequence:03d}{extension}"
sidecar_layout = "parallel"
[exports]
child_records = false
messages = true
billing = true
[children]
use_kindertales_name = false
[children.names]
"Child-2" = "Mark"
[synchronization]
overlap_days = 4
[request_policy]
max_in_flight = 4
max_media_downloads = 1
jitter_fraction = 0.25
max_retries = 0
stop_after_forbidden = 2
quotas = [{count = 5, window_seconds = 2.0}]
[metadata]
fallback_latitude = 1.5
fallback_longitude = -2.5
[metadata.centers."123"]
latitude = 40.0
longitude = -73.0
timezone = "America/New_York"
""",
        encoding="utf-8",
    )

    loaded = config.load(path)

    assert loaded.email == "parent@example.com"
    assert loaded.cache_directory == Path("private-cache")
    assert loaded.allow_plaintext_session_cache
    assert loaded.archive_directory == Path("export")
    assert loaded.archive_layout == config.ArchiveLayout(
        folder_frequency=config.FolderFrequency.MONTHLY,
        folder_format="{child_name}",
        filename_format=(
            "{child_name}_{timestamp:%Y%m%d}_{sequence:03d}{extension}"
        ),
        sidecar_layout=config.SidecarLayout.PARALLEL,
    )
    assert loaded.overlap_days == 4
    assert loaded.exports == config.Exports(
        child_records=False,
        messages=True,
        billing=True,
    )
    assert loaded.child_names == {"Child-2": "Mark"}
    assert not loaded.use_kindertales_name
    assert loaded.source_path == path
    assert loaded.request_policy == config.RequestPolicy(
        quotas=(config.Quota(5, 2.0),),
        max_in_flight=4,
        max_media_downloads=1,
        jitter_fraction=0.25,
        max_retries=0,
        stop_after_forbidden=2,
    )
    assert loaded.fallback_coordinates == config.Coordinates(1.5, -2.5)
    assert loaded.centers["123"] == config.Center(
        coordinates=config.Coordinates(40.0, -73.0),
        timezone="America/New_York",
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("account = 'wrong'", "account must be a TOML table"),
        ("[account]\nemail = 3", "account.email is required"),
        ("[account]\nemail = 'invalid'", "must be an email"),
        (
            "[account]\nemail = 'a@b'\n[synchronization]\noverlap_days=-1",
            "cannot be negative",
        ),
        ("[account]\nemail='a@b'\n[request_policy]\nquotas='x'", "must be an array"),
        ("[account]\nemail='a@b'\n[request_policy]\nquotas=[1]", "each quota"),
        ("[account]\nemail='a@b'\n[request_policy]\nquotas=[]", "at least one"),
        (
            "[account]\nemail='a@b'\n[request_policy]\njitter_fraction=1.0",
            "jitter_fraction",
        ),
        ("[account]\nemail='a@b'\n[request_policy]\nmax_in_flight=0", "request limits"),
        (
            "[account]\nemail='a@b'\n[metadata]\nfallback_latitude=1",
            "must both be numbers",
        ),
        (
            "[account]\nemail='a@b'\n[metadata]\nfallback_latitude=91\nfallback_longitude=2",
            "outside",
        ),
        ("[account]\nemail='a@b'\n[metadata]\ncenters=[]", "centers must be a table"),
        ("[account]\nemail='a@b'\n[metadata.centers]\nx=2", "each center"),
        (
            "[account]\nemail='a@b'\n[archive]\nfolder_frequency='weekly'",
            "folder_frequency",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nsidecar_layout='elsewhere'",
            "sidecar_layout",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nfilename_format='{unknown}_{sequence}{extension}'",
            "unknown fields",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nfilename_format='{timestamp}{extension}'",
            "missing fields",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nfilename_format='{timestamp}_{sequence:nope}{extension}'",
            "format specification",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nfilename_format='{timestamp'",
            "valid format string",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nfolder_format='{unknown}'",
            "folder_format has unknown fields",
        ),
        (
            "[account]\nemail='a@b'\n[archive]\nfolder_format='{sequence:nope}'",
            "folder_format has an invalid format specification",
        ),
        (
            "[account]\nemail='a@b'\n[children]\nnames=[]",
            "children.names",
        ),
        (
            "[account]\nemail='a@b'\n[children.names]\nChild=''",
            "children.names",
        ),
    ],
)
def test_invalid_configuration(tmp_path: Path, content: str, message: str) -> None:
    """Invalid configuration is rejected with an actionable message."""
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(config.ConfigError, match=message):
        config.load(path)


@pytest.mark.parametrize(
    ("count", "window"),
    [(0, 1.0), (1, 0.0), (1, float("inf"))],
)
def test_invalid_quota(count: int, window: float) -> None:
    """Quotas require finite, positive values."""
    with pytest.raises(config.ConfigError, match="positive"):
        config.Quota(count, window)


def test_write_initial_configuration(tmp_path: Path) -> None:
    """Initial configuration is private and safely quotes the email."""
    path = tmp_path / "nested" / "config.toml"
    config.write_initial(path, 'a"b@example.com')
    assert path.stat().st_mode & 0o777 == 0o600
    assert config.load(path).email == 'a"b@example.com'
