"""Tests for resumable end-to-end synchronization."""

import asyncio
import datetime as dt
import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import attrs
import httpx
import pytest

from kindertales_scraper import (
    archive,
    config,
    discovery,
    metadata,
    progress,
    scheduler,
    sync,
)


class ImmediateLimiter:
    """Admit requests immediately."""

    async def acquire(self) -> float:
        return 0.0


class FakeAdapter:
    """Return configured discovery records and retain requested bounds."""

    def __init__(
        self,
        children: tuple[discovery.Child, ...],
        activities: tuple[discovery.Activity, ...],
        error: Exception | None = None,
    ) -> None:
        self.children_records = children
        self.activity_records = activities
        self.error = error
        self.bounds: list[tuple[dt.date | None, dt.date | None]] = []

    async def children(self) -> tuple[discovery.Child, ...]:
        if self.error is not None:
            raise self.error
        return self.children_records

    async def activities(
        self,
        child_id: str,
        *,
        cursor: str | None = None,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
        page_complete: Callable[[], None] | None = None,
    ) -> AsyncIterator[discovery.Activity]:
        assert cursor is None
        assert page_complete is None
        self.bounds.append((from_date, through_date))
        for activity in self.activity_records:
            if activity.child_id == child_id:
                yield activity


class FakeRecordAdapter(FakeAdapter):
    """Expose synthetic child, message, and billing record snapshots."""

    def __init__(
        self,
        children: tuple[discovery.Child, ...],
        activities: tuple[discovery.Activity, ...],
    ) -> None:
        super().__init__(children, activities)
        self.account_options: list[tuple[bool, bool]] = []

    async def child_records(
        self,
        child_id: str,
        *,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
        request_complete: Callable[[], None] | None = None,
    ) -> tuple[discovery.Record, ...]:
        self.bounds.append((from_date, through_date))
        if request_complete is not None:
            for _ in range(6):
                request_complete()
        return (self._record("profile", child_id),)

    async def account_records(
        self,
        *,
        messages: bool,
        billing: bool,
        request_complete: Callable[[], None] | None = None,
        requests_discovered: Callable[[int], None] | None = None,
    ) -> tuple[discovery.Record, ...]:
        del requests_discovered
        self.account_options.append((messages, billing))
        if request_complete is not None:
            for _ in range((5 if messages else 0) + (1 if billing else 0)):
                request_complete()
        return tuple(
            self._record(category)
            for category, enabled in (("messages", messages), ("billing", billing))
            if enabled
        )

    @staticmethod
    def _record(category: str, child_id: str | None = None) -> discovery.Record:
        return discovery.Record(
            f"record-{category}-{child_id or 'account'}",
            category,
            f"https://example.test/?pg={category}&token=secret",
            dt.datetime(2026, 7, 18, tzinfo=dt.UTC),
            {"text": ["Synthetic"]},
            child_id,
        )


class CountedFakeAdapter(FakeAdapter):
    """Expose a known one-page-per-date discovery plan."""

    @staticmethod
    def activity_page_count(
        *,
        from_date: dt.date | None,
        through_date: dt.date | None,
    ) -> int:
        assert from_date is not None
        assert through_date is not None
        return (through_date - from_date).days + 1

    async def activities(
        self,
        child_id: str,
        *,
        cursor: str | None = None,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
        page_complete: Callable[[], None] | None = None,
    ) -> AsyncIterator[discovery.Activity]:
        assert from_date is not None
        assert through_date is not None
        assert page_complete is not None
        current = from_date
        while current <= through_date:
            page_complete()
            current += dt.timedelta(days=1)
        async for activity in super().activities(
            child_id,
            cursor=cursor,
            from_date=from_date,
            through_date=through_date,
        ):
            yield activity


