"""Tests for ExifTool metadata enrichment."""

import datetime as dt
import json
import subprocess
from pathlib import Path

import attrs
import pytest

from kindertales_scraper import config, discovery, metadata


@pytest.fixture
def context() -> tuple[discovery.Child, discovery.Activity]:
    """Return synthetic child and activity metadata context."""
    child = discovery.Child("child-1", "Alex", "center-1")
    activity = discovery.Activity(
        "activity-1",
        child.id,
        "Art",
        dt.datetime(2026, 7, 1, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4))),
        (discovery.MediaReference("media-1", "https://example.test/p.jpg"),),
        "A caption",
        "A teacher",
        "center-1",
    )
    return child, activity


def settings(*, center: bool = True, fallback: bool = True) -> config.Config:
    """Build metadata configuration with selectable coordinate sources."""
    centers = (
        {
            "center-1": config.Center(
                config.Coordinates(40.0, -73.0),
                "America/New_York",
                25.0,
            )
        }
        if center
        else {}
    )
    return config.Config(
        email="a@example.com",
        centers=centers,
        default_center=config.Center(
            config.Coordinates(1.0, 2.0) if fallback else None,
            gps_uncertainty_meters=100.0,
        ),
    )


def test_infers_missing_time_gps_caption_and_author(
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """Missing fields are populated with center coordinates and provenance."""
    child, activity = context
    fields, inferred_time, inferred_gps = metadata.fields_for(
        {}, child, activity, settings()
    )
    assert fields["EXIF:DateTimeOriginal"] == "2026:07:01 09:30:00"
    assert fields["EXIF:OffsetTimeOriginal"] == "-04:00"
    assert fields["EXIF:GPSLatitude"] == "40.0"
    assert fields["EXIF:GPSLatitudeRef"] == "N"
    assert fields["EXIF:GPSLongitude"] == "73.0"
    assert fields["EXIF:GPSLongitudeRef"] == "W"
    assert fields["EXIF:GPSHPositioningError"] == "25.0"
    assert fields["XMP-dc:Description"] == "A caption"
    assert fields["XMP-dc:Creator"] == "A teacher"
    assert fields["XMP-dc:Source"] == "Kindertales"
    provenance = json.loads(fields["XMP-photoshop:Instructions"])
    assert provenance["time_inferred"] is True
    assert provenance["gps_inferred"] is True
    assert provenance["gps_uncertainty_meters"] == 25.0
    assert inferred_time
    assert inferred_gps


def test_preserves_existing_metadata(
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """Authentic capture, GPS, caption, and creator fields are never overwritten."""
    child, activity = context
    original = {
        "[EXIF]DateTimeOriginal": "2020:01:01 01:02:03",
        "Composite:GPSPosition": "1 2",
        "IPTC:Caption-Abstract": "Existing",
        "XMP:Creator": "Existing creator",
    }
    fields, inferred_time, inferred_gps = metadata.fields_for(
        original, child, activity, settings()
    )
    assert "EXIF:DateTimeOriginal" not in fields
    assert "EXIF:GPSLatitude" not in fields
    assert "EXIF:GPSHPositioningError" not in fields
    assert "XMP-dc:Description" not in fields
    assert "XMP-dc:Creator" not in fields
    assert not inferred_time
    assert not inferred_gps


def test_preserves_existing_gps_uncertainty(
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """An embedded positioning error is not replaced when GPS is inferred."""
    child, activity = context
    fields, _, inferred_gps = metadata.fields_for(
        {"EXIF:GPSHPositioningError": 5}, child, activity, settings()
    )
    assert fields["EXIF:GPSLatitude"] == "40.0"
    assert "EXIF:GPSHPositioningError" not in fields
    assert inferred_gps


@pytest.mark.parametrize(
    ("original", "fields", "expected"),
    [
        (
            {
                "XMP-dc:Source": "Camera",
                "EXIF:Make": "Camera Co.",
                "File:Directory": "/private/tmp",
                "SourceFile": "/private/tmp/download",
            },
            {"XMP-dc:Source": "Kindertales"},
            {"XMP-dc:Source": "Camera", "EXIF:Make": "Camera Co."},
        ),
        (
            {"XMP-dc:Source": "Kindertales"},
            {"XMP-dc:Source": "Kindertales"},
            {},
        ),
        (
            {"[XMP-dc]Source": "Camera"},
            {"XMP-dc:Source": "Kindertales"},
            {"[XMP-dc]Source": "Camera"},
        ),
        ({"EXIF:Make": "Camera Co."}, {"XMP-dc:Source": "Kindertales"}, {}),
    ],
)
def test_sidecar_contains_original_only_for_overwritten_metadata(
    original: dict[str, object],
    fields: dict[str, str],
    expected: dict[str, object],
) -> None:
    """Sidecars are portable original-metadata backups, never manifests."""
    assert metadata.sidecar_metadata(original, fields) == expected


@pytest.mark.parametrize(
    ("center", "fallback", "expected"),
    [(False, True, "1.0"), (False, False, None)],
)
def test_gps_precedence(
    context: tuple[discovery.Child, discovery.Activity],
    center: bool,
    fallback: bool,
    expected: str | None,
) -> None:
    """Global coordinates follow center coordinates and may be absent."""
    child, activity = context
    fields, _, inferred = metadata.fields_for(
        {}, child, activity, settings(center=center, fallback=fallback)
    )
    assert fields.get("EXIF:GPSLatitude") == expected
    if expected is not None:
        assert fields["EXIF:GPSHPositioningError"] == "100.0"
    assert inferred is (expected is not None)


def test_optional_caption_and_author(
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """Absent scraped prose is not embedded as a stringified null."""
    child, activity = context
    activity = discovery.Activity(
        activity.id,
        activity.child_id,
        activity.kind,
        activity.occurred_at,
        activity.media,
        center_id=activity.center_id,
    )
    fields, _, _ = metadata.fields_for({}, child, activity, settings())
    assert "XMP-dc:Description" not in fields
    assert "XMP-dc:Creator" not in fields


def test_legacy_iptc_text_has_a_representable_readback(
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """XMP preserves Unicode while legacy IPTC uses its Latin-1 representation."""
    child, activity = context
    activity = attrs.evolve(activity, caption="Caption \ufffd", author="Teacher \u2014")
    fields, _, _ = metadata.fields_for({}, child, activity, settings())
    assert fields["XMP-dc:Description"] == "Caption \ufffd"
    assert fields["IPTC:Caption-Abstract"] == "Caption ?"
    assert fields["XMP-dc:Creator"] == "Teacher \u2014"
    assert fields["IPTC:By-line"] == "Teacher ?"


class FakeRunner:
    """Record ExifTool invocations and return configurable read data."""

    def __init__(self, output: str = '[{"EXIF:Make":"Camera"}]') -> None:
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self, arguments: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        return subprocess.CompletedProcess(arguments, 0, self.output, "")


def test_read_and_atomic_enrichment(
    tmp_path: Path,
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """ExifTool reads complete metadata before enriching an atomic copy."""
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"media")
    runner = FakeRunner()
    tool = metadata.ExifTool(runner=runner)
    child, activity = context
    result = tool.enrich(path, child, activity, settings())
    assert result.original == {"EXIF:Make": "Camera"}
    assert result.inferred_time
    assert result.inferred_gps
    assert runner.calls[0][1:4] == ("-j", "-G", "-n")
    assert "-overwrite_original" in runner.calls[1]
    assert runner.calls[2][1:4] == ("-j", "-G", "-n")
    assert path.read_bytes() == b"media"
    assert not path.with_suffix(".jpg.enriching").exists()


def test_enrichment_records_only_fields_retained_by_the_container(
    tmp_path: Path,
    context: tuple[discovery.Child, discovery.Activity],
) -> None:
    """Unsupported requested fields are omitted from the verification contract."""
    path = tmp_path / "video.mp4"
    path.write_bytes(b"media")
    outputs = iter(
        (
            '[{"QuickTime:Duration": 1}]',
            '[{"[XMP-dc]Description": "A caption"}]',
        )
    )

    def run(
        arguments: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        output = "" if "-overwrite_original" in arguments else next(outputs)
        return subprocess.CompletedProcess(arguments, 0, output, "")

    child, activity = context
    result = metadata.ExifTool(runner=run).enrich(
        path,
        child,
        activity,
        settings(),
    )
    assert result.embedded_fields == {"XMP-dc:Description": "A caption"}


@pytest.mark.parametrize(
    ("output", "message"),
    [("not-json", "invalid JSON"), ("[]", "exactly one"), ("[1]", "exactly one")],
)
def test_invalid_exiftool_output(tmp_path: Path, output: str, message: str) -> None:
    """Unexpected ExifTool output is rejected."""
    path = tmp_path / "media"
    path.write_bytes(b"x")
    with pytest.raises(metadata.MetadataError, match=message):
        metadata.ExifTool(runner=FakeRunner(output)).read(path)


@pytest.mark.parametrize("phase", ["read", "write", "readback"])
def test_exiftool_failure_cleans_atomic_copy(
    tmp_path: Path,
    context: tuple[discovery.Child, discovery.Activity],
    phase: str,
) -> None:
    """Missing or failing ExifTool leaves the input and no partial copy."""
    path = tmp_path / "media.jpg"
    path.write_bytes(b"x")
    calls = 0

    def fail(
        arguments: tuple[str, ...], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if phase == "read" or (phase == "write" and calls == 2) or calls == 3:
            if phase == "read":
                raise FileNotFoundError
            raise subprocess.CalledProcessError(1, arguments)
        return subprocess.CompletedProcess(arguments, 0, "[{}]", "")

    child, activity = context
    with pytest.raises(metadata.MetadataError, match="read|enrich"):
        metadata.ExifTool(runner=fail).enrich(path, child, activity, settings())
    assert path.read_bytes() == b"x"
    assert not path.with_suffix(".jpg.enriching").exists()
