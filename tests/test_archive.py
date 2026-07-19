"""Tests for portable archive storage."""

import datetime as dt
import json
import sqlite3
from pathlib import Path

import attrs
import pytest

from kindertales_scraper import archive, config, discovery


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
    assert path == Path("media/20260701_093000_01.jpg")
    unsafe = discovery.MediaReference("x", "https://example.test", filename="x.bad!")
    assert archive.media_path(child, activity, unsafe).suffix == ".bin"


@pytest.mark.parametrize(
    ("frequency", "folder"),
    [
        (config.FolderFrequency.NONE, ""),
        (config.FolderFrequency.DAILY, "2026-07-01"),
        (config.FolderFrequency.MONTHLY, "2026-07"),
        (config.FolderFrequency.YEARLY, "2026"),
    ],
)
def test_configurable_media_folders_and_names(
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
    frequency: config.FolderFrequency,
    folder: str,
) -> None:
    """Calendar frequency and safe named fields determine the media path."""
    child, activity, medium = entities
    layout = config.ArchiveLayout(
        folder_frequency=frequency,
        folder_format="{child_name}",
        filename_format=(
            "{child_name}_{activity_type}_{original_stem}_{sequence:02d}{extension}"
        ),
    )
    expected = Path("media/A-Lex")
    if folder:
        expected /= folder
    expected /= "A-Lex_Art-Play_photo_02.jpg"
    assert archive.media_path(child, activity, medium, layout, sequence=2) == expected
    if frequency is config.FolderFrequency.MONTHLY:
        captured = dt.datetime(2020, 2, 3, 4, 5, 6, tzinfo=dt.UTC)
        assert archive.media_path(
            child,
            activity,
            medium,
            layout,
            sequence=2,
            timestamp=captured,
        ).parent == Path("media/A-Lex/2020-02")


@pytest.mark.parametrize(
    ("layout", "message"),
    [
        (
            config.ArchiveLayout(
                filename_format="../{sequence:02d}{extension}"
            ),
            "safe filename",
        ),
        (
            config.ArchiveLayout(
                filename_format="same_{sequence!s:.0}{extension}"
            ),
            "sequence",
        ),
        (
            config.ArchiveLayout(folder_format="{child_name}/../elsewhere"),
            "relative folder",
        ),
    ],
)
def test_invalid_rendered_media_name(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
    layout: config.ArchiveLayout,
    message: str,
) -> None:
    """Templates cannot escape the archive or hide every collision number."""
    child, activity, medium = entities
    if message != "sequence":
        with pytest.raises(archive.ArchiveError, match=message):
            archive.media_path(child, activity, medium, layout)
        return
    first = tmp_path / "first"
    first.write_bytes(b"first")
    second = tmp_path / "second"
    second.write_bytes(b"second")
    with archive.Archive(tmp_path / "archive", layout) as store:
        store.upsert_child(child)
        store.upsert_activity(activity)
        store.store_media(
            archive.StoredMedia(medium, activity, child, first, "source", {})
        )
        other = discovery.MediaReference(
            "other",
            medium.url,
            medium.content_type,
            medium.filename,
        )
        with pytest.raises(archive.ArchiveError, match=message):
            store.store_media(
                archive.StoredMedia(other, activity, child, second, "source", {})
            )