class PagedFakeAdapter(FakeAdapter):
    """Expose individual pages and record when discovery reaches each one."""

    def __init__(
        self,
        children: tuple[discovery.Child, ...],
        activities: tuple[discovery.Activity, ...],
        events: list[str],
        page_hook: Callable[[dt.date], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(children, activities)
        self.events = events
        self.page_hook = page_hook

    async def activity_pages(
        self,
        child_id: str,
        *,
        cursor: str | None = None,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
    ) -> AsyncIterator[discovery.ActivityPage]:
        assert cursor is None
        assert from_date is not None
        assert through_date is not None
        current = from_date
        while current <= through_date:
            if self.page_hook is not None:
                await self.page_hook(current)
            self.events.append(f"page:{current.isoformat()}")
            following = current + dt.timedelta(days=1)
            yield discovery.ActivityPage(
                tuple(
                    activity
                    for activity in self.activity_records
                    if activity.child_id == child_id
                    and activity.occurred_at.date() == current
                ),
                following.isoformat() if following <= through_date else None,
            )
            current = following


class StreamingFakeAdapter(PagedFakeAdapter):
    """Expose a known page count in addition to streaming pages."""

    @staticmethod
    def activity_page_count(
        *,
        from_date: dt.date | None,
        through_date: dt.date | None,
    ) -> int:
        assert from_date is not None
        assert through_date is not None
        return (through_date - from_date).days + 1


class FakeDocumentAdapter(FakeRecordAdapter):
    """Expose one child profile record with a standalone attachment."""

    async def child_records(
        self,
        child_id: str,
        *,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
        request_complete: Callable[[], None] | None = None,
    ) -> tuple[discovery.Record, ...]:
        self.bounds.append((from_date, through_date))
        if request_complete is not None:
            for _ in range(6):
                request_complete()
        document = discovery.DocumentReference(
            "document",
            "https://files.example.test/immunization.pdf?token=secret",
            "immunization.pdf",
            "application/pdf",
        )
        return (
            discovery.Record(
                "profile",
                "profile_documents",
                "https://example.test/profile",
                dt.datetime(2026, 7, 18, tzinfo=dt.UTC),
                {"documents": ({"id": document.id},)},
                child_id,
                documents=(document,),
            ),
        )


class FakeEnricher:
    """Enrich media deterministically without an ExifTool installation."""

    def enrich(
        self,
        path: Path,
        _child: discovery.Child,
        _activity: discovery.Activity,
        _settings: config.Config,
    ) -> metadata.Enrichment:
        path.write_bytes(path.read_bytes() + b" enriched")
        return metadata.Enrichment(
            {"original": True},
            {"embedded": "yes"},
            inferred_time=True,
            inferred_gps=False,
        )


class RecordingReporter:
    """Record synchronization progress events."""

    def __init__(self) -> None:
        self.events: list[tuple[str, progress.Stage, int] | tuple[str]] = []

    def start(self, stage: progress.Stage, total: int) -> None:
        self.events.append(("start", stage, total))

    def advance(self, stage: progress.Stage) -> None:
        self.events.append(("advance", stage, 1))

    def extend(self, stage: progress.Stage, amount: int) -> None:
        self.events.append(("extend", stage, amount))

    def close(self) -> None:
        self.events.append(("close",))


@pytest.fixture
def records() -> tuple[discovery.Child, tuple[discovery.Activity, ...]]:
    """Return two activities which reference the same medium."""
    child = discovery.Child("child-1", "Alex")
    medium = discovery.MediaReference(
        "media-1",
        "https://example.test/media.jpg",
        "image/jpeg",
        "media.jpg",
    )
    activities = tuple(
        discovery.Activity(
            f"activity-{day}",
            child.id,
            "Art",
            dt.datetime(2026, 7, day, 10, tzinfo=dt.UTC),
            (medium,),
        )
        for day in (1, 2)
    )
    return child, activities


def settings(tmp_path: Path) -> config.Config:
    """Return isolated synchronization configuration."""
    return config.Config(
        email="a@example.com",
        archive_directory=tmp_path / "archive",
        request_policy=config.RequestPolicy(
            quotas=(config.Quota(100, 1),),
            jitter_fraction=0,
            max_retries=0,
        ),
    )


@pytest.mark.asyncio
async def test_sync_archives_enabled_records(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """Configured child, message, and billing snapshots join the same sync run."""
    child, _activities = records
    configuration = attrs.evolve(
        settings(tmp_path),
        exports=config.Exports(child_records=True, messages=True, billing=True),
    )
    adapter = FakeRecordAdapter((child,), ())
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        with archive.Archive(configuration.archive_directory) as store:
            runner = sync.SyncEngine(
                configuration,
                adapter,
                client,
                store,
                scheduler.Requester(
                    configuration.request_policy, ImmediateLimiter()
                ),
                FakeEnricher(),
            )
            summary = await runner.run(dry_run=False)
            rows = store.connection.execute("SELECT * FROM records").fetchall()
    assert summary.records == 3
    assert len(rows) == 3
    assert adapter.account_options == [(True, True)]
    assert all("REDACTED" in row["source_url"] for row in rows)


@pytest.mark.asyncio
async def test_sync_downloads_standalone_record_documents(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """Discovered profile attachments are streamed, indexed, and verified."""
    child, _activities = records
    adapter = FakeDocumentAdapter((child,), ())
    reporter = RecordingReporter()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"document",
            headers={"Content-Type": "application/pdf"},
        )

    async for runner, store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(handler),
        reporter,
    ):
        summary = await runner.run(
            sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 1))
        )
        row = store.connection.execute("SELECT * FROM documents").fetchone()
        link = store.connection.execute("SELECT * FROM record_documents").fetchone()
    assert summary.records == 1
    assert row["sha256"] == hashlib.sha256(b"document").hexdigest()
    assert link["document_id"] == "document"
    assert ("start", progress.Stage.DOCUMENTS, 0) in reporter.events
    assert ("extend", progress.Stage.DOCUMENTS, 1) in reporter.events
    assert ("advance", progress.Stage.DOCUMENTS, 1) in reporter.events


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", [None, "text/html"])
async def test_sync_rejects_non_document_responses(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
    content_type: str | None,
) -> None:
    """Missing and HTML attachment responses cannot enter the archive."""
    child, _activities = records
    adapter = FakeDocumentAdapter((child,), ())
    headers = {"Content-Type": content_type} if content_type is not None else {}
    async for runner, _store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(
            lambda _request: httpx.Response(200, content=b"bad", headers=headers)
        ),
    ):
        with pytest.raises(ExceptionGroup, match="document downloads failed"):
            await runner.run(
                sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 1))
            )


