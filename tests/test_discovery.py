"""Tests for child, activity, and media discovery."""

import datetime as dt
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from kindertales_scraper import config, discovery, scheduler


def fixture(name: str) -> dict[str, Any]:
    """Load a sanitized JSON fixture."""
    path = Path(__file__).parent / "fixtures" / name
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_parse_multiple_children() -> None:
    """Child parsing preserves stable identifiers and center context."""
    assert discovery.parse_children(fixture("children.json")) == (
        discovery.Child("child-1", "Alex", "center-1"),
        discovery.Child("child-2", "Sam"),
    )


def test_parse_activity_media_context() -> None:
    """Activity parsing retains media and all relevant context."""
    page = discovery.parse_activity_page(fixture("activities-page-1.json"))
    assert page.next_cursor == "cursor-2"
    assert page.activities == (
        discovery.Activity(
            id="activity-1",
            child_id="child-1",
            kind="Art",
            occurred_at=dt.datetime(
                2026, 7, 1, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=-4))
            ),
            media=(
                discovery.MediaReference(
                    "media-1",
                    "https://media.example.test/photo.jpg?signature=synthetic",
                    "image/jpeg",
                    "photo.jpg",
                ),
            ),
            caption="Finger painting",
            author="Teacher",
            center_id="center-1",
        ),
    )


def test_empty_history() -> None:
    """Empty child and activity histories are valid."""
    assert discovery.parse_children({"children": []}) == ()
    assert discovery.parse_activity_page({"activities": []}) == discovery.ActivityPage(
        ()
    )


@pytest.mark.parametrize("details", [[], {"bad": object()}])
def test_invalid_activity_details(details: object) -> None:
    """Structured activity details must be a JSON object."""
    payload = {
        "activities": [
            {
                "id": "activity",
                "child_id": "child",
                "type": "Care",
                "occurred_at": "2026-07-01T09:00:00-04:00",
                "details": details,
            }
        ]
    }
    with pytest.raises(discovery.DiscoveryError, match="details"):
        discovery.parse_activity_page(payload)


@pytest.mark.parametrize(
    ("function", "payload", "message"),
    [
        (discovery.parse_children, {}, "children must be an array"),
        (discovery.parse_children, {"children": [1]}, "each child"),
        (
            discovery.parse_children,
            {"children": [{"id": "", "name": "x"}]},
            "non-empty",
        ),
        (discovery.parse_activity_page, {}, "activities must be an array"),
        (discovery.parse_activity_page, {"activities": [1]}, "each activity"),
        (
            discovery.parse_activity_page,
            {"activities": [{"media": "x"}]},
            "media must be",
        ),
        (
            discovery.parse_activity_page,
            {"activities": [{"media": [1]}]},
            "media reference",
        ),
        (
            discovery.parse_activity_page,
            {
                "activities": [
                    {"id": "a", "child_id": "c", "type": "x", "occurred_at": "bad"}
                ]
            },
            "ISO 8601",
        ),
        (
            discovery.parse_activity_page,
            {
                "activities": [
                    {
                        "id": "a",
                        "child_id": "c",
                        "type": "x",
                        "occurred_at": "2026-01-01",
                    }
                ]
            },
            "timezone",
        ),
    ],
)
def test_invalid_payload(
    function: Callable[[Mapping[str, Any]], object],
    payload: dict[str, Any],
    message: str,
) -> None:
    """Malformed discovery payloads fail at the adapter boundary."""
    with pytest.raises(discovery.DiscoveryError, match=message):
        function(payload)


def test_dom_fallback() -> None:
    """DOM fallback reads only explicit child data attributes."""
    document = """
    <div data-child-id="child-1" data-child-name="Alex" data-center-id="center-1"></div>
    <div data-child-id="incomplete"></div>
    """
    assert discovery.parse_children_html(document) == (
        discovery.Child("child-1", "Alex", "center-1"),
    )


