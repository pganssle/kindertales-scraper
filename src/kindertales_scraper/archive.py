"""Portable SQLite, JSON, and media archive storage."""

import datetime as dt
import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self

import attrs

from . import config, discovery, redaction

SCHEMA_VERSION = 3
SIDECAR_VERSION = 3
RECORD_VERSION = 1
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class ArchiveError(RuntimeError):
    """Raised when an archive operation cannot be completed safely."""


def sha256(path: Path) -> str:
    """Calculate a streaming SHA-256 digest."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


_HOST_METADATA_NAMES = frozenset(
    {
        "Directory",
        "FileAccessDate",
        "FileInodeChangeDate",
        "FileModifyDate",
        "FileName",
        "FilePermissions",
        "SourceFile",
    }
)


def _portable_original_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Remove ExifTool values derived from the local temporary source path."""
    return {
        key: value
        for key, value in metadata.items()
        if key.rsplit(":", 1)[-1] not in _HOST_METADATA_NAMES
    }


def safe_component(value: str) -> str:
    """Convert a remote label or identifier into one safe path component."""
    component = _UNSAFE.sub("-", value).strip(".-")
    component = re.sub(r"-+", "-", component.replace("..", "-"))
    if not component:
        msg = "remote value does not contain a safe path component"
        raise ArchiveError(msg)
    return component[:100]