@pytest.mark.asyncio
async def test_record_discovery_respects_disabled_exports(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """An adapter isn't asked for record areas when every export is disabled."""
    child, _activities = records
    adapter = FakeRecordAdapter((child,), ())
    configuration = attrs.evolve(
        settings(tmp_path),
        exports=config.Exports(child_records=False),
    )
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as client:
        with archive.Archive(configuration.archive_directory) as store:
            runner = sync.SyncEngine(
                configuration,
                adapter,
                client,
                store,
                scheduler.Requester(
                    configuration.request_policy, ImmediateLimiter()
                ),
                FakeEnricher(),
            )
            summary = await runner.run(dry_run=True)
    assert summary.records == 0
    assert adapter.account_options == []


async def engine(
    tmp_path: Path,
    adapter: FakeAdapter,
    handler: httpx.AsyncBaseTransport,
    reporter: progress.Reporter | None = None,
    configuration: config.Config | None = None,
) -> AsyncIterator[tuple[sync.SyncEngine, archive.Archive]]:
    """Yield an engine with open HTTP and archive resources."""
    configuration = configuration or settings(tmp_path)
    async with httpx.AsyncClient(transport=handler) as client:
        with archive.Archive(configuration.archive_directory) as store:
            requester = scheduler.Requester(
                configuration.request_policy, ImmediateLimiter()
            )
            yield (
                sync.SyncEngine(
                    configuration,
                    adapter,
                    client,
                    store,
                    requester,
                    FakeEnricher(),
                    reporter or progress.NullReporter(),
                ),
                store,
            )


@pytest.mark.asyncio
async def test_streaming_sync_prioritizes_media_before_the_next_page(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """New media runs before continuing discovery when one slot is available."""
    child, activities = records
    events: list[str] = []
    adapter = StreamingFakeAdapter((child,), activities, events)
    configuration = settings(tmp_path)
    configuration = attrs.evolve(
        configuration,
        request_policy=attrs.evolve(
            configuration.request_policy,
            max_in_flight=1,
            max_media_downloads=1,
        ),
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        events.append("media")
        return httpx.Response(
            200,
            content=b"source",
            headers={"Content-Type": "image/jpeg"},
        )

    async for runner, store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(handler),
        configuration=configuration,
    ):
        summary = await runner.run(
            sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 2))
        )
        links = store.connection.execute("SELECT * FROM activity_media").fetchall()
    assert events == ["page:2026-07-01", "media", "page:2026-07-02"]
    assert summary == sync.SyncSummary(1, 2, 1, dry_run=False)
    assert len(links) == 2


