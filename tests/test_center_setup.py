"""Tests for interactive center metadata configuration."""

import io
from pathlib import Path

import pytest

from kindertales_scraper import center_setup, config, discovery


def test_interactive_setup_lists_centers_and_preserves_config(tmp_path: Path) -> None:
    """Center discovery writes validated defaults without reformatting other tables."""
    path = tmp_path / "config.toml"
    path.write_text(
        '# keep me\n[account]\nemail = "a@example.com" # account\n',
        encoding="utf-8",
    )
    answers = iter(
        (
            "",
            "",
            "America/New_York",
            "250",
            "40",
            "",
            "40",
            "-73",
            "",
            "50",
        )
    )
    output = io.StringIO()
    center_setup.InteractiveSetup(
        config.load(path),
        input_fn=lambda _prompt: next(answers),
        output=output,
    ).configure((discovery.Child("child", "Mark", "center-1"),))
    loaded = config.load(path)
    assert loaded.default_center == config.Center(
        timezone="America/New_York",
        gps_uncertainty_meters=250,
    )
    assert loaded.centers["center-1"] == config.Center(
        config.Coordinates(40, -73),
        gps_uncertainty_meters=50,
    )
    assert loaded.center("center-1").timezone == "America/New_York"
    assert "# keep me" in path.read_text(encoding="utf-8")
    assert "Center center-1: Mark" in output.getvalue()
    assert "must be specified together" in output.getvalue()


def test_update_config_reuses_tables_and_omits_absent_values(tmp_path: Path) -> None:
    """An existing table remains intact when optional defaults are absent."""
    path = tmp_path / "config.toml"
    path.write_text(
        '[account]\nemail = "a@example.com"\n[metadata]\ncustom = "kept"\n',
        encoding="utf-8",
    )
    center_setup.update_config(path, config.Center(), {"center": config.Center()})
    document = path.read_text(encoding="utf-8")
    assert 'custom = "kept"' in document
    assert "latitude" not in document
    assert config.load(path).centers["center"] == config.Center()


@pytest.mark.parametrize(
    ("settings", "children", "message"),
    [
        (
            config.Config("a@example.com", source_path=Path("config.toml")),
            (discovery.Child("child", "Mark"),),
            "did not expose",
        ),
        (
            config.Config("a@example.com"),
            (discovery.Child("child", "Mark", "center"),),
            "loaded from a file",
        ),
    ],
)
def test_setup_requires_linked_centers_and_source_file(
    settings: config.Config,
    children: tuple[discovery.Child, ...],
    message: str,
) -> None:
    """Interactive setup needs both stable center IDs and a writable TOML source."""
    with pytest.raises(center_setup.CenterSetupError, match=message):
        center_setup.InteractiveSetup(settings).configure(children)
