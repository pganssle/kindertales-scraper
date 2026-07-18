"""Tests for archive integrity verification."""

import datetime as dt
import json
import sqlite3
from pathlib import Path

import pytest

from kindertales_scraper import archive, discovery, metadata, verify


class FakeExifTool:
    """Return configured embedded metadata or fail."""

    def __init__(
        self, values: dict[str, object] | None = None, *, fail: bool = False
    ) -> None:
        self.values = values or {}
        self.fail = fail

    def read(self, _path: Path) -> dict[str, object]:
        if self.fail:
            raise metadata.MetadataError("ExifTool failed")
        return self.values


def create_archive(
    tmp_path: Path,
    suffix: str = ".jpg",
    embedded_fields: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Create a minimal valid archive and return media and sidecar paths."""
    child = discovery.Child("child", "Alex")
    medium = discovery.MediaReference(
        "media",
        "https://example.test/media",
        "image/jpeg",
        f"media{suffix}",
    )
    activity = discovery.Activity(
        "activity",
        child.id,
        "Art",
        dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        (medium,),
    )
    source = tmp_path / "source"
    source.write_bytes(b"media")
    with archive.Archive(tmp_path / "archive") as store:
        store.upsert_child(child)
        store.upsert_activity(activity)
        media_path = store.store_media(
            archive.StoredMedia(
                medium,
                activity,
                child,
                source,
                archive.sha256(source),
                {},
                embedded_fields or {"XMP-dc:Source": "Kindertales"},
            )
        )
    return media_path, media_path.with_suffix(media_path.suffix + ".json")


def create_record(tmp_path: Path) -> Path:
    """Add one valid synthetic account record and return its snapshot path."""
    record = discovery.Record(
        "record",
        "messages_inbox",
        "https://example.test/?token=secret",
        dt.datetime(2026, 7, 18, tzinfo=dt.UTC),
        {"text": ["Synthetic"]},
    )
    with archive.Archive(tmp_path / "archive") as store:
        return store.store_record(record)


def test_valid_archive(tmp_path: Path) -> None:
    """Matching database, files, sidecars, hashes, and metadata are valid."""
    media_path, _ = create_archive(tmp_path)
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        FakeExifTool({"[XMP-dc]Source": ["Kindertales"]}),  # type: ignore[arg-type]
    ).run()
    assert report == verify.VerificationReport(1, ())
    assert report.valid
    assert media_path.is_file()


@pytest.mark.parametrize("version", [1, 2])
def test_valid_historical_sidecar_version(tmp_path: Path, version: int) -> None:
    """Verification remains compatible with sidecars written by older releases."""
    _, sidecar = create_archive(tmp_path, ".bin")
    document = json.loads(sidecar.read_text(encoding="utf-8"))
    document["version"] = version
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    assert verify.ArchiveVerifier(tmp_path / "archive").run().valid


def test_exif_tag_wins_over_composite_tag_with_same_name(tmp_path: Path) -> None:
    """Composite signed GPS does not mask the stored EXIF coordinate value."""
    create_archive(tmp_path, embedded_fields={"EXIF:GPSLongitude": "71.123"})
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        FakeExifTool(
            {
                "EXIF:GPSLongitude": 71.123,
                "Composite:GPSLongitude": -71.123,
            }
        ),
    ).run()
    assert report.valid


def test_valid_record_snapshot(tmp_path: Path) -> None:
    """Indexed non-media snapshots participate in archive verification."""
    create_archive(tmp_path)
    create_record(tmp_path)
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        FakeExifTool({"[XMP-dc]Source": ["Kindertales"]}),  # type: ignore[arg-type]
    ).run()
    assert report.valid
    assert report.checked_records == 1


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("escape", "escapes"),
        ("missing", "missing"),
        ("invalid", "valid UTF-8 JSON"),
        ("array", "JSON object"),
        ("version", "version"),
        ("id", "ID"),
        ("details", "details"),
        ("indexed-details", "details"),
    ],
)
def test_invalid_record_snapshot(
    tmp_path: Path,
    mutation: str,
    message: str,
) -> None:
    """Record paths, JSON, identity, version, and indexed details are checked."""
    create_archive(tmp_path)
    path = create_record(tmp_path)
    database = tmp_path / "archive/index.sqlite3"
    if mutation == "escape":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE records SET relative_path='../record.json'")
    elif mutation == "missing":
        path.unlink()
    elif mutation == "invalid":
        path.write_bytes(b"\xff")
    elif mutation == "array":
        path.write_text("[]", encoding="utf-8")
    elif mutation == "indexed-details":
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE records SET details_json='not-json'")
    else:
        document = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "version":
            document["version"] = 999
        elif mutation == "id":
            document["id"] = "other"
        else:
            document["details"] = {"different": True}
        path.write_text(json.dumps(document), encoding="utf-8")
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        FakeExifTool({"[XMP-dc]Source": ["Kindertales"]}),  # type: ignore[arg-type]
    ).run()
    assert any(message in issue.message for issue in report.issues)


@pytest.mark.parametrize(
    "actual",
    [
        "2026:07:01 00:00:00+00:00",
        ["2026:07:01 00:00:00+00:00"],
    ],
)
def test_exiftool_datetime_representation_is_normalized(
    tmp_path: Path,
    actual: object,
) -> None:
    """ExifTool's EXIF-style date rendering matches the stored ISO value."""
    create_archive(
        tmp_path,
        embedded_fields={"XMP-photoshop:DateCreated": "2026-07-01T00:00:00+00:00"},
    )
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        FakeExifTool({"XMP:DateCreated": actual}),  # type: ignore[arg-type]
    ).run()
    assert report.valid