@pytest.mark.asyncio
async def test_streaming_sync_discovers_while_media_is_downloading(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """A spare slot continues discovery while a media stream remains active."""
    child, activities = records
    events: list[str] = []
    media_started = asyncio.Event()
    second_page = asyncio.Event()

    async def page_hook(current: dt.date) -> None:
        if current == dt.date(2026, 7, 2):
            await media_started.wait()
            second_page.set()

    adapter = StreamingFakeAdapter((child,), activities, events, page_hook)

    async def handler(_request: httpx.Request) -> httpx.Response:
        events.append("media:start")
        media_started.set()
        await second_page.wait()
        events.append("media:finish")
        return httpx.Response(
            200,
            content=b"source",
            headers={"Content-Type": "image/jpeg"},
        )

    async for runner, _store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(handler),
    ):
        await asyncio.wait_for(
            runner.run(sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 2))),
            timeout=2,
        )
    assert events == [
        "page:2026-07-01",
        "media:start",
        "page:2026-07-02",
        "media:finish",
    ]


@pytest.mark.asyncio
async def test_uncounted_stream_extends_discovery_and_media_progress(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """Unknown pagination and newly found media grow their active totals."""
    child, activities = records
    second_medium = attrs.evolve(
        activities[1].media[0],
        id="media-2",
        url="https://example.test/media-2.jpg",
    )
    distinct = (activities[0], attrs.evolve(activities[1], media=(second_medium,)))
    reporter = RecordingReporter()
    adapter = PagedFakeAdapter((child,), distinct, [])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"source",
            headers={"Content-Type": "image/jpeg"},
        )

    async for runner, _store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(handler),
        reporter,
    ):
        await runner.run(sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 2)))
    assert ("extend", progress.Stage.DISCOVERY, 1) in reporter.events
    assert ("extend", progress.Stage.MEDIA, 1) in reporter.events


@pytest.mark.asyncio
async def test_sparse_stream_extends_completed_media_progress(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """A later medium extends the original bar after it reaches its total."""
    child, activities = records
    second_medium = attrs.evolve(
        activities[1].media[0],
        id="media-2",
        url="https://example.test/media-2.jpg",
    )
    distinct = (activities[0], attrs.evolve(activities[1], media=(second_medium,)))
    reporter = RecordingReporter()
    adapter = StreamingFakeAdapter((child,), distinct, [])
    configuration = settings(tmp_path)
    configuration = attrs.evolve(
        configuration,
        request_policy=attrs.evolve(
            configuration.request_policy,
            max_in_flight=1,
            max_media_downloads=1,
        ),
    )
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"source",
            headers={"Content-Type": "image/jpeg"},
        )
    )
    async for runner, _store in engine(
        tmp_path,
        adapter,
        transport,
        reporter,
        configuration,
    ):
        await runner.run(sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 2)))
    assert ("start", progress.Stage.MEDIA, 0) in reporter.events
    assert reporter.events.count(("extend", progress.Stage.MEDIA, 1)) == 2
    assert ("start", progress.Stage.MEDIA, 1) not in reporter.events


