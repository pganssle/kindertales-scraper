"""Quota-aware requests and dependency-ordered work scheduling."""

import asyncio
import datetime as dt
import email.utils
import heapq
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol

import attrs
import httpx

from . import config

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]
Action = Callable[[], Awaitable[Any]]
Sender = Callable[[], Awaitable[httpx.Response]]


class Limiter(Protocol):
    """A request admission controller."""

    async def acquire(self) -> float:
        """Wait for and record an admission."""
        ...


class GraphError(ValueError):
    """Raised for an invalid work graph."""


class RequestPolicyError(RuntimeError):
    """Raised after repeated authorization or throttling responses."""


@attrs.define
class RollingLimiter:
    """Admit requests under every configured rolling quota."""

    policy: config.RequestPolicy
    clock: Clock = time.monotonic
    sleep: Sleeper = asyncio.sleep
    random_source: random.Random = attrs.field(factory=random.Random)
    _dispatches: deque[float] = attrs.field(factory=deque, init=False)
    _last_dispatch: float | None = attrs.field(default=None, init=False)
    _lock: asyncio.Lock = attrs.field(factory=asyncio.Lock, init=False)

    @property
    def base_spacing(self) -> float:
        """Return spacing imposed by the strictest normalized quota."""
        return max(quota.window_seconds / quota.count for quota in self.policy.quotas)

    async def acquire(self) -> float:
        """Wait until a request is admissible and return its dispatch time."""
        async with self._lock:
            jitter = self.random_source.uniform(
                -self.policy.jitter_fraction,
                self.policy.jitter_fraction,
            )
            while True:
                now = self.clock()
                wait = self._required_wait(now, jitter)
                if wait <= 0:
                    self._dispatches.append(now)
                    self._last_dispatch = now
                    return now
                await self.sleep(wait)

    def _required_wait(self, now: float, jitter: float) -> float:
        longest_window = max(quota.window_seconds for quota in self.policy.quotas)
        while self._dispatches and self._dispatches[0] <= now - longest_window:
            self._dispatches.popleft()
        waits = [0.0]
        if self._last_dispatch is not None:
            spacing = self.base_spacing * (1 + jitter)
            waits.append(self._last_dispatch + spacing - now)
        for quota in self.policy.quotas:
            within = tuple(
                dispatch
                for dispatch in self._dispatches
                if dispatch > now - quota.window_seconds
            )
            if len(within) >= quota.count:
                waits.append(within[-quota.count] + quota.window_seconds - now)
        return max(waits)


@attrs.frozen
class Work:
    """One unit of dependency-ordered asynchronous work."""

    key: str
    action: Action
    dependencies: frozenset[str] = frozenset()
    media: bool = False
    depth: int = 0
    order: int = 0


@attrs.frozen
class WorkResult:
    """The value or failure associated with a unit of work."""

    value: Any = None
    error: BaseException | None = None
    skipped: bool = False


@attrs.define
class DAGScheduler:
    """Run ready work in media/depth/discovery priority order."""

    max_in_flight: int
    max_media_downloads: int
    _work: dict[str, Work] = attrs.field(factory=dict, init=False)

    def add(self, work: Work) -> None:
        """Add unique work to the graph."""
        if work.key in self._work:
            msg = f"duplicate work key: {work.key}"
            raise GraphError(msg)
        self._work[work.key] = work

    async def run(self) -> Mapping[str, WorkResult]:
        """Validate and execute the graph, skipping failed dependents."""
        self._validate()
        pending = dict(self._work)
        results: dict[str, WorkResult] = {}
        total = asyncio.Semaphore(self.max_in_flight)
        media = asyncio.Semaphore(self.max_media_downloads)
        while pending:
            ready = [
                work for work in pending.values() if work.dependencies <= results.keys()
            ]
            ready.sort(key=lambda work: (-int(work.media), -work.depth, work.order))
            for work in ready:
                del pending[work.key]
            completed = await asyncio.gather(
                *(self._execute(work, results, total, media) for work in ready)
            )
            results.update(zip((work.key for work in ready), completed, strict=True))
        return results

    def _validate(self) -> None:
        known = self._work.keys()
        for work in self._work.values():
            missing = work.dependencies - known
            if missing:
                msg = f"unknown dependencies for {work.key}: {sorted(missing)}"
                raise GraphError(msg)
        remaining = {key: set(work.dependencies) for key, work in self._work.items()}
        while remaining:
            ready = {key for key, dependencies in remaining.items() if not dependencies}
            if not ready:
                msg = "work graph contains a cycle"
                raise GraphError(msg)
            remaining = {
                key: dependencies - ready
                for key, dependencies in remaining.items()
                if key not in ready
            }

    async def _execute(
        self,
        work: Work,
        results: Mapping[str, WorkResult],
        total: asyncio.Semaphore,
        media: asyncio.Semaphore,
    ) -> WorkResult:
        if any(
            results[key].error is not None or results[key].skipped
            for key in work.dependencies
        ):
            return WorkResult(skipped=True)
        try:
            async with total:
                if work.media:
                    async with media:
                        return WorkResult(value=await work.action())
                return WorkResult(value=await work.action())
        except Exception as error:  # noqa: BLE001
            return WorkResult(error=error)