def test_parse_structured_record_page() -> None:
    """Record snapshots retain visible values while excluding executable secrets."""
    document = """
    <title>Child Profile</title><h1>Ignored second title</h1>
    <script><span>script text</span></script>
    <style>style text</style><svg><path>svg text</path></svg>
    <p>  Medical notes  </p>
    <input name="note" value="synthetic">
    <input name="consent" type="checkbox" checked>
    <input name="declined" type="radio">
    <input name="csrf_token" value="secret">
    <input name="password" type="password" value="secret">
    <input name="save" type="submit" value="Save">
    <input value="unnamed"><textarea name="empty"></textarea>
    """
    observed = dt.datetime(2026, 7, 18, 12, tzinfo=dt.UTC)
    record = discovery.parse_record_page(
        document,
        category="profile",
        source_url="https://example.test/?pg=profile&token=secret",
        observed_at=observed,
        child_id="child-1",
    )
    assert record.title == "Child Profile"
    assert record.details["text"] == (
        "Child Profile",
        "Ignored second title",
        "Medical notes",
    )
    assert record.details["fields"] == {
        "note": "synthetic",
        "consent": True,
        "declined": False,
    }
    assert record.id == discovery.parse_record_page(
        document,
        category="profile",
        source_url=record.source_url,
        observed_at=observed + dt.timedelta(days=1),
        child_id="child-1",
    ).id


def test_record_page_requires_aware_observation_time() -> None:
    """Record provenance cannot silently use an ambiguous local timestamp."""
    with pytest.raises(discovery.DiscoveryError, match="timezone"):
        discovery.parse_record_page(
            "<p>No title</p>",
            category="profile",
            source_url="https://example.test/",
            observed_at=dt.datetime(2026, 7, 18, tzinfo=dt.UTC).replace(tzinfo=None),
        )


def test_record_page_prefers_main_content_and_excludes_subnavigation() -> None:
    """Full-page snapshots omit shared navigation and selector chrome."""
    record = discovery.parse_record_page(
        """
        <nav>Shared navigation</nav>
        <main class="main-content">
          <div class="subNav-content">Child selector</div>
          <section><h1>Attendance</h1><p>Checked in 9:00 AM</p>
          <input name="room" value="Preschool"></section>
        </main>
        """,
        category="attendance",
        source_url="https://example.test/attendance",
        observed_at=dt.datetime(2026, 7, 18, tzinfo=dt.UTC),
    )
    assert record.details == {
        "text": ("Attendance", "Checked in 9:00 AM"),
        "fields": {"room": "Preschool"},
    }
    assert discovery.parse_record_page(
        "</div><main>Still visible</main>",
        category="daily",
        source_url="https://example.test/",
        observed_at=dt.datetime(2026, 7, 18, tzinfo=dt.UTC),
    ).details["text"] == ("Still visible",)


@pytest.mark.asyncio
async def test_adapter_paginates() -> None:
    """The adapter traverses pages and passes continuation cursors."""
    requests: list[httpx.Request] = []
    first = fixture("activities-page-1.json")
    second = {"activities": [], "next_cursor": None}
    children_response = fixture("children.json")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json=children_response)
        requests.append(request)
        return httpx.Response(200, json=first if len(requests) == 1 else second)

    async with httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = discovery.KindertalesAdapter(client)
        assert await adapter.children() == discovery.parse_children(children_response)
        activities = [
            item
            async for item in adapter.activities(
                "child-1",
                from_date=dt.date(2026, 7, 1),
                through_date=dt.date(2026, 7, 2),
            )
        ]
    assert len(activities) == 1
    assert requests[1].url.params["cursor"] == "cursor-2"
    assert requests[1].url.params["from"] == "2026-07-01"
    assert requests[1].url.params["through"] == "2026-07-02"


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["children", "activities"])
async def test_adapter_rejects_non_object_response(route: str) -> None:
    """Top-level API arrays are rejected."""
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=[]))
    async with httpx.AsyncClient(
        base_url="https://example.test", transport=transport
    ) as client:
        adapter = discovery.KindertalesAdapter(client)

        async def invoke() -> object:
            if route == "children":
                return await adapter.children()
            return [item async for item in adapter.activities("child")]

        with pytest.raises(
            discovery.DiscoveryError, match="response must be an object"
        ):
            await invoke()


@pytest.mark.asyncio
async def test_repeated_pagination_cursor_is_rejected() -> None:
    """A broken API cannot cause infinite pagination."""
    payload = {"activities": [], "next_cursor": "same"}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(
        base_url="https://example.test", transport=transport
    ) as client:
        adapter = discovery.KindertalesAdapter(client)
        with pytest.raises(discovery.DiscoveryError, match="repeated"):
            _ = [item async for item in adapter.activities("child", cursor="same")]