@pytest.mark.asyncio
async def test_streaming_sync_filters_bounds_and_reports_media_failure(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """Streaming applies bounds before archiving and groups download failures."""
    child, activities = records
    older = attrs.evolve(
        activities[0],
        id="outside",
        occurred_at=dt.datetime(2026, 6, 30, tzinfo=dt.UTC),
        media=(),
    )

    class MixedPageAdapter(PagedFakeAdapter):
        async def activity_pages(
            self,
            child_id: str,
            *,
            cursor: str | None = None,
            from_date: dt.date | None = None,
            through_date: dt.date | None = None,
        ) -> AsyncIterator[discovery.ActivityPage]:
            del child_id, cursor, from_date, through_date
            yield discovery.ActivityPage((older, activities[0]))

    adapter = MixedPageAdapter((child,), activities, [])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"bad",
            headers={"Content-Type": "text/html"},
        )
    )
    async for runner, store in engine(tmp_path, adapter, transport):
        with pytest.raises(ExceptionGroup, match="archive tasks"):
            await runner.run(
                sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 1))
            )
        archived = store.connection.execute("SELECT id FROM activities").fetchall()
    assert [row["id"] for row in archived] == ["activity-1"]


@pytest.mark.asyncio
async def test_streaming_sync_identifies_exiftool_media_failure(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """Metadata failures identify the source media and activity date."""
    child, activities = records

    class FailingEnricher(FakeEnricher):
        def enrich(
            self,
            path: Path,
            child: discovery.Child,
            activity: discovery.Activity,
            settings: config.Config,
        ) -> metadata.Enrichment:
            del path, child, activity, settings
            raise metadata.MetadataError("ExifTool reported a file format error")

    adapter = PagedFakeAdapter((child,), activities, [])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"bad image",
            headers={"Content-Type": "image/jpeg"},
        )
    )
    async for runner, _store in engine(tmp_path, adapter, transport):
        runner.enricher = FailingEnricher()
        with pytest.raises(ExceptionGroup) as caught:
            await runner.run(
                sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 1))
            )
    failure = str(caught.value.exceptions[0])
    assert "media-1" in failure
    assert "2026-07-01" in failure


@pytest.mark.asyncio
async def test_streaming_sync_records_empty_media_without_failing(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """An empty successful response is indexed as a retryable media failure."""
    child, activities = records
    reporter = RecordingReporter()
    adapter = PagedFakeAdapter((child,), activities, [])
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"",
            headers={"Content-Type": "image/jpeg", "Content-Length": "0"},
        )
    )
    async for runner, store in engine(tmp_path, adapter, transport, reporter):
        summary = await runner.run(
            sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 2))
        )
        failures = store.connection.execute(
            "SELECT * FROM media_failures"
        ).fetchall()
        media_count = store.connection.execute("SELECT count(*) FROM media").fetchone()[
            0
        ]
        run_status = store.connection.execute(
            "SELECT status FROM sync_runs"
        ).fetchone()[0]
    assert summary.media == 1
    assert media_count == 0
    assert run_status == "complete"
    assert len(failures) == 1
    assert failures[0]["reason"] == "empty_response"
    assert reporter.events.count(("advance", progress.Stage.MEDIA, 1)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "returned_content_type",
    ["image/jpeg", "image/jpg", "IMAGE/PJPEG; charset=binary"],
)
async def test_sync_downloads_deduplicates_and_archives(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
    returned_content_type: str,
) -> None:
    """A complete run downloads duplicate media once and links every activity."""
    child, activities = records
    requests = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=b"source",
            headers={"Content-Type": returned_content_type},
        )

    adapter = FakeAdapter((child,), activities)
    reporter = RecordingReporter()
    async for runner, store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(handler),
        reporter,
    ):
        summary = await runner.run()
        media_row = store.connection.execute("SELECT * FROM media").fetchone()
        links = store.connection.execute("SELECT * FROM activity_media").fetchall()
        run = store.connection.execute("SELECT status FROM sync_runs").fetchone()
    assert summary == sync.SyncSummary(1, 2, 1, dry_run=False)
    assert requests == 1
    assert len(links) == 2
    assert run["status"] == "complete"
    assert media_row["source_sha256"] == hashlib.sha256(b"source").hexdigest()
    assert media_row["source_sha256"] != media_row["final_sha256"]
    assert media_row["conflict_sidecar_path"] is None
    assert json.loads(media_row["embedded_fields_json"]) == {"embedded": "yes"}
    assert reporter.events == [
        ("start", progress.Stage.DISCOVERY, 1),
        ("start", progress.Stage.RECORDS, 0),
        ("start", progress.Stage.DOCUMENTS, 0),
        ("start", progress.Stage.MEDIA, 0),
        ("advance", progress.Stage.DISCOVERY, 1),
        ("extend", progress.Stage.MEDIA, 1),
        ("advance", progress.Stage.MEDIA, 1),
        ("close",),
    ]


