"""Progress reporting for interactive synchronization."""

import enum
import sys
from typing import Protocol, TextIO, cast

import attrs
import tqdm


class Stage(enum.Enum):
    """A bounded synchronization phase."""

    DISCOVERY = ("Discovering activities", "page")
    RECORDS = ("Discovering records", "page")
    DOCUMENTS = ("Archiving documents", "file")
    MEDIA = ("Archiving media", "file")

    @property
    def description(self) -> str:
        """Return the terminal label for this stage."""
        return self.value[0]

    @property
    def unit(self) -> str:
        """Return the item unit for this stage."""
        return self.value[1]

    @property
    def position(self) -> int:
        """Return this stage's stable terminal row."""
        return tuple(Stage).index(self)


class Bar(Protocol):
    """Operations used from a terminal progress bar."""

    def update(self, amount: int = 1) -> object:
        """Advance the bar."""
        ...

    def close(self) -> object:
        """Finish rendering the bar."""
        ...

    @property
    def total(self) -> float | None:
        """Return the current expected total."""
        ...

    @total.setter
    def total(self, value: float | None) -> None:
        """Set the current expected total."""
        ...

    def refresh(self) -> object:
        """Refresh the rendered total."""
        ...


class Reporter(Protocol):
    """Progress events emitted by the synchronization engine."""

    def start(self, stage: Stage, total: int) -> None:
        """Start a stage with a known item count."""
        ...

    def advance(self, stage: Stage) -> None:
        """Record one completed item in a stage."""
        ...

    def extend(self, stage: Stage, amount: int) -> None:
        """Add newly discovered work to an active stage."""
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

    def extend(self, _stage: Stage, _amount: int) -> None:
        """Discard newly discovered work."""

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
        position: int,
    ) -> Bar:
        """Return a configured bar."""
        ...


def _terminal_bar(
    stage: Stage,
    total: int,
    stream: TextIO,
    *,
    disable: bool | None,
    position: int,
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
            position=position,
            leave=True,
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
        if stage in self._bars:
            msg = f"progress stage already started: {stage.name}"
            raise ValueError(msg)
        self._bars[stage] = self.bar_factory(
            stage,
            total,
            self.stream,
            disable=self.disable,
            position=stage.position,
        )
        self._remaining[stage] = total

    def advance(self, stage: Stage) -> None:
        """Advance an active terminal bar."""
        self._bars[stage].update()
        self._remaining[stage] -= 1

    def extend(self, stage: Stage, amount: int) -> None:
        """Increase an active bar's total for newly discovered work."""
        if amount <= 0:
            return
        bar = self._bars[stage]
        bar.total = (bar.total or 0) + amount
        self._remaining[stage] += amount
        bar.refresh()

    def close(self) -> None:
        """Close every bar, including partially completed stages."""
        for bar in self._bars.values():
            bar.close()
        self._bars.clear()
        self._remaining.clear()