def test_capture_timestamp_and_collision_sequence(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """Authentic capture time names media and equal names receive a sequence."""
    child, activity, medium = entities
    other = discovery.MediaReference(
        "media-2",
        "https://example.test/other.jpg",
        "image/jpeg",
        "other.jpg",
    )
    destinations = []
    with archive.Archive(tmp_path / "archive") as store:
        store.upsert_child(child)
        store.upsert_activity(activity)
        for index, selected in enumerate((medium, other), start=1):
            source = tmp_path / f"source-{index}"
            source.write_bytes(b"media")
            destinations.append(
                store.store_media(
                    archive.StoredMedia(
                        selected,
                        activity,
                        child,
                        source,
                        "source",
                        {"EXIF:DateTimeOriginal": "2020:02:03 04:05:06"},
                    )
                )
            )
        replacement = tmp_path / "replacement"
        replacement.write_bytes(b"replacement")
        replaced = store.store_media(
            archive.StoredMedia(
                medium,
                activity,
                child,
                replacement,
                "source",
                {"EXIF:DateTimeOriginal": "2020:02:03 04:05:06"},
            )
        )
    assert [path.name for path in destinations] == (
        ["20200203_040506_01.jpg", "20200203_040506_02.jpg"]
    )
    assert replaced == destinations[0]
    assert replaced.read_bytes() == b"replacement"


def test_parallel_sidecars_and_layout_migration(
    tmp_path: Path,
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """A later sync moves indexed media and sidecars to the selected layout."""
    child, activity, medium = entities
    root = tmp_path / "archive"
    daily = config.ArchiveLayout(config.FolderFrequency.DAILY)
    first_source = tmp_path / "first"
    first_source.write_bytes(b"first")
    with archive.Archive(root, daily) as store:
        store.upsert_child(child)
        store.upsert_activity(activity)
        old_media = store.store_media(
            archive.StoredMedia(medium, activity, child, first_source, "source", {})
        )
        old_sidecar = old_media.with_suffix(old_media.suffix + ".json")

    parallel = config.ArchiveLayout(
        sidecar_layout=config.SidecarLayout.PARALLEL
    )
    second_source = tmp_path / "second"
    second_source.write_bytes(b"second")
    with archive.Archive(root, parallel) as store:
        new_media = store.store_media(
            archive.StoredMedia(medium, activity, child, second_source, "source", {})
        )
        row = store.connection.execute("SELECT * FROM media").fetchone()
    new_sidecar = root / row["sidecar_path"]
    assert new_media == root / "media/20260701_093000_01.jpg"
    assert new_sidecar == root / "sidecars/20260701_093000_01.jpg.json"
    assert new_media.read_bytes() == b"second"
    assert new_sidecar.is_file()
    assert not old_media.exists()
    assert not old_sidecar.exists()


@pytest.mark.parametrize(
    ("metadata_values", "expected"),
    [
        ({"XMP:CreateDate": "2020-02-03T04:05:06-05:00"}, 2020),
        ({"EXIF:DateTimeOriginal": "invalid"}, 2026),
        ({"EXIF:DateTimeOriginal": ["2020:02:03 04:05:06"]}, 2026),
    ],
)
def test_capture_timestamp_fallbacks(
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
    metadata_values: dict[str, object],
    expected: int,
) -> None:
    """ISO capture values are used while malformed and non-string values fall back."""
    _, activity, _ = entities
    timestamp = archive.capture_timestamp(metadata_values, activity.occurred_at)
    assert timestamp.year == expected


def test_media_path_rejects_nonpositive_sequence(
    entities: tuple[discovery.Child, discovery.Activity, discovery.MediaReference],
) -> None:
    """Collision sequences are one-based."""
    with pytest.raises(archive.ArchiveError, match="positive"):
        archive.media_path(*entities, sequence=0)


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
        activity_row = store.connection.execute("SELECT * FROM activities").fetchone()
    assert version == archive.SCHEMA_VERSION
    assert row["name"] == "Updated"
    assert row["available"] == 1
    assert json.loads(activity_row["details_json"]) == {}


def test_migrates_version_one_archive(tmp_path: Path) -> None:
    """Opening a v1 index adds structured activity details without data loss."""
    path = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE activities (
            id TEXT PRIMARY KEY, child_id TEXT NOT NULL, kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL, caption TEXT, author TEXT, center_id TEXT,
            available INTEGER NOT NULL DEFAULT 1
        );
        PRAGMA user_version = 1;
        """
    )
    connection.close()
    with archive.Archive(tmp_path) as store:
        columns = {
            row["name"]
            for row in store.connection.execute("PRAGMA table_info(activities)")
        }
    assert "details_json" in columns


def test_store_structured_child_and_account_records(tmp_path: Path) -> None:
    """Non-media records have redacted, deterministic JSON snapshots and rows."""
    child = discovery.Child("child-1", "A Child")
    observed = dt.datetime(2026, 7, 18, 12, tzinfo=dt.UTC)
    child_record = discovery.Record(
        "record-1",
        "health/profile",
        "https://example.test/?token=secret",
        observed,
        {"text": ["Synthetic"]},
        child.id,
        "Profile",
    )
    account_record = discovery.Record(
        "record-2",
        "billing",
        "https://example.test/?signature=secret",
        observed,
        {"balance": "synthetic"},
    )
    with archive.Archive(tmp_path) as store:
        store.upsert_child(child)
        child_path = store.store_record(child_record, child)
        account_path = store.store_record(account_record)
        store.store_record(child_record, child)
        rows = store.connection.execute("SELECT * FROM records ORDER BY id").fetchall()
    assert child_path == tmp_path / "records/A-Child/health-profile.json"
    assert account_path == tmp_path / "records/_account/billing.json"
    document = json.loads(child_path.read_text(encoding="utf-8"))
    assert document["source_url"].endswith("token=REDACTED")
    assert json.loads(rows[0]["details_json"]) == {"text": ["Synthetic"]}
    assert rows[1]["source_url"].endswith("signature=REDACTED")


def test_new_record_source_replaces_same_child_category(tmp_path: Path) -> None:
    """A corrected endpoint may supersede an older snapshot at the same path."""
    child = discovery.Child("child-1", "A Child")
    observed = dt.datetime(2026, 7, 19, tzinfo=dt.UTC)
    old = discovery.Record(
        "old",
        "attendance",
        "https://example.test/profile",
        observed,
        {"ui": True},
        child.id,
    )
    new = discovery.Record(
        "new",
        "attendance",
        "https://example.test/feed",
        observed,
        {"events": []},
        child.id,
    )
    with archive.Archive(tmp_path) as store:
        store.upsert_child(child)
        path = store.store_record(old, child)
        assert store.store_record(new, child) == path
        rows = store.connection.execute("SELECT id FROM records").fetchall()
    assert [row["id"] for row in rows] == ["new"]
    assert json.loads(path.read_text(encoding="utf-8"))["details"] == {"events": []}


def test_store_record_rejects_wrong_child_and_cleans_conflict(tmp_path: Path) -> None:
    """Record context mismatches and path collisions cannot leave temporary JSON."""
    child = discovery.Child("child-1", "Child")
    other = discovery.Child("child-2", "Child")
    record = discovery.Record(
        "first", "profile", "https://example.test", dt.datetime.now(dt.UTC), {}
    )
    with archive.Archive(tmp_path) as store:
        with pytest.raises(archive.ArchiveError, match="does not match"):
            store.store_record(record, child)
        store.upsert_child(child)
        store.upsert_child(other)
        store.store_record(attrs.evolve(record, child_id=child.id), child)
        with pytest.raises(sqlite3.IntegrityError):
            store.store_record(
                attrs.evolve(record, id="second", child_id=other.id),
                other,
            )
    assert not tuple(tmp_path.rglob("*.tmp"))


def test_reject_newer_schema(tmp_path: Path) -> None:
    """A newer archive cannot be silently opened with older code."""
    path = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    with pytest.raises(archive.ArchiveError, match="unsupported"):
        archive.Archive(tmp_path)


def test_in_memory_archive_does_not_create_files(tmp_path: Path) -> None:
    """Dry-run storage provides the schema without touching its working directory."""
    with archive.Archive.memory() as store:
        assert store.connection.execute("PRAGMA user_version").fetchone()[0] == 3
    assert not tuple(tmp_path.iterdir())


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
                {
                    "EXIF:Make": "Camera",
                    "File:Directory": "/private/tmp",
                    "File:FileName": "download.tmp",
                    "SourceFile": "/private/tmp/download.tmp",
                },
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
    assert sidecar["activity"]["details"] == {}
    assert sidecar["media"]["caption"] is None
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
