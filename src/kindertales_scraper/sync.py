"""Resumable discovery, download, enrichment, and archive synchronization."""

import datetime as dt
import hashlib
import tempfile
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import attrs
import httpx

from . import (
    archive,
    auth,
    config,
    credentials,
    discovery,
    metadata,
    progress,
    scheduler,
)


class SyncError(RuntimeError):
    """Raised when media cannot be safely synchronized."""


class Adapter(Protocol):
    """Discovery operations required by the synchronization engine."""

    async def children(self) -> tuple[discovery.Child, ...]:
        """Return linked children."""
        ...

    def activities(
        self,
        child_id: str,
        *,
        cursor: str | None = None,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
    ) -> AsyncIterator[discovery.Activity]:
        """Iterate bounded activity history."""
        ...


class Enricher(Protocol):
    """Metadata enrichment operation required by synchronization."""

    def enrich(
        self,
        path: Path,
        child: discovery.Child,
        activity: discovery.Activity,
        settings: config.Config,
    ) -> metadata.Enrichment:
        """Enrich downloaded media."""
        ...


@attrs.frozen
class Bounds:
    """Optional inclusive activity date bounds."""

    from_date: dt.date | None = None
    through_date: dt.date | None = None

    def __attrs_post_init__(self) -> None:
        """Reject reversed date ranges."""
        if (
            self.from_date is not None
            and self.through_date is not None
            and self.from_date > self.through_date
        ):
            msg = "--from cannot be later than --through"
            raise SyncError(msg)


@attrs.frozen
class SyncSummary:
    """Counts from a completed or dry-run synchronization."""

    children: int
    activities: int
    media: int
    dry_run: bool


@attrs.define
class SyncEngine:
    """Coordinate typed discovery, scheduled downloads, and archive writes."""

    settings: config.Config
    adapter: Adapter
    client: httpx.AsyncClient
    store: archive.Archive
    requester: scheduler.Requester
    enricher: Enricher
    reporter: progress.Reporter = attrs.field(factory=progress.NullReporter)

    async def run(
        self,
        bounds: Bounds | None = None,
        *,
        dry_run: bool = False,
    ) -> SyncSummary:
        """Synchronize all exposed media within the effective date bounds."""
        if bounds is None:
            bounds = Bounds()
        run_id = None if dry_run else self.store.begin_sync()
        cursors = dict(self.store.latest_cursors())
        try:
            children = await self.adapter.children()
            self.reporter.start(progress.Stage.DISCOVERY, len(children))
            activities: list[tuple[discovery.Child, discovery.Activity]] = []
            for child in children:
                effective_from = self._effective_from(
                    bounds.from_date, cursors.get(child.id)
                )
                activities.extend(
                    [
                        (child, activity)
                        async for activity in self.adapter.activities(
                            child.id,
                            from_date=effective_from,
                            through_date=bounds.through_date,
                        )
                        if self._included(activity, bounds)
                    ]
                )
                self.reporter.advance(progress.Stage.DISCOVERY)
            media_count = len(
                {medium.id for _, item in activities for medium in item.media}
            )
            if dry_run:
                return SyncSummary(
                    len(children),
                    len(activities),
                    media_count,
                    dry_run=True,
                )
            for child in children:
                self.store.upsert_child(child)
            await self._archive_activities(activities)
            cursor_updates = self._cursor_updates(activities, cursors)
            self.store.mark_unavailable(
                "children", tuple(child.id for child in children)
            )
            if not cursors and bounds.from_date is None and bounds.through_date is None:
                self.store.mark_unavailable(
                    "activities",
                    tuple(activity.id for _, activity in activities),
                )
                self.store.mark_unavailable(
                    "media",
                    tuple(
                        medium.id
                        for _, activity in activities
                        for medium in activity.media
                    ),
                )
            self.store.finish_sync(cast("str", run_id), "complete", cursor_updates)
            return SyncSummary(
                len(children),
                len(activities),
                media_count,
                dry_run=False,
            )
        except Exception:
            if run_id is not None:
                self.store.finish_sync(run_id, "failed", cursors)
            raise
        finally:
            self.reporter.close()

    def _effective_from(
        self, requested: dt.date | None, cursor: str | None
    ) -> dt.date | None:
        resumed = None
        if cursor is not None:
            resumed = dt.datetime.fromisoformat(cursor).date() - dt.timedelta(
                days=self.settings.overlap_days
            )
        if requested is None:
            return resumed
        if resumed is None:
            return requested
        return max(requested, resumed)

    @staticmethod
    def _included(activity: discovery.Activity, bounds: Bounds) -> bool:
        date = activity.occurred_at.date()
        return not (
            (bounds.from_date is not None and date < bounds.from_date)
            or (bounds.through_date is not None and date > bounds.through_date)
        )

    async def _archive_activities(
        self,
        activities: Sequence[tuple[discovery.Child, discovery.Activity]],
    ) -> None:
        graph = scheduler.DAGScheduler(
            self.settings.request_policy.max_in_flight,
            self.settings.request_policy.max_media_downloads,
        )
        first_media: dict[
            str, tuple[discovery.Child, discovery.Activity, discovery.MediaReference]
        ] = {}
        associations: list[tuple[str, str]] = []
        for order, (child, activity) in enumerate(activities):

            async def store_activity(item: discovery.Activity = activity) -> None:
                self.store.upsert_activity(item)

            activity_key = f"activity:{activity.id}"
            graph.add(
                scheduler.Work(activity_key, store_activity, depth=1, order=order)
            )
            for medium in activity.media:
                associations.append((activity.id, medium.id))
                first_media.setdefault(medium.id, (child, activity, medium))
        for order, (media_id, (child, activity, medium)) in enumerate(
            first_media.items()
        ):

            async def download(
                selected_child: discovery.Child = child,
                selected_activity: discovery.Activity = activity,
                selected_medium: discovery.MediaReference = medium,
            ) -> None:
                await self._download(selected_child, selected_activity, selected_medium)
                self.reporter.advance(progress.Stage.MEDIA)

            graph.add(
                scheduler.Work(
                    f"media:{media_id}",
                    download,
                    frozenset({f"activity:{activity.id}"}),
                    media=True,
                    depth=2,
                    order=order,
                )
            )
        self.reporter.start(progress.Stage.MEDIA, len(first_media))
        results = await graph.run()
        failures = tuple(
            result.error
            for result in results.values()
            if isinstance(result.error, Exception)
        )
        if failures:
            msg = "one or more archive tasks failed"
            raise ExceptionGroup(msg, failures)
        for activity_id, media_id in associations:
            self.store.link_media(activity_id, media_id)

    async def _download(
        self,
        child: discovery.Child,
        activity: discovery.Activity,
        medium: discovery.MediaReference,
    ) -> None:
        request = self.client.build_request("GET", medium.url)

        async def send() -> httpx.Response:
            return await self.client.send(request, stream=True)

        response = await self.requester.request(send, media=True)
        temporary: Path | None = None
        try:
            response.raise_for_status()
            content_type = (
                response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            )
            canonical_content_type = _canonical_media_type(content_type)
            if not canonical_content_type.startswith(("image/", "video/")):
                received = content_type or "missing"
                msg = f"refusing unexpected media content type: {received}"
                raise SyncError(msg)
            if medium.content_type is not None and (
                canonical_content_type != _canonical_media_type(medium.content_type)
            ):
                msg = f"media content type differs from discovery: {content_type}"
                raise SyncError(msg)
            temporary_directory = self.store.root / ".tmp"
            temporary_directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=temporary_directory, delete=False
            ) as stream:
                temporary = Path(stream.name)
                digest = hashlib.sha256()
                async for chunk in response.aiter_bytes():
                    stream.write(chunk)
                    digest.update(chunk)
            enrichment = self.enricher.enrich(
                temporary,
                child,
                activity,
                self.settings,
            )
            self.store.store_media(
                archive.StoredMedia(
                    medium,
                    activity,
                    child,
                    temporary,
                    digest.hexdigest(),
                    enrichment.original,
                    enrichment.embedded_fields,
                    enrichment.inferred_time,
                    enrichment.inferred_gps,
                    {
                        "content_type": content_type,
                        "content_length": response.headers.get("Content-Length"),
                        "etag": response.headers.get("ETag"),
                    },
                )
            )
        finally:
            await response.aclose()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _cursor_updates(
        activities: Sequence[tuple[discovery.Child, discovery.Activity]],
        existing: Mapping[str, str],
    ) -> dict[str, str]:
        updated = dict(existing)
        for child, activity in activities:
            current = updated.get(child.id)
            value = activity.occurred_at.isoformat()
            if current is None or dt.datetime.fromisoformat(
                value
            ) > dt.datetime.fromisoformat(current):
                updated[child.id] = value
        return updated


