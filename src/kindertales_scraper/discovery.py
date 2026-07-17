"""Typed discovery of children, activities, and media references."""

import datetime as dt
import hashlib
import html.parser
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from zoneinfo import ZoneInfo

import attrs
import httpx

from . import scheduler


class DiscoveryError(ValueError):
    """Raised when Kindertales discovery data is malformed."""


@attrs.frozen
class Child:
    """A child linked to the authorized family account."""

    id: str
    name: str
    center_id: str | None = None


@attrs.frozen
class MediaReference:
    """A photo or video exposed by an activity."""

    id: str
    url: str
    content_type: str | None = None
    filename: str | None = None


@attrs.frozen
class Activity:
    """Activity context associated with zero or more media objects."""

    id: str
    child_id: str
    kind: str
    occurred_at: dt.datetime
    media: tuple[MediaReference, ...]
    caption: str | None = None
    author: str | None = None
    center_id: str | None = None


@attrs.frozen
class ActivityPage:
    """One page of activities and its continuation cursor."""

    activities: tuple[Activity, ...]
    next_cursor: str | None = None


def _required_string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        msg = f"{key} must be a non-empty string"
        raise DiscoveryError(msg)
    return value


def parse_children(payload: Mapping[str, Any]) -> tuple[Child, ...]:
    """Parse a sanitized family/children JSON response."""
    raw_children = payload.get("children")
    if not isinstance(raw_children, Sequence) or isinstance(raw_children, str | bytes):
        msg = "children must be an array"
        raise DiscoveryError(msg)
    children = []
    for raw_child in raw_children:
        if not isinstance(raw_child, dict):
            msg = "each child must be an object"
            raise DiscoveryError(msg)
        center_id = raw_child.get("center_id")
        children.append(
            Child(
                id=_required_string(raw_child, "id"),
                name=_required_string(raw_child, "name"),
                center_id=str(center_id) if center_id is not None else None,
            )
        )
    return tuple(children)