def test_invalid_embedded_datetime_differs(tmp_path: Path) -> None:
    """Malformed embedded dates do not bypass metadata verification."""
    create_archive(
        tmp_path,
        embedded_fields={"XMP-photoshop:DateCreated": "not-a-date"},
    )
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        FakeExifTool({"XMP:DateCreated": "also-not-a-date"}),  # type: ignore[arg-type]
    ).run()
    assert report.issues[0].message.startswith("embedded metadata differs")


@pytest.mark.parametrize(
    ("target", "message"),
    [("media", "media file is missing"), ("sidecar", "sidecar file is missing")],
)
def test_missing_files(tmp_path: Path, target: str, message: str) -> None:
    """Missing media and sidecars are reported separately."""
    media_path, sidecar = create_archive(tmp_path)
    (media_path if target == "media" else sidecar).unlink()
    report = verify.ArchiveVerifier(tmp_path / "archive").run()
    assert report.issues[0].message == message
    assert not report.valid


@pytest.mark.parametrize("payload", ["not-json", "[]"])
def test_invalid_sidecar_document(tmp_path: Path, payload: str) -> None:
    """Sidecars must be UTF-8 JSON objects."""
    _, sidecar = create_archive(tmp_path)
    sidecar.write_text(payload, encoding="utf-8")
    report = verify.ArchiveVerifier(tmp_path / "archive").run()
    assert "sidecar" in report.issues[0].message


@pytest.mark.parametrize("version", [True, 999])
def test_sidecar_and_hash_mismatches(tmp_path: Path, version: int) -> None:
    """All sidecar identity and hash invariants are checked."""
    media_path, sidecar = create_archive(tmp_path, ".bin")
    document = json.loads(sidecar.read_text())
    document["version"] = version
    document["source"]["media_id"] = "wrong"
    document["hashes"]["source_sha256"] = "wrong"
    document["hashes"]["final_sha256"] = "wrong"
    document["metadata"]["embedded_fields"] = []
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    media_path.write_bytes(b"tampered")
    report = verify.ArchiveVerifier(tmp_path / "archive").run()
    messages = {issue.message for issue in report.issues}
    assert "unsupported sidecar version" in messages
    assert "sidecar media ID does not match index" in messages
    assert "media SHA-256 does not match index" in messages
    assert "media SHA-256 does not match sidecar" in messages
    assert "embedded_fields must be an object" in messages