def parse_date(value: str) -> dt.date:
    """Parse an ISO date for argparse."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as error:
        msg = f"invalid ISO date: {value}"
        raise ValueError(msg) from error


def _cookies(state: auth.State) -> httpx.Cookies:  # pragma: no cover - live boundary
    cookies = httpx.Cookies()
    raw_cookies = state.get("cookies", ())
    if not isinstance(raw_cookies, Sequence):
        return cookies
    for item in raw_cookies:
        if (
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str)
        ):
            cookies.set(
                item["name"],
                item["value"],
                domain=str(item.get("domain", "app.kindertales.com")),
                path=str(item.get("path", "/")),
            )
    return cookies


async def run_configured(  # pragma: no cover - exercised by the authorized smoke run
    settings: config.Config,
    bounds: Bounds,
    *,
    dry_run: bool,
    headed: bool,
) -> SyncSummary:
    """Authenticate and run synchronization using production components."""
    password, _persistent = credentials.password(settings.email)
    login = auth.PlaywrightLogin()
    manager = auth.SessionManager(auth.SessionCache(settings))

    async def validate(state: auth.State) -> bool:
        async with httpx.AsyncClient(
            base_url="https://app.kindertales.com",
            cookies=_cookies(state),
            follow_redirects=False,
        ) as validation_client:
            response = await validation_client.get("/index.php?pg=dashboard")
            return response.status_code == httpx.codes.OK and not response.is_redirect

    async def authenticate() -> auth.State:
        return await login.authenticate(
            settings.email,
            password,
            headed=headed,
        )

    state = await manager.state(validate, authenticate)
    async with httpx.AsyncClient(
        base_url="https://app.kindertales.com",
        cookies=_cookies(state),
        follow_redirects=False,
    ) as client:
        limiter = scheduler.RollingLimiter(settings.request_policy)
        requester = scheduler.Requester(settings.request_policy, limiter)
        with archive.Archive(settings.archive_directory) as store:
            engine = SyncEngine(
                settings,
                discovery.LegacyKindertalesAdapter(client, requester=requester),
                client,
                store,
                requester,
                metadata.ExifTool(),
                progress.TerminalReporter(),
            )
            return await engine.run(bounds, dry_run=dry_run)


def _canonical_media_type(value: str) -> str:
    normalized = value.strip().casefold()
    return {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}.get(
        normalized,
        normalized,
    )
