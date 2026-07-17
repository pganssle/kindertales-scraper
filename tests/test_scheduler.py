"""Tests for request and DAG scheduling."""

import asyncio
import datetime as dt
import email.utils
import random

import httpx
import pytest
from hypothesis import given, strategies

from kindertales_scraper import config, scheduler


class VirtualClock:
    """A monotonic clock advanced by asynchronous sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def policy(**changes: object) -> config.RequestPolicy:
    """Build a request policy with concise test defaults."""
    values = {
        "quotas": (config.Quota(2, 1.0),),
        "max_in_flight": 2,
        "max_media_downloads": 1,
        "jitter_fraction": 0.0,
        "max_retries": 2,
        "stop_after_forbidden": 2,
    }
    values.update(changes)
    return config.RequestPolicy(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_limiter_uses_strictest_spacing_and_rolling_windows() -> None:
    """Spacing and rolling admission both constrain dispatch times."""
    clock = VirtualClock()
    settings = policy(
        quotas=(config.Quota(2, 1.0), config.Quota(3, 3.0)),
        jitter_fraction=0.5,
    )
    limiter = scheduler.RollingLimiter(
        settings,
        clock,
        clock.sleep,
        random.Random(4),
    )
    assert limiter.base_spacing == 1.0
    dispatches = [await limiter.acquire() for _ in range(4)]
    assert dispatches == sorted(dispatches)
    assert dispatches[2] - dispatches[0] >= 1.0
    assert dispatches[3] - dispatches[0] >= 3.0


@given(
    count=strategies.integers(min_value=1, max_value=8),
    window=strategies.floats(min_value=0.01, max_value=10, allow_nan=False),
    jitter=strategies.floats(min_value=0, max_value=0.9, allow_nan=False),
)
def test_quota_is_never_violated(count: int, window: float, jitter: float) -> None:
    """Centered negative jitter cannot violate a rolling quota."""

    async def exercise() -> list[float]:
        clock = VirtualClock()
        limiter = scheduler.RollingLimiter(
            policy(
                quotas=(config.Quota(count, window),),
                jitter_fraction=jitter,
            ),
            clock,
            clock.sleep,
            random.Random(1),
        )
        return [await limiter.acquire() for _ in range(count * 3)]

    dispatches = asyncio.run(exercise())
    for index, dispatch in enumerate(dispatches[count:], start=count):
        assert dispatch - dispatches[index - count] >= window - 1e-9


@pytest.mark.asyncio
async def test_dag_priority_and_dependencies() -> None:
    """Each ready generation orders media, depth, and discovery stably."""
    observed: list[str] = []

    def action(name: str) -> scheduler.Action:
        async def run() -> str:
            observed.append(name)
            return name

        return run

    graph = scheduler.DAGScheduler(max_in_flight=1, max_media_downloads=1)
    graph.add(scheduler.Work("root", action("root"), order=0))
    graph.add(
        scheduler.Work(
            "ordinary",
            action("ordinary"),
            frozenset({"root"}),
            depth=3,
            order=1,
        )
    )
    graph.add(
        scheduler.Work(
            "media",
            action("media"),
            frozenset({"root"}),
            media=True,
            depth=1,
            order=2,
        )
    )
    results = await graph.run()
    assert observed == ["root", "media", "ordinary"]
    assert results["media"].value == "media"


@pytest.mark.asyncio
async def test_dependency_failure_is_skipped() -> None:
    """Failures are captured and prevent dependent actions from running."""

    async def fail() -> None:
        msg = "failed"
        raise RuntimeError(msg)

    async def should_not_run() -> None:
        pytest.fail("dependent action ran")

    graph = scheduler.DAGScheduler(2, 1)
    graph.add(scheduler.Work("failure", fail))
    graph.add(scheduler.Work("dependent", should_not_run, frozenset({"failure"})))
    results = await graph.run()
    assert isinstance(results["failure"].error, RuntimeError)
    assert results["dependent"].skipped


@pytest.mark.parametrize(
    "works",
    [
        (
            scheduler.Work("x", lambda: asyncio.sleep(0)),
            scheduler.Work("x", lambda: asyncio.sleep(0)),
        ),
        (scheduler.Work("x", lambda: asyncio.sleep(0), frozenset({"missing"})),),
        (
            scheduler.Work("x", lambda: asyncio.sleep(0), frozenset({"y"})),
            scheduler.Work("y", lambda: asyncio.sleep(0), frozenset({"x"})),
        ),
    ],
)
def test_invalid_graph(works: tuple[scheduler.Work, ...]) -> None:
    """Duplicate, missing, and cyclic work is rejected."""
    graph = scheduler.DAGScheduler(1, 1)
    if len(works) > 1 and works[0].key == works[1].key:
        graph.add(works[0])
        with pytest.raises(scheduler.GraphError, match="duplicate"):
            graph.add(works[1])
        return
    for work in works:
        graph.add(work)
    with pytest.raises(scheduler.GraphError, match="unknown|cycle"):
        asyncio.run(graph.run())


@pytest.mark.asyncio
async def test_concurrency_limits() -> None:
    """Total and media work honor their separate concurrency limits."""
    active = 0
    active_media = 0
    maximum = 0
    maximum_media = 0

    def action(is_media: bool) -> scheduler.Action:
        async def run() -> None:
            nonlocal active, active_media, maximum, maximum_media
            active += 1
            active_media += is_media
            maximum = max(maximum, active)
            maximum_media = max(maximum_media, active_media)
            await asyncio.sleep(0)
            active -= 1
            active_media -= is_media

        return run

    graph = scheduler.DAGScheduler(2, 1)
    for index in range(6):
        graph.add(scheduler.Work(str(index), action(index < 3), media=index < 3))
    await graph.run()
    assert maximum == 2
    assert maximum_media == 1


class ImmediateLimiter:
    """Count admissions without delaying tests."""

    def __init__(self) -> None:
        self.calls = 0

    async def acquire(self) -> float:
        self.calls += 1
        return float(self.calls)


def response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    """Construct a response with a request for error handling."""
    return httpx.Response(
        status, headers=headers, request=httpx.Request("GET", "https://example.test")
    )


@pytest.mark.asyncio
async def test_request_retries_and_retry_after() -> None:
    """Retries count against quotas and honor numeric Retry-After."""
    limiter = ImmediateLimiter()
    sleeps: list[float] = []
    responses = iter((response(503, {"Retry-After": "2.5"}), response(200)))

    async def send() -> httpx.Response:
        return next(responses)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    requester = scheduler.Requester(policy(), limiter, sleep)
    assert (await requester.request(send)).status_code == 200
    assert limiter.calls == 2
    assert sleeps == [2.5]


@pytest.mark.asyncio
async def test_transport_retry_and_exhaustion() -> None:
    """Transport failures back off and the last failure is raised."""
    limiter = ImmediateLimiter()
    sleeps: list[float] = []

    async def send() -> httpx.Response:
        request = httpx.Request("GET", "https://example.test")
        raise httpx.ConnectError("no connection", request=request)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    requester = scheduler.Requester(policy(), limiter, sleep)
    with pytest.raises(httpx.ConnectError):
        await requester.request(send)
    assert limiter.calls == 3
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_forbidden_stop_and_reset() -> None:
    """Repeated 403/429 stops, while an intervening success resets the count."""
    limiter = ImmediateLimiter()
    requester = scheduler.Requester(policy(max_retries=0), limiter)
    statuses = iter((403, 200, 403, 403))

    async def send() -> httpx.Response:
        return response(next(statuses))

    assert (await requester.request(send)).status_code == 403
    assert (await requester.request(send)).status_code == 200
    assert (await requester.request(send)).status_code == 403
    with pytest.raises(scheduler.RequestPolicyError, match="repeated"):
        await requester.request(send, media=True)


@pytest.mark.asyncio
async def test_retry_exhaustion_returns_response() -> None:
    """The final retryable HTTP response is returned to the caller."""
    limiter = ImmediateLimiter()
    requester = scheduler.Requester(policy(max_retries=0), limiter)

    async def send() -> httpx.Response:
        return response(500)

    assert (await requester.request(send, media=True)).status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, 1.0),
        ("invalid-date", 1.0),
        (
            email.utils.format_datetime(
                dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)
            ),
            None,
        ),
        ("-2", 0.0),
    ],
)
async def test_retry_delay(header: str | None, expected: float | None) -> None:
    """Retry delay supports backoff, dates, and non-negative seconds."""
    limiter = ImmediateLimiter()
    sleeps: list[float] = []
    responses = iter(
        (
            response(503, {"Retry-After": header} if header is not None else None),
            response(200),
        )
    )

    async def send() -> httpx.Response:
        return next(responses)

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    requester = scheduler.Requester(policy(), limiter, sleep)
    assert (await requester.request(send)).status_code == 200
    if expected is None:
        assert 0 <= sleeps[0] <= 5
    else:
        assert sleeps == [expected]