@pytest.mark.asyncio
async def test_adapter_uses_request_policy() -> None:
    """Discovery requests pass through the shared quota/retry boundary."""

    class Limiter:
        calls = 0

        async def acquire(self) -> float:
            self.calls += 1
            return float(self.calls)

    limiter = Limiter()
    policy = config.RequestPolicy(
        quotas=(config.Quota(100, 1),),
        jitter_fraction=0,
        max_retries=0,
    )
    requester = scheduler.Requester(policy, limiter)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"children": []})
    )
    async with httpx.AsyncClient(
        base_url="https://example.test",
        transport=transport,
    ) as client:
        adapter = discovery.KindertalesAdapter(client, requester=requester)
        assert await adapter.children() == ()
    assert limiter.calls == 1


LEGACY_DASHBOARD = """
<a href="/index.php?pg=help">Help</a>
<ul class="children-menu">
  <li><a href="/index.php?pg=dashboard&amp;cid=child-1&amp;clid=center-1">
    <span class="childName">Alex</span>
  </a></li>
  <li><a href="/index.php?pg=dashboard&amp;cid=child-2">
    <span class="childName">Sam</span>
  </a></li>
</ul>
<a href="/index.php?pg=dailyreport&amp;cid=child-1&amp;subpg=activity">
  Daily Activity
</a>
"""

LEGACY_REPORT = """
<div class="contentBoxes" id="forfun">
  <div class="enrollmentTitle">For Fun</div>
  <div class="care-details">At 9:15 AM <input name="staff" value="Teacher"></div>
  <input type="hidden" name="csrf" value="secret">
  <div class="gallery">
    <a class="html5lightbox image_video"
       href="https://media.example.test/uploads/photo.JPG?signature=synthetic"
       title="Building blocks"><img src="thumbnail.jpg"></a>
    <a class="image_video html5lightbox"
       href="https://media.example.test/uploads/movie.mp4?signature=synthetic"
       title="Building blocks"><img src="thumbnail.jpg"></a>
  </div>
</div>
<div class="contentBoxes">
  <a class="html5lightbox image_video">Missing URL</a>
  <a class="html5lightbox image_video" href="https://media.example.test/"
     title="First caption"></a>
  <a class="html5lightbox image_video" href="https://media.example.test/file.bin"
     title="Second caption"></a>
  <a class="html5lightbox image_video" href="https://media.example.test/no-title.bin">
  </a>
</div>
"""

LEGACY_FEED = """
<table><tr class="box-shadow-default"><td class="resultdata">
  <span>9:15 AM</span><div>My Day</div>
  <a class="html5lightbox image_video"
     href="https://media.example.test/uploads/photo.JPG?signature=new"></a>
  <div>@ 1:37 pm</div><div>July 14</div>
</td></tr></table>
"""

LEGACY_ATTENDANCE_FEED = """
<table>
  <tr class="box-shadow-default"><td>
    <div>Tuesday</div><div>8:05 AM</div>
    <div onclick="location.href='/?pg=attendance&amp;cid=child-1'">Checked In</div>
    <input disabled>
    <a href="/?pg=unrelated">Details</a><div>July 14</div>
  </td></tr>
  <tr class="box-shadow-default"><td>
    <span>No clock</span><a href="/?cid=child-1">Ignored</a><div>July 14</div>
  </td></tr>
  <tr class="box-shadow-default"><td>
    <span>10:00 AM</span><a href="/?cid=child-1">Snack</a><div>July 14</div>
  </td></tr>
  <tr class="box-shadow-default"><td><tr><td>Nested</td></tr></td></tr>
</table>
"""


def test_parse_legacy_dashboard_children() -> None:
    """Legacy dashboard navigation provides every linked child and name."""
    assert discovery.parse_legacy_children(LEGACY_DASHBOARD) == (
        discovery.Child("child-1", "Alex", "center-1"),
        discovery.Child("child-2", "Sam"),
    )
    assert discovery.parse_legacy_children(
        '<a href="?cid=unknown">Daily Activity</a>'
    ) == (discovery.Child("unknown", "Child 1"),)
    assert discovery.parse_legacy_children(
        '<a href="?cid=child-1"><span class="childName">Alex</span></a>'
    ) == (discovery.Child("child-1", "Alex"),)


