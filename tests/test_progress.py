"""Tests for terminal progress reporting."""

import io
from typing import TextIO

import pytest

from kindertales_scraper import progress


class FakeBar:
    """Record bar updates and closure."""

    def __init__(self) -> None:
        self.updates = 0
        self.closed = False
        self.total: float | None = 0
        self.refreshes = 0

    def update(self, amount: int = 1) -> None:
        self.updates += amount

    def close(self) -> None:
        self.closed = True

    def refresh(self) -> None:
        self.refreshes += 1


def test_terminal_reporter_renders_progress() -> None:
    """The default terminal reporter renders labeled phase progress."""
    stream = io.StringIO()
    reporter = progress.TerminalReporter(stream, disable=False)
    reporter.start(progress.Stage.DISCOVERY, 1)
    reporter.advance(progress.Stage.DISCOVERY)
    reporter.close()
    progress.NullReporter().extend(progress.Stage.DISCOVERY, 1)
    output = stream.getvalue()
    assert progress.Stage.DISCOVERY.description in output
    assert progress.Stage.DISCOVERY.unit == "page"
    assert "100%" in output


def test_completed_bars_remain_extensible_until_final_close() -> None:
    """A completed bar can gain work without being closed or recreated."""
    bars: list[FakeBar] = []

    def factory(
        _stage: progress.Stage,
        _total: int,
        _stream: TextIO,
        *,
        disable: bool | None,
        position: int,
    ) -> FakeBar:
        assert disable is None
        assert position == _stage.position
        bar = FakeBar()
        bar.total = float(_total)
        bars.append(bar)
        return bar

    reporter = progress.TerminalReporter(bar_factory=factory)
    reporter.start(progress.Stage.MEDIA, 1)
    with pytest.raises(ValueError, match="already started"):
        reporter.start(progress.Stage.MEDIA, 1)
    reporter.advance(progress.Stage.MEDIA)
    reporter.extend(progress.Stage.MEDIA, 2)
    reporter.advance(progress.Stage.MEDIA)
    reporter.extend(progress.Stage.MEDIA, 0)
    reporter.start(progress.Stage.DISCOVERY, 1)
    reporter.close()
    assert tuple(stage.position for stage in progress.Stage) == (0, 1, 2, 3)
    assert len(bars) == 2
    assert bars[0].updates == 2
    assert bars[0].total == 3
    assert bars[0].refreshes == 1
    assert all(bar.closed for bar in bars)
