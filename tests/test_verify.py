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


def create_archive(tmp_path: Path, suffix: str = ".jpg") -> tuple[Path, Path]:
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
                {"XMP-dc:Source": "Kindertales"},
            )
        )
    return media_path, media_path.with_suffix(media_path.suffix + ".json")


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


def test_sidecar_and_hash_mismatches(tmp_path: Path) -> None:
    """All sidecar identity and hash invariants are checked."""
    media_path, sidecar = create_archive(tmp_path, ".bin")
    document = json.loads(sidecar.read_text())
    document["version"] = 999
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
