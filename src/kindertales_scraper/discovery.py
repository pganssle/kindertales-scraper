"""Typed discovery of children, activities, and media references."""

import datetime as dt
import hashlib
import html.parser
import json
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
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
    caption: str | None = None


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
    details: Mapping[str, Any] = attrs.field(factory=dict)


@attrs.frozen
class ActivityPage:
    """One page of activities and its continuation cursor."""

    activities: tuple[Activity, ...]
    next_cursor: str | None = None


@attrs.frozen
class DocumentReference:
    """A standalone document attached to a Kindertales record."""

    id: str
    url: str
    filename: str
    content_type: str | None = None
    description: str | None = None


@attrs.frozen
class Record:
    """A read-only snapshot of a non-media Kindertales record area."""

    id: str
    category: str
    source_url: str
    observed_at: dt.datetime
    details: Mapping[str, Any]
    child_id: str | None = None
    title: str | None = None
    documents: tuple[DocumentReference, ...] = ()


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
                    caption=str(medium["caption"])
                    if medium.get("caption") is not None
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
                details=_details(item.get("details", {})),
            )
        )
    cursor = payload.get("next_cursor")
    return ActivityPage(
        tuple(activities),
        str(cursor) if cursor is not None else None,
    )


def _details(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        msg = "activity details must be an object"
        raise DiscoveryError(msg)
    try:
        json.dumps(value)
    except (TypeError, ValueError) as error:
        msg = "activity details must contain JSON values"
        raise DiscoveryError(msg) from error
    return value


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


_SECRET_FIELD_PARTS = frozenset(
    {"authorization", "csrf", "password", "secret", "session", "token"}
)


class _RecordHTMLParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.fields: dict[str, str | bool] = {}
        self._main_text: list[str] = []
        self._main_fields: dict[str, str | bool] = {}
        self.title: list[str] = []
        self._ignored_depth = 0
        self._title_depth = 0
        self._depth = 0
        self._main_depth = 0
        self._saw_main = False

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        classes = _classes(attributes)
        ignored_region = self._is_ignored_region(tag, attributes, classes)
        if tag == "main" and "main-content" in classes:
            self._main_depth = self._depth + 1
            self._saw_main = True
        if tag not in {"input", "img", "br", "hr", "meta", "link"}:
            self._depth += 1
        if ignored_region and not self._ignored_depth:
            self._ignored_depth = self._depth
        if self._ignored_depth:
            return
        if tag in {"title", "h1"} and not self.title:
            self._title_depth += 1
        if tag not in {"input", "select", "textarea"}:
            return
        field = self._field(attributes)
        if field is None:
            return
        name, value = field
        self.fields[name] = value
        if self._main_depth:
            self._main_fields[name] = value

    @staticmethod
    def _is_ignored_region(
        tag: str,
        attributes: Mapping[str, str | None],
        classes: frozenset[str],
    ) -> bool:
        return (
            tag in {"script", "style", "svg"}
            or any(name.casefold().startswith("subnav") for name in classes)
            or (attributes.get("id") or "").casefold().startswith("subnav")
        )

    @staticmethod
    def _field(
        attributes: Mapping[str, str | None],
    ) -> tuple[str, str | bool] | None:
        name = attributes.get("name")
        field_type = (attributes.get("type") or "").casefold()
        if (
            not name
            or field_type in {"hidden", "password", "submit"}
            or any(part in name.casefold() for part in _SECRET_FIELD_PARTS)
        ):
            return None
        if field_type in {"checkbox", "radio"}:
            return name, "checked" in attributes
        value = attributes.get("value")
        return (name, value) if value is not None else None

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        self.text.append(value)
        if self._main_depth:
            self._main_text.append(value)
        if self._title_depth:
            self.title.append(value)

    def handle_endtag(self, tag: str) -> None:
        if self._depth:
            self._depth -= 1
        if self._main_depth and self._depth < self._main_depth:
            self._main_depth = 0
        if self._ignored_depth and self._depth < self._ignored_depth:
            self._ignored_depth = 0
        if tag in {"title", "h1"} and self._title_depth:
            self._title_depth -= 1

    @property
    def record_text(self) -> tuple[str, ...]:
        """Return meaningful main content, falling back for partial fixtures."""
        return tuple(self._main_text if self._saw_main else self.text)

    @property
    def record_fields(self) -> Mapping[str, str | bool]:
        """Return fields within the meaningful main content."""
        return self._main_fields if self._saw_main else self.fields


def parse_record_page(
    document: str,
    *,
    category: str,
    source_url: str,
    observed_at: dt.datetime,
    child_id: str | None = None,
) -> Record:
    """Convert one server-rendered account page into a structured snapshot."""
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        msg = "record observed_at must include a timezone offset"
        raise DiscoveryError(msg)
    parser = _RecordHTMLParser()
    parser.feed(document)
    title = " ".join(parser.title) or None
    return Record(
        _stable_id(category, child_id or "", source_url),
        category,
        source_url,
        observed_at,
        {"text": parser.record_text, "fields": parser.record_fields},
        child_id,
        title,
    )


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


def _query_id(href: str, name: str) -> str | None:
    values = parse_qs(urlsplit(href).query).get(name)
    return values[0] if values and values[0] else None


def _child_id(href: str) -> str | None:
    return _query_id(href, "cid")


def parse_legacy_children(document: str) -> tuple[Child, ...]:
    """Extract linked children from the authenticated dashboard navigation."""
    parser = _LegacyDashboardParser()
    parser.feed(document)
    names: dict[str, str] = {}
    centers: dict[str, str] = {}
    linked_ids: list[str] = []
    for anchor in parser.anchors:
        child_id = _child_id(anchor.href)
        if child_id is None:
            continue
        if center_id := _query_id(anchor.href, "clid"):
            centers.setdefault(child_id, center_id)
        name = " ".join("".join(anchor.child_name).split())
        if name:
            names.setdefault(child_id, name)
        text = " ".join("".join(anchor.text).split()).casefold()
        if "daily activity" in text and child_id not in linked_ids:
            linked_ids.append(child_id)
    child_ids = list(names)
    child_ids.extend(child_id for child_id in linked_ids if child_id not in names)
    return tuple(
        Child(
            child_id,
            names.get(child_id, f"Child {index}"),
            centers.get(child_id),
        )
        for index, child_id in enumerate(child_ids, start=1)
    )


@attrs.define
class _LegacyBox:
    identifier: str
    title: list[str] = attrs.field(factory=list)
    media: list[_Anchor] = attrs.field(factory=list)
    text: list[str] = attrs.field(factory=list)
    fields: dict[str, str] = attrs.field(factory=dict)


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
        if self._box is not None and tag in {"input", "select", "textarea"}:
            name = attributes.get("name")
            field_type = (attributes.get("type") or "").casefold()
            value = attributes.get("value")
            if name and field_type not in {"hidden", "password"} and value:
                self._box.fields[name] = value

    def handle_data(self, data: str) -> None:
        if self._box is not None:
            if self._title_depth:
                self._box.title.append(data)
            normalized = " ".join(data.split())
            if normalized:
                self._box.text.append(normalized)

    def handle_endtag(self, tag: str) -> None:
        if self._box is None or tag != "div":
            return
        if self._title_depth == self._box_depth:
            self._title_depth = 0
        self._box_depth -= 1
        if self._box_depth == 0:
            self.boxes.append(self._box)
            self._box = None


_CLOCK = re.compile(r"(?i)\b(\d{1,2}):(\d{2})\s*(am|pm)\b")
_CHILD_ID_QUERY = re.compile(
    r"(?:[?&]|&amp;)cid=([^&'\"\s)]+)",
    re.IGNORECASE,
)
_ATTENDANCE_KIND = re.compile(
    r"(?i)\bcheck(?:ed)?[-\s]+(?P<direction>in|out)\b"
)


@attrs.frozen
class FeedContext:
    """Precise activity and publication context from one news-feed row."""

    occurred_at: dt.datetime
    published_at: dt.datetime | None
    text: tuple[str, ...]


@attrs.define
class _FeedRow:
    media_ids: list[str] = attrs.field(factory=list)
    child_ids: list[str] = attrs.field(factory=list)
    text: list[str] = attrs.field(factory=list)
    span_text: list[str] = attrs.field(factory=list)


class _NotificationFeedParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[_FeedRow] = []
        self._row: _FeedRow | None = None
        self._row_depth = 0
        self._span_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        if (
            self._row is None
            and tag == "tr"
            and "box-shadow-default" in _classes(attributes)
        ):
            self._row = _FeedRow()
            self._row_depth = 1
            return
        if self._row is None:
            return
        for value in attributes.values():
            if value is not None:
                self._row.child_ids.extend(_CHILD_ID_QUERY.findall(value))
        if tag == "tr":
            self._row_depth += 1
        if tag == "span":
            self._span_depth += 1
        if tag == "a" and (href := attributes.get("href")):
            split_url = urlsplit(href)
            source_path = unquote(split_url.path)
            if "image_video" in _classes(attributes):
                self._row.media_ids.append(
                    _stable_id(split_url.netloc, source_path)
                )

    def handle_data(self, data: str) -> None:
        if self._row is None:
            return
        value = " ".join(data.split())
        if value:
            self._row.text.append(value)
            if self._span_depth:
                self._row.span_text.append(value)

    def handle_endtag(self, tag: str) -> None:
        if self._row is None:
            return
        if tag == "span" and self._span_depth:
            self._span_depth -= 1
        if tag != "tr":
            return
        self._row_depth -= 1
        if self._row_depth == 0:
            self.rows.append(self._row)
            self._row = None
            self._span_depth = 0


def _clock(value: str) -> dt.time | None:
    match = _CLOCK.search(value)
    if match is None:
        return None
    hour = int(match.group(1)) % 12
    if match.group(3).casefold() == "pm":
        hour += 12
    return dt.time(hour, int(match.group(2)))


def parse_notification_context(
    document: str,
    activity_date: dt.date,
    timezone: dt.tzinfo,
) -> Mapping[str, FeedContext]:
    """Map media IDs to precise activity context from the family news feed."""
    parser = _NotificationFeedParser()
    parser.feed(document)
    date_label = f"{activity_date:%B} {activity_date.day}"
    contexts: dict[str, FeedContext] = {}
    for row in parser.rows:
        if not row.media_ids or not any(date_label in value for value in row.text):
            continue
        occurred_time = next(
            (
                parsed
                for value in row.span_text
                if (parsed := _clock(value)) is not None
            ),
            None,
        )
        if occurred_time is None:
            continue
        all_times = tuple(
            parsed for value in row.text if (parsed := _clock(value)) is not None
        )
        published_time = next(
            (value for value in reversed(all_times) if value != occurred_time),
            None,
        )
        context = FeedContext(
            dt.datetime.combine(activity_date, occurred_time, timezone),
            dt.datetime.combine(activity_date, published_time, timezone)
            if published_time is not None
            else None,
            tuple(row.text),
        )
        contexts.update(dict.fromkeys(row.media_ids, context))
    return contexts


def parse_notification_activities(
    document: str,
    child_id: str,
    activity_date: dt.date,
    timezone: dt.tzinfo,
) -> tuple[Activity, ...]:
    """Extract non-media news-feed events such as check-in and check-out."""
    parser = _NotificationFeedParser()
    parser.feed(document)
    date_label = f"{activity_date:%B} {activity_date.day}"
    activities = []
    for row in parser.rows:
        if (
            row.media_ids
            or child_id not in row.child_ids
            or not any(date_label in value for value in row.text)
        ):
            continue
        occurred_time = next(
            (
                parsed
                for value in row.span_text
                if (parsed := _clock(value)) is not None
            ),
            None,
        )
        if occurred_time is None:
            occurred_time = next(
                (
                    parsed
                    for value in row.text
                    if (parsed := _clock(value)) is not None
                ),
                None,
            )
        if occurred_time is None:
            continue
        attendance_match = next(
            (
                match
                for value in row.text
                if (match := _ATTENDANCE_KIND.search(value)) is not None
            ),
            None,
        )
        if attendance_match is None:
            continue
        kind = f"Checked {attendance_match.group('direction').title()}"
        activities.append(
            Activity(
                _stable_id(child_id, activity_date.isoformat(), *row.text),
                child_id,
                kind,
                dt.datetime.combine(activity_date, occurred_time, timezone),
                (),
                details={"notification": {"text": tuple(row.text)}},
            )
        )
    return tuple(activities)


@attrs.frozen
class RecordWindow:
    """Bounded observation context for a generated record snapshot."""

    from_date: dt.date
    through_date: dt.date
    source_url: str
    observed_at: dt.datetime


def parse_attendance_record(
    document: str,
    child_id: str,
    timezone: dt.tzinfo,
    window: RecordWindow,
) -> Record:
    """Build a bounded attendance snapshot from child-linked news-feed rows."""
    events: list[dict[str, Any]] = []
    current = window.from_date
    while current <= window.through_date:
        events.extend(
            {
                "type": activity.kind,
                "occurred_at": activity.occurred_at.isoformat(),
                "details": dict(activity.details),
            }
            for activity in parse_notification_activities(
                document,
                child_id,
                current,
                timezone,
            )
        )
        current += dt.timedelta(days=1)
    return Record(
        _stable_id("attendance", child_id, window.source_url),
        "attendance",
        window.source_url,
        window.observed_at,
        {
            "from_date": window.from_date.isoformat(),
            "through_date": window.through_date.isoformat(),
            "events": tuple(events),
        },
        child_id,
        "Attendance",
    )


def _clean_label(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(re.sub(r"<[^>]*>", " ", value).split())
    return cleaned or None


def _meaningful(value: object) -> bool:
    return value not in (None, "", (), [], {})


def _form_entries(raw_form: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw_form, str):
        return ()
    try:
        items = json.loads(raw_form)
    except json.JSONDecodeError:
        return ()
    if not isinstance(items, list):
        return ()
    entries = []
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        selected = tuple(
            option.get("value") or option.get("label")
            for option in item.get("values", ())
            if isinstance(option, dict) and option.get("selected")
        ) if isinstance(item.get("values", ()), list) else ()
        if not _meaningful(value) and selected:
            value = selected
        if not _meaningful(value):
            continue
        entry: dict[str, Any] = {"value": value}
        if label := _clean_label(item.get("label")):
            entry["label"] = label
        if isinstance(item.get("name"), str) and item["name"]:
            entry["name"] = item["name"]
        entries.append(entry)
    return tuple(entries)


@attrs.define
class _EnrollmentBaseParser(html.parser.HTMLParser):
    pickups: list[dict[str, str]] = attrs.field(factory=list, init=False)
    _stack: list[tuple[str, frozenset[str]]] = attrs.field(factory=list, init=False)
    _pickup_candidate: bool = attrs.field(default=False, init=False)
    _pickup_active: bool = attrs.field(default=False, init=False)
    _current_label: str | None = attrs.field(default=None, init=False)
    _current_pickup: dict[str, str] = attrs.field(factory=dict, init=False)

    def __attrs_post_init__(self) -> None:
        html.parser.HTMLParser.__init__(self)

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        classes = _classes(dict(attrs_list))
        if tag == "div" and "title2" in classes:
            self._finish_pickup()
            self._pickup_active = False
            self._pickup_candidate = "pickup" in classes
        if tag not in {"br", "hr", "img", "input", "link", "meta"}:
            self._stack.append((tag, classes))

    def handle_startendtag(
        self,
        _tag: str,
        _attrs_list: list[tuple[str, str | None]],
    ) -> None:
        """Ignore self-closing elements without changing the class stack."""

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._pickup_candidate and value.casefold() == "authorized pickups details":
            self._pickup_active = True
            self._pickup_candidate = False
            return
        if not self._pickup_active:
            return
        active_classes = frozenset().union(
            *(classes for _tag, classes in self._stack)
        )
        if "title3" in active_classes:
            self._current_label = value.rstrip(":").casefold().replace(" ", "_")
        elif "text1" in active_classes and self._current_label is not None:
            label = self._current_label
            if label == "name" and self._current_pickup:
                self._finish_pickup()
                self._current_label = label
            self._current_pickup[label] = value

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index][0] == tag:
                del self._stack[index:]
                return

    def close(self) -> None:
        super().close()
        self._finish_pickup()

    def _finish_pickup(self) -> None:
        if self._current_pickup:
            self.pickups.append(self._current_pickup)
        self._current_pickup = {}
        self._current_label = None


def parse_authorized_pickups(document: str) -> tuple[Mapping[str, str], ...]:
    """Extract populated authorized-pickup rows from the base enrollment form."""
    parser = _EnrollmentBaseParser()
    parser.feed(document)
    parser.close()
    return tuple(parser.pickups)


def parse_enrollment_forms(
    payload: Mapping[str, Any],
    child_id: str,
    *,
    source_url: str,
    observed_at: dt.datetime,
) -> Record:
    """Extract only completed values from Kindertales enrollment forms."""
    forms = []
    raw_defaults = payload.get("defaultForms", {})
    default_forms: tuple[tuple[object, object], ...] = (
        tuple(raw_defaults.items()) if isinstance(raw_defaults, dict) else ()
    )
    authorized_pickups: tuple[Mapping[str, str], ...] = ()
    for form_id, form in default_forms:
        if not isinstance(form, dict):
            continue
        if isinstance(form.get("html"), str):
            authorized_pickups = parse_authorized_pickups(form["html"])
        entries = _form_entries(form.get("form"))
        if entries:
            forms.append({"kind": "default", "id": str(form_id), "entries": entries})
    raw_custom = payload.get("customForms", ())
    custom_forms: tuple[tuple[int, object], ...] = (
        tuple(enumerate(raw_custom)) if isinstance(raw_custom, list) else ()
    )
    for form_id, form in custom_forms:
        if not isinstance(form, dict):
            continue
        entries = _form_entries(form.get("form"))
        if entries:
            forms.append({"kind": "custom", "id": str(form_id), "entries": entries})
    return Record(
        _stable_id("enrollment", child_id, source_url),
        "enrollment",
        source_url,
        observed_at,
        {
            "authorized_pickups": authorized_pickups,
            "forms": tuple(forms),
        },
        child_id,
        "Enrollment",
    )


_DOCUMENT_SUFFIXES = frozenset(
    {
        ".csv",
        ".doc",
        ".docx",
        ".gif",
        ".heic",
        ".jpeg",
        ".jpg",
        ".ods",
        ".odt",
        ".pdf",
        ".png",
        ".rtf",
        ".tif",
        ".tiff",
        ".txt",
        ".xls",
        ".xlsx",
    }
)


def _document_reference(
    href: str,
    *,
    base_url: str,
    description: str | None = None,
) -> DocumentReference | None:
    absolute = urljoin(base_url, href)
    split_url = urlsplit(absolute)
    filename = PurePosixPath(unquote(split_url.path)).name
    suffix = PurePosixPath(filename).suffix.casefold()
    query = parse_qs(split_url.query)
    if suffix not in _DOCUMENT_SUFFIXES and "X-Amz-Signature" not in query:
        return None
    content_types = query.get("response-content-type", ())
    return DocumentReference(
        _stable_id("document", split_url.netloc, split_url.path),
        absolute,
        filename or "document.bin",
        content_types[0] if content_types else None,
        description,
    )


@attrs.define
class _ProfileDocumentParser(html.parser.HTMLParser):
    base_url: str
    documents: list[DocumentReference] = attrs.field(factory=list, init=False)
    _table_depth: int = attrs.field(default=0, init=False)
    _row_depth: int = attrs.field(default=0, init=False)
    _row_text: list[str] = attrs.field(factory=list, init=False)

    def __attrs_post_init__(self) -> None:
        html.parser.HTMLParser.__init__(self)

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        if tag == "table" and attributes.get("id") == "attachments_table":
            self._table_depth = 1
            return
        if not self._table_depth:
            return
        if tag == "table":
            self._table_depth += 1
        if tag == "tr":
            self._row_depth += 1
            if self._row_depth == 1:
                self._row_text = []
        if tag == "a" and (href := attributes.get("href")):
            document = _document_reference(
                href,
                base_url=self.base_url,
                description=" | ".join(self._row_text) or None,
            )
            if document is not None:
                self.documents.append(document)

    def handle_data(self, data: str) -> None:
        if self._row_depth and (value := " ".join(data.split())):
            self._row_text.append(value)

    def handle_endtag(self, tag: str) -> None:
        if not self._table_depth:
            return
        if tag == "tr" and self._row_depth:
            self._row_depth -= 1
        if tag == "table":
            self._table_depth -= 1


def parse_profile_documents(
    document: str,
    *,
    child_id: str,
    source_url: str,
    observed_at: dt.datetime,
) -> Record:
    """Extract attached child documents from the profile attachment table."""
    parser = _ProfileDocumentParser(source_url)
    parser.feed(document)
    documents = tuple(dict.fromkeys(parser.documents))
    return Record(
        _stable_id("profile_documents", child_id, source_url),
        "profile_documents",
        source_url,
        observed_at,
        {
            "documents": tuple(
                {
                    "id": item.id,
                    "filename": item.filename,
                    "content_type": item.content_type,
                    "description": item.description,
                }
                for item in documents
            )
        },
        child_id,
        "Profile Documents",
        documents,
    )


@attrs.frozen
class MessageLink:
    """One message-detail link and its listing state."""

    id: str
    href: str
    unread: bool


@attrs.define
class _MessageListingParser(html.parser.HTMLParser):
    links: list[MessageLink] = attrs.field(factory=list, init=False)
    pages: list[str] = attrs.field(factory=list, init=False)
    _unread_depth: int = attrs.field(default=0, init=False)
    _depth: int = attrs.field(default=0, init=False)

    def __attrs_post_init__(self) -> None:
        html.parser.HTMLParser.__init__(self)

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        self._depth += 1
        if tag == "li" and "unread" in _classes(attributes):
            self._unread_depth = self._depth
        if tag != "a" or not (href := attributes.get("href")):
            return
        query = parse_qs(urlsplit(href).query)
        if message_ids := query.get("msgid"):
            self.links.append(
                MessageLink(message_ids[0], href, bool(self._unread_depth))
            )
        elif query.get("page") and query.get("pg") == ["messagecentre"]:
            self.pages.append(href)

    def handle_endtag(self, _tag: str) -> None:
        if self._unread_depth == self._depth:
            self._unread_depth = 0
        if self._depth:
            self._depth -= 1


def parse_message_listing(
    document: str,
) -> tuple[tuple[MessageLink, ...], tuple[str, ...]]:
    """Return message-detail and pagination links from one folder page."""
    parser = _MessageListingParser()
    parser.feed(document)
    links = tuple({link.id: link for link in parser.links}.values())
    return links, tuple(dict.fromkeys(parser.pages))


@attrs.define
class _MessageDetailParser(html.parser.HTMLParser):
    base_url: str
    subject: list[str] = attrs.field(factory=list, init=False)
    headers: list[str] = attrs.field(factory=list, init=False)
    body: list[str] = attrs.field(factory=list, init=False)
    documents: list[DocumentReference] = attrs.field(factory=list, init=False)
    _depth: int = attrs.field(default=0, init=False)
    _subject_depth: int = attrs.field(default=0, init=False)
    _headers_depth: int = attrs.field(default=0, init=False)
    _body_depth: int = attrs.field(default=0, init=False)
    _ignored_depth: int = attrs.field(default=0, init=False)

    def __attrs_post_init__(self) -> None:
        html.parser.HTMLParser.__init__(self)

    def handle_starttag(
        self,
        tag: str,
        attrs_list: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs_list)
        self._depth += 1
        if tag in {"script", "style"}:
            self._ignored_depth = self._depth
        classes = _classes(attributes)
        if "subject" in classes:
            self._subject_depth = self._depth
        if "printHead" in classes:
            self._headers_depth = self._depth
        if attributes.get("id") == "viewer-content":
            self._body_depth = self._depth
        if tag == "a" and (href := attributes.get("href")):
            document = _document_reference(href, base_url=self.base_url)
            if document is not None:
                self.documents.append(document)

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        value = " ".join(data.split())
        if not value:
            return
        if self._body_depth:
            self.body.append(value)
        elif self._subject_depth:
            self.subject.append(value)
        elif self._headers_depth:
            self.headers.append(value)

    def handle_endtag(self, _tag: str) -> None:
        if self._body_depth == self._depth:
            self._body_depth = 0
        if self._subject_depth == self._depth:
            self._subject_depth = 0
        if self._headers_depth == self._depth:
            self._headers_depth = 0
        if self._ignored_depth == self._depth:
            self._ignored_depth = 0
        if self._depth:
            self._depth -= 1


def parse_message_detail(
    document: str,
    link: MessageLink,
    *,
    source_url: str,
) -> tuple[Mapping[str, Any], tuple[DocumentReference, ...]]:
    """Extract one message body, headers, state, and attachments."""
    parser = _MessageDetailParser(source_url)
    parser.feed(document)
    documents = tuple(dict.fromkeys(parser.documents))
    return (
        {
            "id": link.id,
            "unread": link.unread,
            "subject": " ".join(parser.subject) or None,
            "headers": tuple(parser.headers),
            "body": "\n".join(parser.body),
            "documents": tuple(
                {"id": item.id, "filename": item.filename}
                for item in documents
            ),
        },
        documents,
    )


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
    feed_context: Mapping[str, FeedContext] | None = None,
) -> tuple[Activity, ...]:
    """Extract activity context and media from one legacy daily report."""
    parser = _LegacyActivityParser()
    parser.feed(document)
    if feed_context is None:
        feed_context = {}
    activities = []
    for index, box in enumerate(parser.boxes):
        kind = " ".join("".join(box.title).split()) or box.identifier
        base_activity_id = _stable_id(
            child_id,
            activity_date.isoformat(),
            box.identifier,
            str(index),
        )
        media = []
        for anchor in box.media:
            split_url = urlsplit(anchor.href)
            source_path = unquote(split_url.path)
            media.append(
                MediaReference(
                    _stable_id(split_url.netloc, source_path),
                    anchor.href,
                    _content_type(source_path),
                    PurePosixPath(source_path).name or None,
                    anchor.title.strip()
                    if anchor.title and anchor.title.strip()
                    else None,
                )
            )
        visible_text = tuple(
            text
            for text in box.text
            if text != kind and not text.casefold().startswith("javascript:")
        )
        grouped_media: dict[FeedContext | None, list[MediaReference]] = {}
        for medium in media:
            grouped_media.setdefault(feed_context.get(medium.id), []).append(medium)
        if not grouped_media:
            grouped_media[None] = []
        for group_index, (context, context_media) in enumerate(
            grouped_media.items()
        ):
            details: dict[str, Any] = {
                "text": visible_text,
                "fields": box.fields,
            }
            if context is not None:
                details["notification"] = {
                    "text": context.text,
                    "published_at": context.published_at.isoformat()
                    if context.published_at is not None
                    else None,
                }
            captions = {
                medium.caption
                for medium in context_media
                if medium.caption is not None
            }
            activity_id = (
                base_activity_id
                if group_index == 0
                else _stable_id(
                    base_activity_id,
                    context.occurred_at.isoformat()
                    if context is not None
                    else "unmatched",
                )
            )
            activities.append(
                Activity(
                    activity_id,
                    child_id,
                    kind,
                    context.occurred_at
                    if context is not None
                    else dt.datetime.combine(activity_date, dt.time(), timezone),
                    tuple(context_media),
                    next(iter(captions)) if len(captions) == 1 else None,
                    details=details,
                )
            )
    return tuple(activities)


