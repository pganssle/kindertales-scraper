"""Tests for child, activity, and media discovery."""

import datetime as dt
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest

from kindertales_scraper import discovery


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
