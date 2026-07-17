"""Typed discovery of children, activities, and media references."""

import datetime as dt
import html.parser
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import attrs
import httpx


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


@attrs.frozen
class KindertalesAdapter:
    """Read-only adapter for the routes observed in an authorized session."""

    client: httpx.AsyncClient
    children_path: str = "/api/family/children"
    activities_path: str = "/api/children/{child_id}/activities"

    async def children(self) -> tuple[Child, ...]:
        """Return every child linked to the family account."""
        response = await self.client.get(self.children_path)
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
    ) -> AsyncIterator[Activity]:
        """Yield all activity pages, rejecting repeated cursors."""
        seen: set[str] = set()
        next_cursor = cursor
        while True:
            params = {"cursor": next_cursor} if next_cursor is not None else None
            response = await self.client.get(
                self.activities_path.format(child_id=child_id),
                params=params,
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