def test_source_hash_mismatch_and_unsupported_container(tmp_path: Path) -> None:
    """Source hashes are checked while unsupported containers trust their sidecar."""
    _, sidecar = create_archive(tmp_path, ".bin")
    document = json.loads(sidecar.read_text())
    document["hashes"]["source_sha256"] = "wrong"
    sidecar.write_text(json.dumps(document), encoding="utf-8")
    report = verify.ArchiveVerifier(tmp_path / "archive").run()
    assert report.issues == (
        verify.VerificationIssue("media", "source SHA-256 does not match index"),
    )


@pytest.mark.parametrize(
    ("tool", "message"),
    [
        (FakeExifTool({}), "embedded metadata is missing"),
        (FakeExifTool({"XMP:Source": "Wrong"}), "embedded metadata differs"),
        (FakeExifTool(fail=True), "ExifTool failed"),
    ],
)
def test_embedded_metadata_failure(
    tmp_path: Path,
    tool: FakeExifTool,
    message: str,
) -> None:
    """ExifTool read and missing-field failures are reported."""
    create_archive(tmp_path)
    report = verify.ArchiveVerifier(
        tmp_path / "archive",
        tool,  # type: ignore[arg-type]
    ).run()
    assert message in report.issues[0].message


def test_missing_and_wrong_schema(tmp_path: Path) -> None:
    """A missing index and unsupported schema fail before record checks."""
    report = verify.ArchiveVerifier(tmp_path).run()
    assert report.issues[0].message == "archive index.sqlite3 is missing"
    connection = sqlite3.connect(tmp_path / "index.sqlite3")
    connection.execute("PRAGMA user_version = 999")
    connection.close()
    report = verify.ArchiveVerifier(tmp_path).run()
    assert report.issues[0].message == "unsupported schema version: 999"


def test_failed_sqlite_integrity_check(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A non-ok SQLite integrity result stops record verification."""
    (tmp_path / "index.sqlite3").touch()

    class Cursor:
        def __init__(self, value: object) -> None:
            self.value = value

        def fetchone(self) -> tuple[object]:
            return (self.value,)

    class Connection:
        row_factory: object = None

        def execute(self, statement: str) -> Cursor:
            return Cursor(
                archive.SCHEMA_VERSION
                if "user_version" in statement
                else "page 1 is corrupt"
            )

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        verify.sqlite3, "connect", lambda *_args, **_kwargs: Connection()
    )
    report = verify.ArchiveVerifier(tmp_path).run()
    assert (
        report.issues[0].message == "SQLite integrity check failed: page 1 is corrupt"
    )


@pytest.mark.parametrize("phase", ["open", "read"])
def test_sqlite_database_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    phase: str,
) -> None:
    """Unreadable SQLite files produce an issue rather than a traceback."""
    (tmp_path / "index.sqlite3").touch()
    if phase == "open":

        def connect(*_args: object, **_kwargs: object) -> None:
            raise sqlite3.DatabaseError("broken")

        expected = "cannot open SQLite index: broken"
    else:

        class BrokenConnection:
            row_factory: object = None

            def execute(self, _statement: str) -> None:
                raise sqlite3.DatabaseError("broken")

            def close(self) -> None:
                return None

        def connect(*_args: object, **_kwargs: object) -> BrokenConnection:
            return BrokenConnection()

        expected = "cannot read SQLite index: broken"
    monkeypatch.setattr(verify.sqlite3, "connect", connect)
    report = verify.ArchiveVerifier(tmp_path).run()
    assert report.issues[0].message == expected


def test_escaping_paths_and_orphan_links(tmp_path: Path) -> None:
    """Traversal paths and relationship orphans are reported."""
    create_archive(tmp_path)
    database = tmp_path / "archive" / "index.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "UPDATE media SET relative_path='../outside', sidecar_path='../outside'"
    )
    connection.execute("INSERT INTO activity_media VALUES ('missing', 'media')")
    connection.commit()
    connection.close()
    report = verify.ArchiveVerifier(tmp_path / "archive").run()
    messages = {issue.message for issue in report.issues}
    assert "media path escapes archive root" in messages
    assert "sidecar path escapes archive root" in messages
    assert "1 orphan activity-media links" in messages