@pytest.mark.asyncio
async def test_dry_run_filters_without_writing(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """Dry-run applies bounds but does not mutate the archive or request media."""
    child, activities = records
    adapter = FakeAdapter((child,), activities)

    def fail(_request: httpx.Request) -> httpx.Response:
        pytest.fail("dry-run downloaded media")

    async for runner, store in engine(tmp_path, adapter, httpx.MockTransport(fail)):
        summary = await runner.run(
            sync.Bounds(dt.date(2026, 7, 2), dt.date(2026, 7, 2)),
            dry_run=True,
        )
        assert (
            store.connection.execute("SELECT count(*) FROM sync_runs").fetchone()[0]
            == 0
        )
    assert summary == sync.SyncSummary(1, 1, 1, dry_run=True)
    assert adapter.bounds == [(dt.date(2026, 7, 2), dt.date(2026, 7, 2))]


@pytest.mark.asyncio
async def test_discovery_progress_counts_daily_pages(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """A long dated traversal advances once per completed daily request."""
    child, activities = records
    reporter = RecordingReporter()
    adapter = CountedFakeAdapter((child,), activities)
    async for runner, _store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(lambda _request: httpx.Response(500)),
        reporter,
    ):
        await runner.run(
            sync.Bounds(dt.date(2026, 7, 1), dt.date(2026, 7, 3)),
            dry_run=True,
        )
    assert reporter.events[:4] == [
        ("start", progress.Stage.DISCOVERY, 3),
        ("start", progress.Stage.RECORDS, 0),
        ("start", progress.Stage.DOCUMENTS, 0),
        ("start", progress.Stage.MEDIA, 0),
    ]
    assert reporter.events[4:7] == [
        ("advance", progress.Stage.DISCOVERY, 1),
        ("advance", progress.Stage.DISCOVERY, 1),
        ("advance", progress.Stage.DISCOVERY, 1),
    ]


@pytest.mark.asyncio
async def test_explicit_historical_start_precedes_resume_cursor(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """An explicit historical backfill is not narrowed by a newer cursor."""
    child, _ = records
    older = discovery.Activity(
        "older",
        child.id,
        "Note",
        dt.datetime(2026, 7, 11, tzinfo=dt.UTC),
        (),
    )
    adapter = FakeAdapter((child,), (older,))
    before = dt.datetime.now().astimezone().date()
    async for runner, store in engine(
        tmp_path, adapter, httpx.MockTransport(lambda _: httpx.Response(500))
    ):
        run_id = store.begin_sync()
        store.finish_sync(run_id, "complete", {child.id: "2026-07-15T10:00:00+00:00"})
        await runner.run(sync.Bounds(from_date=dt.date(2022, 6, 1)))
        assert store.latest_cursors()[child.id] == "2026-07-15T10:00:00+00:00"
    after = dt.datetime.now().astimezone().date()
    assert adapter.bounds[0][0] == dt.date(2022, 6, 1)
    assert adapter.bounds[0][1] in {before, after}


@pytest.mark.asyncio
async def test_resume_without_explicit_start_uses_overlap(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
) -> None:
    """An omitted start date resumes from the stored cursor with overlap."""
    child, _ = records
    adapter = FakeAdapter((child,), ())
    async for runner, store in engine(
        tmp_path, adapter, httpx.MockTransport(lambda _: httpx.Response(500))
    ):
        run_id = store.begin_sync()
        store.finish_sync(run_id, "complete", {child.id: "2026-07-15T10:00:00+00:00"})
        await runner.run(sync.Bounds(through_date=dt.date(2026, 7, 16)))
    assert adapter.bounds[0] == (dt.date(2026, 7, 8), dt.date(2026, 7, 16))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, "missing"),
        ({"Content-Type": "text/html"}, "text/html"),
        ({"Content-Type": "video/mp4"}, "differs"),
    ],
)
async def test_invalid_media_response_fails_run_and_cleans_temporary(
    tmp_path: Path,
    records: tuple[discovery.Child, tuple[discovery.Activity, ...]],
    headers: dict[str, str],
    expected: str,
) -> None:
    """Unexpected media responses fail the run without leaving partial downloads."""
    child, activities = records
    adapter = FakeAdapter((child,), activities[:1])
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=b"not media", headers=headers)
    )
    async for runner, store in engine(tmp_path, adapter, transport):
        with pytest.raises(ExceptionGroup, match="archive tasks") as caught:
            await runner.run()
        assert expected in str(caught.value.exceptions[0])
        status = store.connection.execute("SELECT status FROM sync_runs").fetchone()[0]
    assert status == "failed"
    assert not tuple(settings(tmp_path).archive_directory.rglob(".tmp/*"))