def test_parse_legacy_daily_report() -> None:
    """Legacy daily reports produce stable, typed activity media context."""
    activity_date = dt.date(2026, 7, 14)
    first = discovery.parse_legacy_activities(
        LEGACY_REPORT,
        "child-1",
        activity_date,
        ZoneInfo("America/New_York"),
    )
    second = discovery.parse_legacy_activities(
        LEGACY_REPORT.replace("signature=synthetic", "signature=reissued"),
        "child-1",
        activity_date,
        ZoneInfo("America/New_York"),
    )
    assert len(first) == 2
    assert first[0].id == second[0].id
    assert first[0].kind == "For Fun"
    assert first[0].occurred_at.isoformat() == "2026-07-14T00:00:00-04:00"
    assert first[0].caption == "Building blocks"
    assert first[0].details == {
        "fields": {"staff": "Teacher"},
        "text": ("At 9:15 AM",),
    }
    assert first[0].media[0].id == second[0].media[0].id
    assert first[0].media[0].content_type == "image/jpeg"
    assert first[0].media[0].filename == "photo.JPG"
    assert first[0].media[0].caption == "Building blocks"
    assert first[0].media[1].content_type == "video/mp4"
    assert first[1].kind == "activity"
    assert first[1].caption is None
    assert first[1].media[0].filename is None
    assert first[1].media[0].content_type is None
    assert discovery.parse_legacy_activities(
        "<html><body>No activities</body></html>",
        "child-1",
        activity_date,
        dt.UTC,
    ) == ()
    empty_activity = discovery.parse_legacy_activities(
        '<div class="contentBoxes" id="empty"></div>',
        "child-1",
        activity_date,
        dt.UTC,
    )
    assert len(empty_activity) == 1
    assert empty_activity[0].media == ()


def test_news_feed_supplies_precise_activity_and_publication_times() -> None:
    """Media correlation replaces midnight without conflating publication time."""
    activity_date = dt.date(2026, 7, 14)
    contexts = discovery.parse_notification_context(
        LEGACY_FEED,
        activity_date,
        ZoneInfo("America/New_York"),
    )
    activities = discovery.parse_legacy_activities(
        LEGACY_REPORT,
        "child-1",
        activity_date,
        ZoneInfo("America/New_York"),
        contexts,
    )
    assert activities[0].occurred_at.isoformat() == "2026-07-14T09:15:00-04:00"
    assert activities[0].details["notification"] == {
        "text": ("9:15 AM", "My Day", "@ 1:37 pm", "July 14"),
        "published_at": "2026-07-14T13:37:00-04:00",
    }
    assert activities[1].occurred_at.isoformat() == "2026-07-14T00:00:00-04:00"
    assert [medium.filename for medium in activities[0].media] == ["photo.JPG"]
    assert [medium.filename for medium in activities[1].media] == ["movie.mp4"]


def test_news_feed_supplies_non_media_attendance_events() -> None:
    """Child-linked feed rows retain precise non-media attendance context."""
    activities = discovery.parse_notification_activities(
        LEGACY_ATTENDANCE_FEED,
        "child-1",
        dt.date(2026, 7, 14),
        ZoneInfo("America/New_York"),
    )
    assert len(activities) == 1
    assert activities[0].kind == "Checked In"
    assert activities[0].occurred_at.isoformat() == "2026-07-14T08:05:00-04:00"
    assert activities[0].media == ()
    span_clock = discovery.parse_notification_activities(
        '<tr class="box-shadow-default"><td><span>9:10 AM</span>'
        '<a href="/?cid=child-1">Checked Out</a><div>July 14</div></td></tr>',
        "child-1",
        dt.date(2026, 7, 14),
        dt.UTC,
    )
    assert span_clock[0].occurred_at == dt.datetime(2026, 7, 14, 9, 10, tzinfo=dt.UTC)
    assert not discovery.parse_notification_context(
        '<tr class="box-shadow-default"><td><a class="image_video" '
        'href="/photo.jpg"></a><div>July 14</div></td></tr>',
        dt.date(2026, 7, 14),
        dt.UTC,
    )