def _parse_time(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as error:
        msg = "occurred_at must be an ISO 8601 datetime"
        raise DiscoveryError(msg) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        msg = "occurred_at must include a timezone offset"
        raise DiscoveryError(msg)
    return parsed


def parse_activity_page(payload: Mapping[str, Any]) -> ActivityPage:
    """Parse a sanitized activity page response."""
    raw_activities = payload.get("activities")
    if not isinstance(raw_activities, Sequence) or isinstance(
        raw_activities, str | bytes
    ):
        msg = "activities must be an array"
        raise DiscoveryError(msg)
    activities = []
    for item in raw_activities:
        if not isinstance(item, dict):
            msg = "each activity must be an object"
            raise DiscoveryError(msg)
        raw_media = item.get("media", ())
        if not isinstance(raw_media, Sequence) or isinstance(raw_media, str | bytes):
            msg = "activity media must be an array"
            raise DiscoveryError(msg)
        media = []
        for medium in raw_media:
            if not isinstance(medium, dict):
                msg = "each media reference must be an object"
                raise DiscoveryError(msg)
            media.append(
                MediaReference(
                    id=_required_string(medium, "id"),
                    url=_required_string(medium, "url"),
                    content_type=str(medium["content_type"])
                    if medium.get("content_type") is not None
                    else None,
                    filename=str(medium["filename"])
                    if medium.get("filename") is not None
                    else None,
                )
            )
        activities.append(
            Activity(
                id=_required_string(item, "id"),
                child_id=_required_string(item, "child_id"),
                kind=_required_string(item, "type"),
                occurred_at=_parse_time(_required_string(item, "occurred_at")),
                media=tuple(media),
                caption=str(item["caption"])
                if item.get("caption") is not None
                else None,
                author=str(item["author"]) if item.get("author") is not None else None,
                center_id=str(item["center_id"])
                if item.get("center_id") is not None
                else None,
            )
        )
    cursor = payload.get("next_cursor")
    return ActivityPage(
        tuple(activities),
        str(cursor) if cursor is not None else None,
    )


class _ChildHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.children: list[Child] = []

    def handle_starttag(
        self,
        _tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = dict(attrs)
        child_id = values.get("data-child-id")
        name = values.get("data-child-name")
        if child_id and name:
            self.children.append(Child(child_id, name, values.get("data-center-id")))


def parse_children_html(document: str) -> tuple[Child, ...]:
    """Extract child data attributes when no JSON endpoint is available."""
    parser = _ChildHTMLParser()
    parser.feed(document)
    return tuple(parser.children)


def _classes(attributes: Mapping[str, str | None]) -> frozenset[str]:
    return frozenset((attributes.get("class") or "").split())


@attrs.define
class _Anchor:
    href: str
    classes: frozenset[str]
    title: str | None
    text: list[str] = attrs.field(factory=list)
    child_name: list[str] = attrs.field(factory=list)


class _LegacyDashboardParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[_Anchor] = []
        self._anchor: _Anchor | None = None
        self._child_name_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        if tag == "a" and self._anchor is None:
            self._anchor = _Anchor(
                attributes.get("href") or "",
                _classes(attributes),
                attributes.get("title"),
            )
        if self._anchor is not None and "childName" in _classes(attributes):
            self._child_name_depth += 1

    def handle_data(self, data: str) -> None:
        if self._anchor is None:
            return
        self._anchor.text.append(data)
        if self._child_name_depth:
            self._anchor.child_name.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._anchor is None:
            return
        if self._child_name_depth and tag != "a":
            self._child_name_depth -= 1
        if tag == "a":
            self.anchors.append(self._anchor)
            self._anchor = None
            self._child_name_depth = 0


def _child_id(href: str) -> str | None:
    values = parse_qs(urlsplit(href).query).get("cid")
    return values[0] if values and values[0] else None


def parse_legacy_children(document: str) -> tuple[Child, ...]:
    """Extract linked children from the authenticated dashboard navigation."""
    parser = _LegacyDashboardParser()
    parser.feed(document)
    names: dict[str, str] = {}
    linked_ids: list[str] = []
    for anchor in parser.anchors:
        child_id = _child_id(anchor.href)
        if child_id is None:
            continue
        name = " ".join("".join(anchor.child_name).split())
        if name:
            names.setdefault(child_id, name)
        text = " ".join("".join(anchor.text).split()).casefold()
        if "daily activity" in text and child_id not in linked_ids:
            linked_ids.append(child_id)
    child_ids = list(names)
    child_ids.extend(child_id for child_id in linked_ids if child_id not in names)
    return tuple(
        Child(child_id, names.get(child_id, f"Child {index}"))
        for index, child_id in enumerate(child_ids, start=1)
    )


@attrs.define
class _LegacyBox:
    identifier: str
    title: list[str] = attrs.field(factory=list)
    media: list[_Anchor] = attrs.field(factory=list)


class _LegacyActivityParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.boxes: list[_LegacyBox] = []
        self._box: _LegacyBox | None = None
        self._box_depth = 0
        self._title_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        classes = _classes(attributes)
        if self._box is None and tag == "div" and "contentBoxes" in classes:
            self._box = _LegacyBox(attributes.get("id") or "activity")
            self._box_depth = 1
        elif self._box is not None and tag == "div":
            self._box_depth += 1
        if self._box is not None and "enrollmentTitle" in classes:
            self._title_depth = self._box_depth
        if (
            self._box is not None
            and tag == "a"
            and {"html5lightbox", "image_video"} <= classes
        ):
            href = attributes.get("href")
            if href:
                self._box.media.append(
                    _Anchor(href, classes, attributes.get("title"))
                )

    def handle_data(self, data: str) -> None:
        if self._box is not None and self._title_depth:
            self._box.title.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._box is None or tag != "div":
            return
        if self._title_depth == self._box_depth:
            self._title_depth = 0
        self._box_depth -= 1
        if self._box_depth == 0:
            self.boxes.append(self._box)
            self._box = None


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _content_type(path: str) -> str | None:
    return {
        ".gif": "image/gif",
        ".jpeg": "image/jpeg",
        ".jpg": "image/jpeg",
        ".mov": "video/quicktime",
        ".mp4": "video/mp4",
        ".png": "image/png",
        ".webp": "image/webp",
    }.get(PurePosixPath(path).suffix.casefold())


def parse_legacy_activities(
    document: str,
    child_id: str,
    activity_date: dt.date,
    timezone: dt.tzinfo,
) -> tuple[Activity, ...]:
    """Extract activity context and media from one legacy daily report."""
    parser = _LegacyActivityParser()
    parser.feed(document)
    activities = []
    for index, box in enumerate(parser.boxes):
        kind = " ".join("".join(box.title).split()) or box.identifier
        activity_id = _stable_id(
            child_id,
            activity_date.isoformat(),
            box.identifier,
            str(index),
        )
        media = []
        captions = set()
        for anchor in box.media:
            split_url = urlsplit(anchor.href)
            source_path = unquote(split_url.path)
            if anchor.title and anchor.title.strip():
                captions.add(anchor.title.strip())
            media.append(
                MediaReference(
                    _stable_id(split_url.netloc, source_path),
                    anchor.href,
                    _content_type(source_path),
                    PurePosixPath(source_path).name or None,
                )
            )
        activities.append(
            Activity(
                activity_id,
                child_id,
                kind,
                dt.datetime.combine(activity_date, dt.time(), timezone),
                tuple(media),
                next(iter(captions)) if len(captions) == 1 else None,
            )
        )
    return tuple(activities)


@attrs.frozen
class LegacyKindertalesAdapter:
    """Read-only adapter for Kindertales' authenticated legacy HTML pages."""

    client: httpx.AsyncClient
    requester: scheduler.Requester | None = None
    timezone: dt.tzinfo = attrs.field(factory=lambda: ZoneInfo("America/New_York"))

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send discovery through the configured quota and retry boundary."""

        async def send() -> httpx.Response:
            return await self.client.get(path, params=params)

        if self.requester is None:
            return await send()
        return await self.requester.request(send)

    async def children(self) -> tuple[Child, ...]:
        """Return every child linked from the authenticated dashboard."""
        response = await self.get("/index.php", params={"pg": "dashboard"})
        response.raise_for_status()
        return parse_legacy_children(response.text)

    async def activities(
        self,
        child_id: str,
        *,
        cursor: str | None = None,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
    ) -> AsyncIterator[Activity]:
        """Yield daily-report activities over an inclusive bounded range."""
        del cursor
        if from_date is None or through_date is None:
            msg = "legacy Kindertales discovery requires --from and --through"
            raise DiscoveryError(msg)
        current = from_date
        while current <= through_date:
            response = await self.get(
                "/index.php",
                params={
                    "pg": "dailyreport",
                    "cid": child_id,
                    "activitydate": current.strftime("%m/%d/%Y"),
                },
            )
            response.raise_for_status()
            for activity in parse_legacy_activities(
                response.text,
                child_id,
                current,
                self.timezone,
            ):
                yield activity
            current += dt.timedelta(days=1)


@attrs.frozen
class KindertalesAdapter:
    """Read-only adapter for the routes observed in an authorized session."""

    client: httpx.AsyncClient
    children_path: str = "/api/family/children"
    activities_path: str = "/api/children/{child_id}/activities"
    requester: scheduler.Requester | None = None

    async def get(
        self,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send discovery through the configured quota and retry boundary."""

        async def send() -> httpx.Response:
            return await self.client.get(path, params=params)

        if self.requester is None:
            return await send()
        return await self.requester.request(send)

    async def children(self) -> tuple[Child, ...]:
        """Return every child linked to the family account."""
        response = await self.get(self.children_path)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            msg = "children response must be an object"
            raise DiscoveryError(msg)
        return parse_children(payload)

    async def activities(
        self,
        child_id: str,
        *,
        cursor: str | None = None,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
    ) -> AsyncIterator[Activity]:
        """Yield all activity pages, rejecting repeated cursors."""
        seen: set[str] = set()
        next_cursor = cursor
        while True:
            params = {}
            if next_cursor is not None:
                params["cursor"] = next_cursor
            if from_date is not None:
                params["from"] = from_date.isoformat()
            if through_date is not None:
                params["through"] = through_date.isoformat()
            response = await self.get(
                self.activities_path.format(child_id=child_id),
                params=params or None,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                msg = "activities response must be an object"
                raise DiscoveryError(msg)
            page = parse_activity_page(payload)
            for activity in page.activities:
                yield activity
            if page.next_cursor is None:
                return
            if page.next_cursor in seen:
                msg = "activity pagination repeated a cursor"
                raise DiscoveryError(msg)
            seen.add(page.next_cursor)
            next_cursor = page.next_cursor
