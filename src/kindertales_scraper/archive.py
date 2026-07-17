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

from . import discovery, redaction

SCHEMA_VERSION = 1
SIDECAR_VERSION = 1
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


def safe_component(value: str) -> str:
    """Convert a remote label or identifier into one safe path component."""
    component = _UNSAFE.sub("-", value).strip(".-")
    component = re.sub(r"-+", "-", component.replace("..", "-"))
    if not component:
        msg = "remote value does not contain a safe path component"
        raise ArchiveError(msg)
    return component[:100]


def media_path(
    child: discovery.Child,
    activity: discovery.Activity,
    medium: discovery.MediaReference,
) -> Path:
    """Return a deterministic archive-relative media path."""
    suffix = Path(medium.filename or "").suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
        suffix = ".bin"
    child_part = safe_component(f"{child.name}-{child.id}")
    activity_part = safe_component(f"{activity.kind}-{activity.id}")
    name = f"{safe_component(medium.id)}{suffix}"
    return Path(
        "media",
        child_part,
        activity.occurred_at.date().isoformat(),
        activity_part,
        name,
    )


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

    def __init__(self, root: Path) -> None:
        """Open or create an archive rooted at *root*."""
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.database_path = root / "index.sqlite3"
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._initialize()

    def __enter__(self) -> Self:
        """Return this open archive."""
        return self

    def __exit__(self, *_args: object) -> None:
        """Close the archive on context exit."""
        self.close()

    def close(self) -> None:
        """Close the SQLite index."""
        self.connection.close()

    def _initialize(self) -> None:
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, SCHEMA_VERSION}:
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
                author TEXT, center_id TEXT, available INTEGER NOT NULL DEFAULT 1
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
            CREATE TABLE IF NOT EXISTS sync_runs (
                id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, cursors_json TEXT NOT NULL
            );
            """
        )
        self.connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.connection.commit()

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
            (id, child_id, kind, occurred_at, caption, author, center_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET child_id=excluded.child_id,
            kind=excluded.kind, occurred_at=excluded.occurred_at,
            caption=excluded.caption, author=excluded.author,
            center_id=excluded.center_id, available=1""",
            (
                activity.id,
                activity.child_id,
                activity.kind,
                activity.occurred_at.isoformat(),
                activity.caption,
                activity.author,
                activity.center_id,
            ),
        )
        self.connection.commit()

    def store_media(self, record: StoredMedia) -> Path:
        """Atomically place enriched media, its sidecar, and its index record."""
        relative = media_path(record.child, record.activity, record.medium)
        destination = self.root / relative
        sidecar = destination.with_suffix(destination.suffix + ".json")
        destination.parent.mkdir(parents=True, exist_ok=True)
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
                        sidecar.relative_to(self.root).as_posix(),
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
        return destination

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
            },
            "archive_path": relative.as_posix(),
            "http": dict(record.http_properties),
            "hashes": {
                "source_sha256": record.source_sha256,
                "final_sha256": final_hash,
            },
            "metadata": {
                "original_exiftool": dict(record.original_metadata),
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
        if table not in {"children", "activities", "media"}:
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
            }
            self.connection.execute(statements[table])
        self.connection.commit()
