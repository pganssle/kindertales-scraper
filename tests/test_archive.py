"""Tests for portable archive storage."""

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from kindertales_scraper import archive, discovery


@pytest.fixture
def entities() -> tuple[discovery.Child, discovery.Activity, discovery.MediaReference]:
    """Return related synthetic archive entities."""
    child = discovery.Child("child/1", "A ../ Lex", "center")
    medium = discovery.MediaReference(
        "media/1",
        "https://example.test/p.jpg?token=secret",
        "image/jpeg",
        "../../photo.JPG",
    )
    activity = discovery.Activity(
        "activity/1",
        child.id,
        "Art / Play",
        dt.datetime(2026, 7, 1, 9, 30, tzinfo=dt.UTC),
        (medium,),
        "Caption",
        "Teacher",
        "center",
    )
    return child, activity, medium


def test_path_safety_and_fallback_extension(
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """Remote path data cannot escape the media root."""
    child, activity, medium = entities
    path = archive.media_path(child, activity, medium)
    assert path == Path(
        "media/A-Lex-child-1/2026-07-01/Art-Play-activity-1/media-1.jpg"
    )
    unsafe = discovery.MediaReference("x", "https://example.test", filename="x.bad!")
    assert archive.media_path(child, activity, unsafe).suffix == ".bin"


@pytest.mark.parametrize("value", ["..", "///", "..."])
def test_empty_safe_component(value: str) -> None:
    """Components with no safe characters are rejected."""
    with pytest.raises(archive.ArchiveError, match="safe path"):
        archive.safe_component(value)


def test_schema_and_upserts(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """The versioned schema updates child and activity records idempotently."""
    child, activity, _ = entities
    with archive.Archive(tmp_path) as store:
        store.upsert_child(child)
        store.upsert_child(discovery.Child(child.id, "Updated"))
        store.upsert_activity(activity)
        store.upsert_activity(activity)
        version = store.connection.execute("PRAGMA user_version").fetchone()[0]
        row = store.connection.execute("SELECT * FROM children").fetchone()
    assert version == archive.SCHEMA_VERSION
    assert row["name"] == "Updated"
    assert row["available"] == 1


def test_reject_newer_schema(tmp_path: Path) -> None:
    """A newer archive cannot be silently opened with older code."""
    path = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(archive.ArchiveError, match="unsupported"):
        archive.Archive(tmp_path)


def test_store_media_and_sidecar(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """Media, authoritative sidecar, and database hashes commit together."""
    child, activity, medium = entities
    source = tmp_path / "download.tmp"
    source.write_bytes(b"enriched media")
    source_hash = archive.sha256(source)
    with archive.Archive(tmp_path / "archive") as store:
        store.upsert_child(child)
        store.upsert_activity(activity)
        destination = store.store_media(
            archive.StoredMedia(
                medium,
                activity,
                child,
                source,
                source_hash,
                {"EXIF:Make": "Camera"},
                {"XMP:Description": "Caption"},
                inferred_time=True,
                inferred_gps=True,
                http_properties={"content_type": "image/jpeg"},
            )
        )
        row = store.connection.execute("SELECT * FROM media").fetchone()
        link = store.connection.execute("SELECT * FROM activity_media").fetchone()
    assert destination.read_bytes() == b"enriched media"
    sidecar = json.loads(
        destination.with_suffix(".jpg.json").read_text(encoding="utf-8")
    )
    assert sidecar["version"] == archive.SIDECAR_VERSION
    assert sidecar["source"]["url"].endswith("token=REDACTED")
    assert sidecar["metadata"]["original_exiftool"] == {"EXIF:Make": "Camera"}
    assert row["source_sha256"] == source_hash == row["final_sha256"]
    assert link["activity_id"] == activity.id


def test_store_media_cleans_temporary_sidecar_on_failure(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """A database failure does not leave a partial sidecar."""
    child, activity, medium = entities
    source = tmp_path / "source"
    source.write_bytes(b"data")
    with (
        archive.Archive(tmp_path / "archive") as store,
        pytest.raises(sqlite3.IntegrityError),
    ):
        store.store_media(
            archive.StoredMedia(
                medium, activity, child, source, archive.sha256(source), {}
            )
        )
    assert not tuple((tmp_path / "archive").rglob("*.tmp"))


def test_sync_state_and_availability(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """Completed cursors resume and missing records become unavailable."""
    child, activity, _ = entities
    with archive.Archive(tmp_path) as store:
        assert store.latest_cursors() == {}
        store.upsert_child(child)
        store.upsert_activity(activity)
        failed = store.begin_sync()
        store.finish_sync(failed, "failed", {"ignored": "cursor"})
        complete = store.begin_sync()
        store.finish_sync(complete, "complete", {child.id: "cursor"})
        assert store.latest_cursors() == {child.id: "cursor"}
        store.mark_unavailable("children", ())
        store.mark_unavailable("activities", (activity.id,))
        children_available = store.connection.execute(
            "SELECT available FROM children"
        ).fetchone()[0]
        activity_available = store.connection.execute(
            "SELECT available FROM activities"
        ).fetchone()[0]
    assert children_available == 0
    assert activity_available == 1


def test_mark_unavailable_rejects_table(tmp_path: Path) -> None:
    """Dynamic SQL is restricted to the archive's known record tables."""
    with (
        archive.Archive(tmp_path) as store,
        pytest.raises(archive.ArchiveError, match="cannot mark"),
    ):
        store.mark_unavailable("sync_runs", ())