@attrs.frozen
class LegacyKindertalesAdapter:
    """Read-only adapter for Kindertales' authenticated legacy HTML pages."""

    client: httpx.AsyncClient
    requester: scheduler.Requester | None = None
    timezone: dt.tzinfo = attrs.field(factory=lambda: ZoneInfo("America/New_York"))
    _notification_documents: list[str] = attrs.field(factory=list, init=False)

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

    async def post(
        self,
        path: str,
        *,
        data: Mapping[str, str],
    ) -> httpx.Response:
        """Send a read-only discovery POST through the request boundary."""

        async def send() -> httpx.Response:
            return await self.client.post(path, data=data)

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
        page_complete: Callable[[], None] | None = None,
    ) -> AsyncIterator[Activity]:
        """Yield daily-report activities over an inclusive bounded range."""
        del cursor
        if from_date is None:
            msg = "legacy Kindertales discovery requires --from"
            raise DiscoveryError(msg)
        if through_date is None:
            msg = "legacy Kindertales discovery requires an upper date bound"
            raise DiscoveryError(msg)
        notification_document = await self._notification_document()
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
            if page_complete is not None:
                page_complete()
            for activity in parse_legacy_activities(
                response.text,
                child_id,
                current,
                self.timezone,
                parse_notification_context(
                    notification_document,
                    current,
                    self.timezone,
                ),
            ):
                yield activity
            for activity in parse_notification_activities(
                notification_document,
                child_id,
                current,
                self.timezone,
            ):
                yield activity
            current += dt.timedelta(days=1)

    @staticmethod
    def activity_page_count(
        *,
        from_date: dt.date | None,
        through_date: dt.date | None,
    ) -> int:
        """Return the number of daily-report requests for one child."""
        if from_date is None or through_date is None:
            return 0
        return max(0, (through_date - from_date).days + 1)

    async def _notification_document(self) -> str:
        if not self._notification_documents:
            response = await self.get(
                "/modules/notificationsV2/notificationsv2.php",
                params={"limit": "200", "offset": "0"},
            )
            response.raise_for_status()
            self._notification_documents.append(response.text)
        return self._notification_documents[0]

    async def child_records(
        self,
        child_id: str,
        *,
        from_date: dt.date | None = None,
        through_date: dt.date | None = None,
        request_complete: Callable[[], None] | None = None,
    ) -> tuple[Record, ...]:
        """Snapshot the read-only report and profile areas for one child."""
        if from_date is None:
            msg = "legacy attendance discovery requires --from"
            raise DiscoveryError(msg)
        if through_date is None:
            msg = "legacy attendance discovery requires an upper date bound"
            raise DiscoveryError(msg)
        observed_at = dt.datetime.now(dt.UTC)
        notification_document = await self._notification_document()
        notification_url = str(
            self.client.base_url.join(
                "/modules/notificationsV2/notificationsv2.php?limit=200&offset=0"
            )
        )
        records = [
            parse_attendance_record(
                notification_document,
                child_id,
                self.timezone,
                RecordWindow(
                    from_date,
                    through_date,
                    notification_url,
                    observed_at,
                ),
            )
        ]
        enrollment_response = await self.post(
            "/db/allActiveForms.php",
            data={"cid": child_id},
        )
        if request_complete is not None:
            request_complete()
        enrollment_response.raise_for_status()
        try:
            enrollment_payload = enrollment_response.json()
        except json.JSONDecodeError as error:
            msg = "Kindertales enrollment endpoint returned invalid JSON"
            raise DiscoveryError(msg) from error
        if not isinstance(enrollment_payload, dict):
            msg = "Kindertales enrollment endpoint must return an object"
            raise DiscoveryError(msg)
        records.append(
            parse_enrollment_forms(
                enrollment_payload,
                child_id,
                source_url=str(enrollment_response.url),
                observed_at=observed_at,
            )
        )
        routes = (
            ("baby_bulletin", "babybulletin", "child"),
            ("immunizations", "immunization", None),
            ("medications", "medications", "main"),
            ("milestones", "milestonereport", None),
            ("profile_documents", "profiledetails", None),
        )
        for category, page, subpage in routes:
            params = {"pg": page, "cid": child_id}
            if subpage is not None:
                params["subpg"] = subpage
            response = await self.get("/index.php", params=params)
            if request_complete is not None:
                request_complete()
            response.raise_for_status()
            if category == "profile_documents":
                records.append(
                    parse_profile_documents(
                        response.text,
                        child_id=child_id,
                        source_url=str(response.url),
                        observed_at=observed_at,
                    )
                )
            else:
                records.append(
                    parse_record_page(
                        response.text,
                        category=category,
                        source_url=str(response.url),
                        observed_at=observed_at,
                        child_id=child_id,
                    )
                )
        return tuple(records)

    async def account_records(
        self,
        *,
        messages: bool,
        billing: bool,
        request_complete: Callable[[], None] | None = None,
        requests_discovered: Callable[[int], None] | None = None,
    ) -> tuple[Record, ...]:
        """Snapshot enabled account-level communications and financial pages."""
        records = []
        if messages:
            for subpage in ("inbox", "sent", "draft", "scheduled"):
                record = await self._message_folder(
                    subpage,
                    request_complete=request_complete,
                    requests_discovered=requests_discovered,
                )
                if record is not None:
                    records.append(record)
            response = await self.get(
                "/index.php",
                params={"pg": "messagecentre", "subpg": "contacts"},
            )
            if request_complete is not None:
                request_complete()
            if not _dashboard_redirect(response):
                response.raise_for_status()
                records.append(
                    parse_record_page(
                        response.text,
                        category="messages_contacts",
                        source_url=str(response.url),
                        observed_at=dt.datetime.now(dt.UTC),
                    )
                )
        if billing:
            response = await self.get("/index.php", params={"pg": "pbilling"})
            if request_complete is not None:
                request_complete()
            if _dashboard_redirect(response):
                return tuple(records)
            response.raise_for_status()
            records.append(
                parse_record_page(
                    response.text,
                    category="billing",
                    source_url=str(response.url),
                    observed_at=dt.datetime.now(dt.UTC),
                )
            )
        return tuple(records)

    async def _message_folder(
        self,
        subpage: str,
        *,
        request_complete: Callable[[], None] | None,
        requests_discovered: Callable[[int], None] | None,
    ) -> Record | None:
        listing_url = f"/index.php?pg=messagecentre&subpg={subpage}"
        pending = [listing_url]
        known_pages = {listing_url}
        links: dict[str, MessageLink] = {}
        source_url = str(self.client.base_url.join(listing_url))
        while pending:
            page_url = pending.pop(0)
            response = await self.get(page_url)
            if request_complete is not None:
                request_complete()
            if _dashboard_redirect(response):
                return None
            response.raise_for_status()
            source_url = str(response.url)
            page_links, page_urls = parse_message_listing(response.text)
            links.update({link.id: link for link in page_links})
            new_pages = tuple(
                _legacy_request_target(href)
                for href in page_urls
                if parse_qs(urlsplit(href).query).get("subpg") == [subpage]
                and _legacy_request_target(href) not in known_pages
            )
            known_pages.update(new_pages)
            pending.extend(new_pages)
            if requests_discovered is not None:
                requests_discovered(len(new_pages))
        messages = []
        documents: dict[str, DocumentReference] = {}
        if requests_discovered is not None:
            requests_discovered(len(links))
        for link in links.values():
            response = await self.get(_legacy_request_target(link.href))
            if request_complete is not None:
                request_complete()
            response.raise_for_status()
            message, attached = parse_message_detail(
                response.text,
                link,
                source_url=str(response.url),
            )
            messages.append(message)
            documents.update({item.id: item for item in attached})
        observed_at = dt.datetime.now(dt.UTC)
        return Record(
            _stable_id(f"messages_{subpage}", source_url),
            f"messages_{subpage}",
            source_url,
            observed_at,
            {"messages": tuple(messages)},
            title=f"Messages: {subpage.title()}",
            documents=tuple(documents.values()),
        )


def _dashboard_redirect(response: httpx.Response) -> bool:
    if not response.is_redirect:
        return False
    location = response.headers.get("Location", "")
    return parse_qs(urlsplit(location).query).get("pg") == ["dashboard"]


def _legacy_request_target(href: str) -> str:
    """Return an authenticated relative target for a same-site legacy link."""
    split_url = urlsplit(href)
    path = split_url.path or "/index.php"
    if not path.startswith("/"):
        path = f"/{path}"
    return f"{path}?{split_url.query}" if split_url.query else path


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
        page_complete: Callable[[], None] | None = None,
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
            if page_complete is not None:
                page_complete()
            for activity in page.activities:
                yield activity
            if page.next_cursor is None:
                return
            if page.next_cursor in seen:
                msg = "activity pagination repeated a cursor"
                raise DiscoveryError(msg)
            seen.add(page.next_cursor)
            next_cursor = page.next_cursor