def test_bounded_attendance_record_contains_only_linked_events() -> None:
    """Attendance snapshots expose bounded child events instead of profile UI."""
    record = discovery.parse_attendance_record(
        LEGACY_ATTENDANCE_FEED,
        "child-1",
        ZoneInfo("America/New_York"),
        discovery.RecordWindow(
            dt.date(2026, 7, 14),
            dt.date(2026, 7, 15),
            "https://example.test/feed",
            dt.datetime(2026, 7, 19, tzinfo=dt.UTC),
        ),
    )
    assert record.details["from_date"] == "2026-07-14"
    assert record.details["through_date"] == "2026-07-15"
    assert len(record.details["events"]) == 1
    assert record.details["events"][0]["type"] == "Checked In"


def test_enrollment_forms_contain_only_completed_values() -> None:
    """Enrollment extraction drops unused controls and unselected options."""
    record = discovery.parse_enrollment_forms(
        {
            "defaultForms": {
                "1": {
                    "form": json.dumps(
                        [
                            {"label": "Name", "name": "name", "value": "Mark"},
                            "not-an-object",
                            {"label": "Unused", "name": "unused", "value": ""},
                            {"label": 2, "name": "", "value": "Value"},
                            {
                                "label": "Room <b>choice</b>",
                                "name": "room",
                                "values": [
                                    {"label": "A", "value": "a"},
                                    {"label": "B", "value": "b", "selected": True},
                                ],
                            },
                        ]
                    )
                },
                "invalid": {"form": "not-json"},
                "wrong": {"form": "{}"},
                "missing": {"form": None},
                "not-a-form": "wrong",
            },
            "customForms": [
                {"form": json.dumps([{"name": "note", "value": "Complete"}])},
                {"form": "not-json"},
                "invalid",
            ],
        },
        "child-1",
        source_url="https://example.test/forms",
        observed_at=dt.datetime(2026, 7, 19, tzinfo=dt.UTC),
    )
    assert record.details == {
        "forms": (
            {
                "kind": "default",
                "id": "1",
                "entries": (
                    {"value": "Mark", "label": "Name", "name": "name"},
                    {"value": "Value"},
                    {
                        "value": ("b",),
                        "label": "Room choice",
                        "name": "room",
                    },
                ),
            },
            {
                "kind": "custom",
                "id": "0",
                "entries": ({"value": "Complete", "name": "note"},),
            },
        )
    }
    empty = discovery.parse_enrollment_forms(
        {"defaultForms": [], "customForms": {}},
        "child-1",
        source_url="https://example.test/forms",
        observed_at=dt.datetime(2026, 7, 19, tzinfo=dt.UTC),
    )
    assert empty.details == {"forms": ()}


