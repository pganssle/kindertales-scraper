"""Progress reporting for interactive synchronization."""

import enum
import sys
from typing import Protocol, TextIO, cast

import attrs
import tqdm


class Stage(enum.Enum):
    """A bounded synchronization phase."""

    DISCOVERY = ("Discovering activities", "child")
    MEDIA = ("Archiving media", "file")

    @property
    def description(self) -> str:
        """Return the terminal label for this stage."""
        return self.value[0]

    @property
    def unit(self) -> str:
        """Return the item unit for this stage."""
        return self.value[1]


class Bar(Protocol):
    """Operations used from a terminal progress bar."""

    def update(self, amount: int = 1) -> object:
        """Advance the bar."""
        ...

    def close(self) -> object:
        """Finish rendering the bar."""
        ...


class Reporter(Protocol):
    """Progress events emitted by the synchronization engine."""

    def start(self, stage: Stage, total: int) -> None:
        """Start a stage with a known item count."""
        ...

    def advance(self, stage: Stage) -> None:
        """Record one completed item in a stage."""
        ...

    def close(self) -> None:
        """Close all active progress displays."""
        ...


@attrs.frozen
class NullReporter:
    """Discard progress events for library callers and tests."""

    def start(self, _stage: Stage, _total: int) -> None:
        """Discard a stage start."""

    def advance(self, _stage: Stage) -> None:
        """Discard an item completion."""

    def close(self) -> None:
        """Discard closure."""


class BarFactory(Protocol):
    """Create one terminal progress bar."""

    def __call__(
        self,
        stage: Stage,
        total: int,
        stream: TextIO,
        *,
        disable: bool | None,
    ) -> Bar:
        """Return a configured bar."""
        ...


def _terminal_bar(
    stage: Stage,
    total: int,
    stream: TextIO,
    *,
    disable: bool | None,
) -> Bar:
    return cast(
        "Bar",
        tqdm.tqdm(
            total=total,
            desc=stage.description,
            unit=stage.unit,
            dynamic_ncols=True,
            file=stream,
            disable=disable,
        ),
    )


@attrs.define
class TerminalReporter:
    """Render synchronization progress to a terminal."""

    stream: TextIO = attrs.field(factory=lambda: sys.stderr)
    disable: bool | None = None
    bar_factory: BarFactory = _terminal_bar
    _bars: dict[Stage, Bar] = attrs.field(factory=dict, init=False)
    _remaining: dict[Stage, int] = attrs.field(factory=dict, init=False)

    def start(self, stage: Stage, total: int) -> None:
        """Create a terminal bar for a stage."""
        existing = self._bars.pop(stage, None)
        if existing is not None:
            existing.close()
        self._bars[stage] = self.bar_factory(
            stage,
            total,
            self.stream,
            disable=self.disable,
        )
        self._remaining[stage] = total

    def advance(self, stage: Stage) -> None:
        """Advance an active terminal bar."""
        self._bars[stage].update()
        self._remaining[stage] -= 1
        if self._remaining[stage] <= 0:
            self._bars.pop(stage).close()
            del self._remaining[stage]

    def close(self) -> None:
        """Close every bar, including partially completed stages."""
        for bar in self._bars.values():
            bar.close()
        self._bars.clear()
        self._remaining.clear()
