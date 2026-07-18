"""Integrity verification for portable Kindertales archives."""

import datetime as dt
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import attrs

from . import archive, metadata

_EMBEDDABLE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".mp4", ".mov"}
)
_DATETIME_TAGS = frozenset({"CreateDate", "DateCreated", "DateTimeOriginal"})


@attrs.frozen
class VerificationIssue:
    """One archive integrity failure."""

    media_id: str | None
    message: str


@attrs.frozen
class VerificationReport:
    """Aggregate archive verification results."""

    checked_media: int
    issues: tuple[VerificationIssue, ...]
    checked_records: int = 0

    @property
    def valid(self) -> bool:
        """Return whether no integrity failures were found."""
        return not self.issues


@attrs.frozen
class ArchiveVerifier:
    """Validate database, file, sidecar, hash, and embedded metadata contracts."""

    root: Path
    exiftool: metadata.ExifTool = attrs.field(factory=metadata.ExifTool)

    def run(self) -> VerificationReport:
        """Verify every indexed media record without mutating the archive."""
        database = self.root / "index.sqlite3"
        if not database.is_file():
            return VerificationReport(
                0,
                (VerificationIssue(None, "archive index.sqlite3 is missing"),),
            )
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        except sqlite3.DatabaseError as error:
            return VerificationReport(
                0,
                (VerificationIssue(None, f"cannot open SQLite index: {error}"),),
            )
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != archive.SCHEMA_VERSION:
                return VerificationReport(
                    0,
                    (
                        VerificationIssue(
                            None, f"unsupported schema version: {version}"
                        ),
                    ),
                )
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                return VerificationReport(
                    0,
                    (
                        VerificationIssue(
                            None, f"SQLite integrity check failed: {integrity}"
                        ),
                    ),
                )
            rows = connection.execute("SELECT * FROM media ORDER BY id").fetchall()
            issues = [issue for row in rows for issue in self._verify_row(row)]
            record_rows = connection.execute(
                "SELECT * FROM records ORDER BY id"
            ).fetchall()
            issues.extend(
                issue for row in record_rows for issue in self._verify_record(row)
            )
            orphan_count = connection.execute(
                """SELECT count(*) FROM activity_media am
                LEFT JOIN activities a ON a.id=am.activity_id
                LEFT JOIN media m ON m.id=am.media_id
                WHERE a.id IS NULL OR m.id IS NULL"""
            ).fetchone()[0]
            if orphan_count:
                issues.append(
                    VerificationIssue(
                        None, f"{orphan_count} orphan activity-media links"
                    )
                )
            return VerificationReport(len(rows), tuple(issues), len(record_rows))
        except sqlite3.DatabaseError as error:
            return VerificationReport(
                0,
                (VerificationIssue(None, f"cannot read SQLite index: {error}"),),
            )
        finally:
            connection.close()

    def _verify_record(self, row: sqlite3.Row) -> tuple[VerificationIssue, ...]:
        record_id = str(row["id"])
        path = self._contained(str(row["relative_path"]))
        if path is None:
            return (VerificationIssue(record_id, "record path escapes archive root"),)
        if not path.is_file():
            return (VerificationIssue(record_id, "record snapshot is missing"),)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return (VerificationIssue(record_id, "record is not valid UTF-8 JSON"),)
        if not isinstance(document, dict):
            return (VerificationIssue(record_id, "record must contain a JSON object"),)
        issues = []
        if document.get("version") != archive.RECORD_VERSION:
            issues.append(VerificationIssue(record_id, "unsupported record version"))
        if document.get("id") != record_id:
            issues.append(
                VerificationIssue(record_id, "record ID does not match index")
            )
        try:
            indexed_details = json.loads(str(row["details_json"]))
        except json.JSONDecodeError:
            indexed_details = object()
        if document.get("details") != indexed_details:
            issues.append(
                VerificationIssue(record_id, "record details do not match index")
            )
        return tuple(issues)

    def _verify_row(self, row: sqlite3.Row) -> tuple[VerificationIssue, ...]:
        media_id = str(row["id"])
        issues: list[VerificationIssue] = []
        media_path = self._contained(str(row["relative_path"]))
        sidecar_path = self._contained(str(row["sidecar_path"]))
        if media_path is None:
            issues.append(
                VerificationIssue(media_id, "media path escapes archive root")
            )
        elif not media_path.is_file():
            issues.append(VerificationIssue(media_id, "media file is missing"))
        if sidecar_path is None:
            issues.append(
                VerificationIssue(media_id, "sidecar path escapes archive root")
            )
        elif not sidecar_path.is_file():
            issues.append(VerificationIssue(media_id, "sidecar file is missing"))
        if (
            media_path is None
            or sidecar_path is None
            or not media_path.is_file()
            or not sidecar_path.is_file()
        ):
            return tuple(issues)
        try:
            document = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            issues.append(
                VerificationIssue(media_id, "sidecar is not valid UTF-8 JSON")
            )
            return tuple(issues)
        if not isinstance(document, dict):
            issues.append(
                VerificationIssue(media_id, "sidecar must contain a JSON object")
            )
            return tuple(issues)
        issues.extend(self._verify_document(row, media_path, document))
        return tuple(issues)

    def _verify_document(
        self,
        row: sqlite3.Row,
        media_path: Path,
        document: Mapping[str, Any],
    ) -> tuple[VerificationIssue, ...]:
        media_id = str(row["id"])
        issues: list[VerificationIssue] = []
        version = document.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or not archive.MIN_SIDECAR_VERSION <= version <= archive.SIDECAR_VERSION
        ):
            issues.append(VerificationIssue(media_id, "unsupported sidecar version"))
        source = document.get("source")
        if not isinstance(source, dict) or source.get("media_id") != media_id:
            issues.append(
                VerificationIssue(media_id, "sidecar media ID does not match index")
            )
        hashes = document.get("hashes")
        final_hash = archive.sha256(media_path)
        if final_hash != row["final_sha256"]:
            issues.append(
                VerificationIssue(media_id, "media SHA-256 does not match index")
            )
        if not isinstance(hashes, dict) or hashes.get("final_sha256") != final_hash:
            issues.append(
                VerificationIssue(media_id, "media SHA-256 does not match sidecar")
            )
        elif hashes.get("source_sha256") != row["source_sha256"]:
            issues.append(
                VerificationIssue(media_id, "source SHA-256 does not match index")
            )
        issues.extend(self._verify_embedded(media_id, media_path, document))
        return tuple(issues)

    def _verify_embedded(
        self,
        media_id: str,
        media_path: Path,
        document: Mapping[str, Any],
    ) -> tuple[VerificationIssue, ...]:
        metadata_document = document.get("metadata")
        embedded = (
            metadata_document.get("embedded_fields", {})
            if isinstance(metadata_document, dict)
            else {}
        )
        if not isinstance(embedded, dict):
            return (VerificationIssue(media_id, "embedded_fields must be an object"),)
        if media_path.suffix.casefold() not in _EMBEDDABLE_SUFFIXES:
            return ()
        try:
            actual = self.exiftool.read(media_path)
        except metadata.MetadataError as error:
            return (VerificationIssue(media_id, str(error)),)
        actual_by_name: dict[str, object] = {}
        for name, value in actual.items():
            tag = name.rsplit("]", 1)[-1].rsplit(":", 1)[-1]
            actual_by_name.setdefault(tag, value)
        issues = []
        for name, expected in embedded.items():
            tag = name.rsplit("]", 1)[-1].rsplit(":", 1)[-1]
            if tag not in actual_by_name:
                issues.append(
                    VerificationIssue(media_id, f"embedded metadata is missing {name}")
                )
            elif not self._value_matches(actual_by_name[tag], expected, tag=tag):
                issues.append(
                    VerificationIssue(media_id, f"embedded metadata differs for {name}")
                )
        return tuple(issues)

    @staticmethod
    def _value_matches(actual: object, expected: object, *, tag: str) -> bool:
        values = actual if isinstance(actual, list) else [actual]
        if tag in _DATETIME_TAGS:
            expected_datetime = ArchiveVerifier._parse_datetime(expected)
            return expected_datetime is not None and any(
                ArchiveVerifier._parse_datetime(value) == expected_datetime
                for value in values
            )
        return str(expected) in {str(value) for value in values}

    @staticmethod
    def _parse_datetime(value: object) -> dt.datetime | None:
        text = str(value)
        if (
            len(text) >= len("0000:00:00 00:00:00")
            and text[4] == ":"
            and text[7] == ":"
        ):
            text = f"{text[:4]}-{text[5:7]}-{text[8:10]}T{text[11:]}"
        try:
            return dt.datetime.fromisoformat(text)
        except ValueError:
            return None

    def _contained(self, relative: str) -> Path | None:
        candidate = (self.root / relative).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return None
        return candidate