@pytest.mark.asyncio
async def test_legacy_adapter_traverses_inclusive_dates() -> None:
    """Legacy discovery requests each bounded daily report through the limiter."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            document = LEGACY_DASHBOARD
        elif "notificationsV2" in request.url.path:
            document = LEGACY_FEED + LEGACY_ATTENDANCE_FEED
        else:
            document = LEGACY_REPORT
        return httpx.Response(200, text=document)

    class Limiter:
        calls = 0

        async def acquire(self) -> float:
            self.calls += 1
            return float(self.calls)

    limiter = Limiter()
    policy = config.RequestPolicy(
        quotas=(config.Quota(100, 1),),
        jitter_fraction=0,
        max_retries=0,
    )
    requester = scheduler.Requester(policy, limiter)
    async with httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = discovery.LegacyKindertalesAdapter(
            client,
            requester,
            dt.UTC,
        )
        assert len(await adapter.children()) == 2
        activities = [
            item
            async for item in adapter.activities(
                "child-1",
                cursor="ignored",
                from_date=dt.date(2026, 7, 14),
                through_date=dt.date(2026, 7, 15),
            )
        ]
        second_child = [
            item
            async for item in adapter.activities(
                "child-2",
                from_date=dt.date(2026, 7, 16),
                through_date=dt.date(2026, 7, 16),
            )
        ]
    assert len(activities) == 6
    assert len(second_child) == 2
    assert requests[2].url.params["activitydate"] == "07/14/2026"
    assert requests[3].url.params["activitydate"] == "07/15/2026"
    assert limiter.calls == 5
    assert sum("notificationsV2" in request.url.path for request in requests) == 1


@pytest.mark.asyncio
async def test_legacy_adapter_snapshots_child_record_routes() -> None:
    """Every documented child report area is requested read-only."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "notificationsV2" in request.url.path:
            return httpx.Response(200, text=LEGACY_ATTENDANCE_FEED)
        if request.url.path.endswith("allActiveForms.php"):
            return httpx.Response(200, json={"defaultForms": {}, "customForms": []})
        return httpx.Response(200, text="<title>Synthetic</title>")

    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as client:
        records = await discovery.LegacyKindertalesAdapter(client).child_records(
            "child-1",
            from_date=dt.date(2026, 7, 14),
            through_date=dt.date(2026, 7, 16),
        )
    assert [record.category for record in records] == [
        "attendance",
        "enrollment",
        "baby_bulletin",
        "immunizations",
        "medications",
        "milestones",
        "profile_documents",
    ]
    assert requests[1].content == b"cid=child-1"
    assert requests[2].url.params["subpg"] == "child"
    assert "subpg" not in requests[3].url.params


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", ["not-json", "[]"])
async def test_child_record_enrollment_response_must_be_an_object(
    payload: str,
) -> None:
    """Invalid structured enrollment responses fail at discovery."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "notificationsV2" in request.url.path:
            return httpx.Response(200, text="")
        return httpx.Response(200, text=payload)

    class Limiter:
        async def acquire(self) -> float:
            return 0.0

    requester = scheduler.Requester(
        config.RequestPolicy(
            quotas=(config.Quota(100, 1),),
            jitter_fraction=0,
            max_retries=0,
        ),
        Limiter(),
    )
    async with httpx.AsyncClient(
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    ) as client:
        adapter = discovery.LegacyKindertalesAdapter(client, requester=requester)
        with pytest.raises(discovery.DiscoveryError, match="enrollment endpoint"):
            await adapter.child_records(
                "child-1",
                from_date=dt.date(2026, 7, 14),
                through_date=dt.date(2026, 7, 16),
            )


@pytest.mark.asyncio
async def test_child_records_require_date_bounds() -> None:
    """Attendance records cannot silently escape the requested range."""
    async with httpx.AsyncClient(base_url="https://example.test") as client:
        with pytest.raises(discovery.DiscoveryError, match="--from and --through"):
            await discovery.LegacyKindertalesAdapter(client).child_records("child")


@pytest.mark.asyncio
async def test_legacy_adapter_snapshots_enabled_account_routes() -> None:
    """Message folders and billing are independently opt-in snapshots."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("subpg") == "scheduled":
            return httpx.Response(
                302,
                headers={"Location": "index.php?pg=dashboard"},
            )
        return httpx.Response(200, text="<p>Synthetic</p>")

    async with httpx.AsyncClient(
        base_url="https://example.test", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = discovery.LegacyKindertalesAdapter(client)
        assert await adapter.account_records(messages=False, billing=False) == ()
        records = await adapter.account_records(messages=True, billing=True)
    assert [record.category for record in records] == [
        "messages_inbox",
        "messages_sent",
        "messages_draft",
        "messages_contacts",
        "billing",
    ]
    assert requests[0].url.params["subpg"] == "inbox"
    assert requests[4].url.params["subpg"] == "contacts"
    assert "subpg" not in requests[5].url.params


@pytest.mark.asyncio
async def test_account_record_redirect_does_not_hide_authentication_failure() -> None:
    """Only a known unavailable-area redirect is skipped."""
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            302,
            headers={"Location": "index.php?pg=login"},
        )
    )
    async with httpx.AsyncClient(
        base_url="https://example.test", transport=transport
    ) as client:
        adapter = discovery.LegacyKindertalesAdapter(client)
        with pytest.raises(httpx.HTTPStatusError):
            await adapter.account_records(messages=False, billing=True)


@pytest.mark.asyncio
async def test_legacy_adapter_requires_explicit_bounds() -> None:
    """The HTML adapter refuses an accidental unbounded history traversal."""
    async with httpx.AsyncClient(base_url="https://example.test") as client:
        adapter = discovery.LegacyKindertalesAdapter(client)
        with pytest.raises(discovery.DiscoveryError, match="--from and --through"):
            _ = [item async for item in adapter.activities("child-1")]


@pytest.mark.asyncio
async def test_legacy_adapter_can_send_without_request_policy() -> None:
    """The adapter remains independently usable without a scheduler."""
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, text=LEGACY_DASHBOARD)
    )
    async with httpx.AsyncClient(
        base_url="https://example.test",
        transport=transport,
    ) as client:
        assert len(await discovery.LegacyKindertalesAdapter(client).children()) == 2