@attrs.define
class DynamicScheduler:
    """Run priority work that may enqueue more work while it is running."""

    max_in_flight: int
    max_media_downloads: int
    _queue: asyncio.PriorityQueue[tuple[tuple[int, int, int], int, Work]] = attrs.field(
        factory=asyncio.PriorityQueue, init=False
    )
    _keys: set[str] = attrs.field(factory=set, init=False)
    _sequence: int = attrs.field(default=0, init=False)
    _running: bool = attrs.field(default=False, init=False)
    _finished: bool = attrs.field(default=False, init=False)

    def add(self, work: Work) -> None:
        """Add work before or during a run, ordered by media, depth, and order."""
        if self._finished:
            msg = "cannot add work after the scheduler has finished"
            raise GraphError(msg)
        if work.dependencies:
            msg = "dynamic work expresses dependencies by enqueueing successors"
            raise GraphError(msg)
        if work.key in self._keys:
            msg = f"duplicate work key: {work.key}"
            raise GraphError(msg)
        self._keys.add(work.key)
        priority = (-int(work.media), -work.depth, work.order)
        self._queue.put_nowait((priority, self._sequence, work))
        self._sequence += 1

    async def run(self) -> Mapping[str, WorkResult]:
        """Run until every initial and dynamically generated item completes."""
        if self._running or self._finished:
            msg = "dynamic scheduler can only run once"
            raise GraphError(msg)
        self._running = True
        results: dict[str, WorkResult] = {}
        media = asyncio.Semaphore(self.max_media_downloads)
        workers = tuple(
            asyncio.create_task(self._worker(results, media))
            for _ in range(self.max_in_flight)
        )
        try:
            await self._queue.join()
        finally:
            self._finished = True
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
        return results

    async def _worker(
        self,
        results: dict[str, WorkResult],
        media: asyncio.Semaphore,
    ) -> None:
        while True:
            _priority, _sequence, work = await self._queue.get()
            try:
                results[work.key] = await self._execute(work, media)
            finally:
                self._queue.task_done()

    @staticmethod
    async def _execute(work: Work, media: asyncio.Semaphore) -> WorkResult:
        try:
            if work.media:
                async with media:
                    return WorkResult(value=await work.action())
            return WorkResult(value=await work.action())
        except Exception as error:  # noqa: BLE001
            return WorkResult(error=error)


@attrs.define
class Requester:
    """Apply quotas, concurrency, retries, and stop conditions to requests."""

    policy: config.RequestPolicy
    limiter: Limiter
    sleep: Sleeper = asyncio.sleep
    _total: asyncio.Semaphore = attrs.field(init=False)
    _media: asyncio.Semaphore = attrs.field(init=False)
    _forbidden: int = attrs.field(default=0, init=False)
    _admissions: list[tuple[int, int, asyncio.Future[float]]] = attrs.field(
        factory=list, init=False
    )
    _admission_sequence: int = attrs.field(default=0, init=False)
    _dispatcher: asyncio.Task[None] | None = attrs.field(default=None, init=False)

    def __attrs_post_init__(self) -> None:
        """Create concurrency controls from the request policy."""
        self._total = asyncio.Semaphore(self.policy.max_in_flight)
        self._media = asyncio.Semaphore(self.policy.max_media_downloads)

    async def request(self, send: Sender, *, media: bool = False) -> httpx.Response:
        """Send a request, counting every retry against configured quotas."""
        for attempt in range(self.policy.max_retries + 1):
            await self._acquire(media=media)
            try:
                async with self._total:
                    if media:
                        async with self._media:
                            response = await send()
                    else:
                        response = await send()
            except httpx.TransportError:
                if attempt == self.policy.max_retries:
                    raise
                await self.sleep(min(2**attempt, 30))
                continue
            if response.status_code in {403, 429}:
                self._forbidden += 1
                if self._forbidden >= self.policy.stop_after_forbidden:
                    msg = "stopping after repeated 403/429 responses"
                    raise RequestPolicyError(msg)
            else:
                self._forbidden = 0
            if response.status_code not in {408, 429, 500, 502, 503, 504}:
                return response
            if attempt == self.policy.max_retries:
                return response
            await self.sleep(self._retry_delay(response, attempt))
        raise RuntimeError  # pragma: no cover

    async def _acquire(self, *, media: bool) -> float:
        loop = asyncio.get_running_loop()
        admitted = loop.create_future()
        heapq.heappush(
            self._admissions,
            (-int(media), self._admission_sequence, admitted),
        )
        self._admission_sequence += 1
        if self._dispatcher is None:
            self._dispatcher = asyncio.create_task(self._dispatch_admissions())
        return await asyncio.shield(admitted)

    async def _dispatch_admissions(self) -> None:
        try:
            while self._admissions:
                _priority, _sequence, admitted = heapq.heappop(self._admissions)
                try:
                    dispatch = await self.limiter.acquire()
                except Exception as error:  # noqa: BLE001
                    admitted.set_exception(error)
                else:
                    admitted.set_result(dispatch)
        finally:
            self._dispatcher = None

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After")
        if value is None:
            return float(min(2**attempt, 30))
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError, OverflowError):
                return float(min(2**attempt, 30))
            now = dt.datetime.now(dt.UTC)
            return max(0.0, float((parsed - now).total_seconds()))