@pytest.mark.asyncio
async def test_discovery_failure_finishes_sync(
    tmp_path: Path,
) -> None:
    """A discovery failure marks the run failed before propagating."""
    adapter = FakeAdapter((), (), RuntimeError("discovery failed"))
    async for runner, store in engine(
        tmp_path, adapter, httpx.MockTransport(lambda _: httpx.Response(500))
    ):
        with pytest.raises(RuntimeError, match="discovery failed"):
            await runner.run()
        status = store.connection.execute("SELECT status FROM sync_runs").fetchone()[0]
    assert status == "failed"


@pytest.mark.asyncio
async def test_dry_run_discovery_failure_has_no_run(
    tmp_path: Path,
) -> None:
    """A dry-run failure propagates without creating sync state."""
    adapter = FakeAdapter((), (), RuntimeError("discovery failed"))
    async for runner, store in engine(
        tmp_path,
        adapter,
        httpx.MockTransport(lambda _: httpx.Response(500)),
    ):
        with pytest.raises(RuntimeError, match="discovery failed"):
            await runner.run(dry_run=True)
        count = store.connection.execute("SELECT count(*) FROM sync_runs").fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    ("from_date", "through_date"),
    [(dt.date(2026, 2, 2), dt.date(2026, 2, 1))],
)
def test_reversed_bounds(from_date: dt.date, through_date: dt.date) -> None:
    """Reversed bounds are rejected."""
    with pytest.raises(sync.SyncError, match="cannot be later"):
        sync.Bounds(from_date, through_date)


def test_parse_date() -> None:
    """CLI dates are strict ISO dates."""
    assert sync.parse_date("2026-07-01") == dt.date(2026, 7, 1)
    with pytest.raises(ValueError, match="invalid ISO date"):
        sync.parse_date("July 1")
