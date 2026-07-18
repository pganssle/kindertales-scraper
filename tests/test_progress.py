"""Tests for terminal progress reporting."""

import io
from typing import TextIO

from kindertales_scraper import progress


class FakeBar:
    """Record bar updates and closure."""

    def __init__(self) -> None:
        self.updates = 0
        self.closed = False

    def update(self, amount: int = 1) -> None:
        self.updates += amount

    def close(self) -> None:
        self.closed = True


def test_terminal_reporter_renders_progress() -> None:
    """The default terminal reporter renders labeled phase progress."""
    stream = io.StringIO()
    reporter = progress.TerminalReporter(stream, disable=False)
    reporter.start(progress.Stage.DISCOVERY, 1)
    reporter.advance(progress.Stage.DISCOVERY)
    reporter.close()
    output = stream.getvalue()
    assert progress.Stage.DISCOVERY.description in output
    assert progress.Stage.DISCOVERY.unit == "child"
    assert "100%" in output


def test_restarting_and_closing_terminal_bars() -> None:
    """Restarting a phase closes its old bar and final closure clears all bars."""
    bars: list[FakeBar] = []

    def factory(
        _stage: progress.Stage,
        _total: int,
        _stream: TextIO,
        *,
        disable: bool | None,
    ) -> FakeBar:
        assert disable is None
        bar = FakeBar()
        bars.append(bar)
        return bar

    reporter = progress.TerminalReporter(bar_factory=factory)
    reporter.start(progress.Stage.MEDIA, 2)
    reporter.advance(progress.Stage.MEDIA)
    reporter.start(progress.Stage.MEDIA, 1)
    reporter.start(progress.Stage.DISCOVERY, 1)
    reporter.close()
    assert bars[0].updates == 1
    assert all(bar.closed for bar in bars)