def media_path(  # noqa: PLR0913 - path formatting requires the complete context
    child: discovery.Child,
    activity: discovery.Activity,
    medium: discovery.MediaReference,
    layout: config.ArchiveLayout | None = None,
    *,
    sequence: int = 1,
    timestamp: dt.datetime | None = None,
) -> Path:
    """Return an archive-relative media path under the configured layout."""
    if layout is None:
        layout = config.ArchiveLayout()
    if sequence < 1:
        msg = "media filename sequence must be positive"
        raise ArchiveError(msg)
    suffix = Path(medium.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    original_name = Path(medium.filename or "").name
    original_stem = Path(original_name).stem
    effective_timestamp = timestamp or activity.occurred_at
    values = {
        "activity_id": safe_component(activity.id),
        "activity_type": safe_component(activity.kind),
        "child_id": safe_component(child.id),
        "child_name": safe_component(child.name),
        "extension": suffix,
        "media_id": safe_component(medium.id),
        "original_name": safe_component(original_name or medium.id),
        "original_stem": safe_component(original_stem or medium.id),
        "sequence": sequence,
        "timestamp": effective_timestamp,
    }
    name = layout.filename_format.format_map(values)
    if Path(name).name != name or safe_component(name) != name:
        msg = "archive.filename_format must produce one safe filename"
        raise ArchiveError(msg)
    formatted_folder = layout.folder_format.format_map(values)
    folder = Path(formatted_folder) if formatted_folder else Path()
    if folder.is_absolute() or any(
        part in {".", ".."} or safe_component(part) != part for part in folder.parts
    ):
        msg = "archive.folder_format must produce a safe relative folder"
        raise ArchiveError(msg)
    calendar_folder = {
        config.FolderFrequency.NONE: (),
        config.FolderFrequency.DAILY: (effective_timestamp.strftime("%Y-%m-%d"),),
        config.FolderFrequency.MONTHLY: (effective_timestamp.strftime("%Y-%m"),),
        config.FolderFrequency.YEARLY: (effective_timestamp.strftime("%Y"),),
    }[layout.folder_frequency]
    return Path("media", folder, *calendar_folder, name)


def sidecar_path(relative_media: Path, layout: config.ArchiveLayout) -> Path:
    """Return the sidecar path for an archive-relative media path."""
    if layout.sidecar_layout is config.SidecarLayout.PARALLEL:
        relative_media = Path("sidecars", *relative_media.parts[1:])
    return relative_media.with_suffix(relative_media.suffix + ".json")


def capture_timestamp(
    original_metadata: Mapping[str, Any],
    fallback: dt.datetime,
) -> dt.datetime:
    """Return the best authentic capture timestamp, or the activity fallback."""
    by_name = {
        name.rsplit("]", 1)[-1].rsplit(":", 1)[-1]: value
        for name, value in original_metadata.items()
    }
    for name in ("DateTimeOriginal", "CreateDate", "DateCreated"):
        value = by_name.get(name)
        if not isinstance(value, str):
            continue
        normalized = value
        if (
            len(normalized) >= len("0000:00:00 00:00:00")
            and normalized[4] == ":"
            and normalized[7] == ":"
        ):
            normalized = (
                f"{normalized[:4]}-{normalized[5:7]}-{normalized[8:10]}"
                f"T{normalized[11:]}"
            )
        try:
            return dt.datetime.fromisoformat(normalized)
        except ValueError:
            continue
    return fallback


@attrs.frozen
class StoredMedia:
    """Data required to commit one downloaded and enriched medium."""

    medium: discovery.MediaReference
    activity: discovery.Activity
    child: discovery.Child
    temporary_path: Path
    source_sha256: str
    original_metadata: Mapping[str, Any]
    embedded_fields: Mapping[str, Any] = attrs.field(factory=dict)
    inferred_time: bool = False
    inferred_gps: bool = False
    http_properties: Mapping[str, Any] = attrs.field(factory=dict)


class Archive:
    """Own the SQLite index and atomic archive filesystem updates."""

    def __init__(
        self,
        root: Path,
        layout: config.ArchiveLayout | None = None,
    ) -> None:
        """Open or create an archive rooted at *root*."""
        self.root = root
        self.layout = layout or config.ArchiveLayout()
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "index.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def __enter__(self) -> Self:
        """Return this open archive."""
        return self

    @classmethod
    def memory(cls, layout: config.ArchiveLayout | None = None) -> Self:
        """Create a schema-compatible in-memory store for a dry run."""
        instance = cls.__new__(cls)
        instance.root = Path()
        instance.layout = layout or config.ArchiveLayout()
        instance.database_path = Path(":memory:")
        instance.connection = sqlite3.connect(":memory:")
        instance.connection.row_factory = sqlite3.Row
        instance.connection.execute("PRAGMA foreign_keys = ON")
        instance._initialize()  # noqa: SLF001 - alternate constructor
        return instance

    def __exit__(self, *_args: object) -> None:
        """Close the archive on context exit."""
        self.close()

    def close(self) -> None:
        """Close the SQLite index."""
        self.connection.close()

    def _initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2, SCHEMA_VERSION}:
            msg = f"unsupported archive schema version: {version}"
            raise ArchiveError(msg)
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS children (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, center_id TEXT,
                available INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS activities (
                id TEXT PRIMARY KEY, child_id TEXT NOT NULL REFERENCES children(id),
                kind TEXT NOT NULL, occurred_at TEXT NOT NULL, caption TEXT,
                author TEXT, center_id TEXT, details_json TEXT NOT NULL DEFAULT '{}',
                available INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS media (
                id TEXT PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                sidecar_path TEXT NOT NULL UNIQUE, content_type TEXT,
                source_url TEXT NOT NULL, source_sha256 TEXT NOT NULL,
                final_sha256 TEXT NOT NULL, inferred_time INTEGER NOT NULL,
                inferred_gps INTEGER NOT NULL, available INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS activity_media (
                activity_id TEXT NOT NULL REFERENCES activities(id),
                media_id TEXT NOT NULL REFERENCES media(id),
                PRIMARY KEY (activity_id, media_id)
            );
            CREATE TABLE IF NOT EXISTS records (
                id TEXT PRIMARY KEY, category TEXT NOT NULL,
                child_id TEXT REFERENCES children(id),
                relative_path TEXT NOT NULL UNIQUE, source_url TEXT NOT NULL,
                observed_at TEXT NOT NULL, title TEXT, details_json TEXT NOT NULL,
                available INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, cursors_json TEXT NOT NULL
            );
            """
        )
        if version == 1:
            self.connection.execute(
                "ALTER TABLE activities ADD COLUMN details_json "
                "TEXT NOT NULL DEFAULT '{}'"
            )
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

    def store_record(
        self,
        record: discovery.Record,
        child: discovery.Child | None = None,
    ) -> Path:
        """Atomically store a structured non-media record and JSON snapshot."""
        if child is not None and child.id != record.child_id:
            msg = "record child does not match its archive context"
            raise ArchiveError(msg)
        owner = safe_component(child.name) if child is not None else "_account"
        relative = Path("records", owner, f"{safe_component(record.category)}.json")
        destination = self.root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.tmp")
        document = {
            "version": RECORD_VERSION,
            "id": record.id,
            "category": record.category,
            "child_id": record.child_id,
            "title": record.title,
            "observed_at": record.observed_at.isoformat(),
            "source_url": redaction.url(record.source_url),
            "details": dict(record.details),
        }
        temporary.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO records
                    (id, category, child_id, relative_path, source_url, observed_at,
                    title, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET category=excluded.category,
                    child_id=excluded.child_id, relative_path=excluded.relative_path,
                    source_url=excluded.source_url,
                    observed_at=excluded.observed_at, title=excluded.title,
                    details_json=excluded.details_json, available=1""",
                    (
                        record.id,
                        record.category,
                        record.child_id,
                        relative.as_posix(),
                        redaction.url(record.source_url),
                        record.observed_at.isoformat(),
                        record.title,
                        json.dumps(record.details, sort_keys=True),
                    ),
                )
                temporary.replace(destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def upsert_child(self, child: discovery.Child) -> None:
        """Insert or update an available child."""
        self.connection.execute(
            """INSERT INTO children (id, name, center_id) VALUES (?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,
            center_id=excluded.center_id, available=1""",
            (child.id, child.name, child.center_id),
        )
        self.connection.commit()

    def upsert_activity(self, activity: discovery.Activity) -> None:
        """Insert or update available activity context."""
        self.connection.execute(
            """INSERT INTO activities
            (id, child_id, kind, occurred_at, caption, author, center_id, details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET child_id=excluded.child_id,
            kind=excluded.kind, occurred_at=excluded.occurred_at,
            caption=excluded.caption, author=excluded.author,
            center_id=excluded.center_id, details_json=excluded.details_json,
            available=1""",
            (
                activity.id,
                activity.child_id,
                activity.kind,
                activity.occurred_at.isoformat(),
                activity.caption,
                activity.author,
                activity.center_id,
                json.dumps(activity.details, sort_keys=True),
            ),
        )
        self.connection.commit()

    def store_media(self, record: StoredMedia) -> Path:
        """Atomically place enriched media, its sidecar, and its index record."""
        previous = self.connection.execute(
            "SELECT relative_path, sidecar_path FROM media WHERE id=?",
            (record.medium.id,),
        ).fetchone()
        relative, sidecar_relative = self._allocate_paths(record)
        destination = self.root / relative
        sidecar = self.root / sidecar_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        temporary_sidecar = sidecar.with_suffix(sidecar.suffix + ".tmp")
        final_hash = sha256(record.temporary_path)
        document = self._sidecar(record, relative, final_hash)
        temporary_sidecar.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        try:
            with self.connection:
                self.connection.execute(
                    """INSERT INTO media
                    (id, relative_path, sidecar_path, content_type, source_url,
                    source_sha256, final_sha256, inferred_time, inferred_gps)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET relative_path=excluded.relative_path,
                    sidecar_path=excluded.sidecar_path,
                    content_type=excluded.content_type, source_url=excluded.source_url,
                    source_sha256=excluded.source_sha256,
                    final_sha256=excluded.final_sha256,
                    inferred_time=excluded.inferred_time,
                    inferred_gps=excluded.inferred_gps, available=1""",
                    (
                        record.medium.id,
                        relative.as_posix(),
                        sidecar_relative.as_posix(),
                        record.medium.content_type,
                        redaction.url(record.medium.url),
                        record.source_sha256,
                        final_hash,
                        record.inferred_time,
                        record.inferred_gps,
                    ),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO activity_media VALUES (?, ?)",
                    (record.activity.id, record.medium.id),
                )
                record.temporary_path.replace(destination)
                temporary_sidecar.replace(sidecar)
        except Exception:
            temporary_sidecar.unlink(missing_ok=True)
            raise
        if previous is not None:
            self._remove_previous(
                Path(str(previous["relative_path"])),
                Path(str(previous["sidecar_path"])),
                relative,
                sidecar_relative,
            )
        return destination

    def _allocate_paths(self, record: StoredMedia) -> tuple[Path, Path]:
        timestamp = capture_timestamp(
            record.original_metadata,
            record.activity.occurred_at,
        )
        previous_candidate: Path | None = None
        sequence = 1
        while True:
            relative = media_path(
                record.child,
                record.activity,
                record.medium,
                self.layout,
                sequence=sequence,
                timestamp=timestamp,
            )
            if relative == previous_candidate:
                msg = "archive.filename_format does not distinguish sequence values"
                raise ArchiveError(msg)
            sidecar_relative = sidecar_path(relative, self.layout)
            media_owner = self.connection.execute(
                "SELECT id FROM media WHERE relative_path=?",
                (relative.as_posix(),),
            ).fetchone()
            sidecar_owner = self.connection.execute(
                "SELECT id FROM media WHERE sidecar_path=?",
                (sidecar_relative.as_posix(),),
            ).fetchone()
            owned_media = media_owner is None or media_owner["id"] == record.medium.id
            owned_sidecar = (
                sidecar_owner is None or sidecar_owner["id"] == record.medium.id
            )
            media_available = owned_media and (
                not (self.root / relative).exists() or media_owner is not None
            )
            sidecar_available = owned_sidecar and (
                not (self.root / sidecar_relative).exists()
                or sidecar_owner is not None
            )
            if media_available and sidecar_available:
                return relative, sidecar_relative
            previous_candidate = relative
            sequence += 1

    def _remove_previous(
        self,
        previous_media: Path,
        previous_sidecar: Path,
        media: Path,
        sidecar: Path,
    ) -> None:
        for previous, current in (
            (previous_media, media),
            (previous_sidecar, sidecar),
        ):
            if previous == current:
                continue
            path = self.root / previous
            path.unlink(missing_ok=True)
            parent = path.parent
            layout_root = self.root / previous.parts[0]
            while parent != layout_root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _sidecar(
        self,
        record: StoredMedia,
        relative: Path,
        final_hash: str,
    ) -> dict[str, Any]:
        return {
            "version": SIDECAR_VERSION,
            "source": {
                "service": "Kindertales",
                "child_id": record.child.id,
                "activity_id": record.activity.id,
                "media_id": record.medium.id,
                "url": redaction.url(record.medium.url),
            },
            "activity": {
                "type": record.activity.kind,
                "occurred_at": record.activity.occurred_at.isoformat(),
                "caption": record.activity.caption,
                "author": record.activity.author,
                "center_id": record.activity.center_id,
                "details": dict(record.activity.details),
            },
            "media": {"caption": record.medium.caption},
            "archive_path": relative.as_posix(),
            "http": dict(record.http_properties),
            "hashes": {
                "source_sha256": record.source_sha256,
                "final_sha256": final_hash,
            },
            "metadata": {
                "original_exiftool": _portable_original_metadata(
                    record.original_metadata
                ),
                "embedded_fields": dict(record.embedded_fields),
                "inferred_time": record.inferred_time,
                "inferred_gps": record.inferred_gps,
            },
        }

    def begin_sync(self) -> str:
        """Record and return a new running sync identifier."""
        run_id = str(uuid.uuid4())
        self.connection.execute(
            "INSERT INTO sync_runs VALUES (?, ?, NULL, 'running', '{}')",
            (run_id, dt.datetime.now(dt.UTC).isoformat()),
        )
        self.connection.commit()
        return run_id

    def link_media(self, activity_id: str, media_id: str) -> None:
        """Associate already archived media with another activity."""
        self.connection.execute(
            "INSERT OR IGNORE INTO activity_media VALUES (?, ?)",
            (activity_id, media_id),
        )
        self.connection.commit()

    def finish_sync(
        self,
        run_id: str,
        status: str,
        cursors: Mapping[str, str],
    ) -> None:
        """Finish a sync with its per-child continuation cursors."""
        self.connection.execute(
            "UPDATE sync_runs SET finished_at=?, status=?, cursors_json=? WHERE id=?",
            (
                dt.datetime.now(dt.UTC).isoformat(),
                status,
                json.dumps(cursors, sort_keys=True),
                run_id,
            ),
        )
        self.connection.commit()

    def latest_cursors(self) -> Mapping[str, str]:
        """Return cursors from the newest successful sync."""
        row = self.connection.execute(
            """SELECT cursors_json FROM sync_runs WHERE status='complete'
            ORDER BY finished_at DESC LIMIT 1"""
        ).fetchone()
        if row is None:
            return {}
        value = json.loads(row["cursors_json"])
        return {str(key): str(cursor) for key, cursor in value.items()}

    def mark_unavailable(self, table: str, present_ids: Sequence[str]) -> None:
        """Mark remotely missing records unavailable without deleting files."""
        if table not in {"children", "activities", "media", "records"}:
            msg = f"cannot mark records in table {table}"
            raise ArchiveError(msg)
        placeholders = ",".join("?" for _ in present_ids)
        if placeholders:
            sql = f"UPDATE {table} SET available=0 WHERE id NOT IN ({placeholders})"  # noqa: S608 - table is checked against the fixed allowlist above.
            self.connection.execute(sql, tuple(present_ids))
        else:
            statements = {
                "children": "UPDATE children SET available=0",
                "activities": "UPDATE activities SET available=0",
                "media": "UPDATE media SET available=0",
                "records": "UPDATE records SET available=0",
            }
            self.connection.execute(statements[table])
        self.connection.commit()
