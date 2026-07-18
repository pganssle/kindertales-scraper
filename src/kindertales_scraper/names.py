"""Interactive preferred-name resolution for linked children."""

import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from pathlib import Path
from typing import TextIO

import attrs
import tomlkit

from . import config, discovery


class NameConfigurationRequiredError(RuntimeError):
    """Raised after printing mappings that the user chose to add manually."""


Input = Callable[[str], str]


def update_config(path: Path, mappings: Mapping[str, str]) -> None:
    """Merge preferred names into TOML without discarding comments or formatting."""
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    children = document.get("children")
    if not isinstance(children, MutableMapping):
        children = tomlkit.table()
        document["children"] = children
    names = children.get("names")
    if not isinstance(names, MutableMapping):
        names = tomlkit.table()
        children["names"] = names
    for official, preferred in mappings.items():
        names[official] = preferred
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(tomlkit.dumps(document), encoding="utf-8")
    temporary.chmod(path.stat().st_mode)
    temporary.replace(path)


def format_mappings(mappings: Mapping[str, str]) -> str:
    """Return a standalone TOML snippet for manual configuration."""
    document = tomlkit.document()
    children = tomlkit.table()
    names = tomlkit.table()
    for official, preferred in mappings.items():
        names[official] = preferred
    children["names"] = names
    document["children"] = children
    return tomlkit.dumps(document).rstrip()


@attrs.frozen
class PassthroughResolver:
    """Leave discovered child names unchanged for library callers."""

    def resolve(
        self,
        children: Sequence[discovery.Child],
    ) -> tuple[discovery.Child, ...]:
        """Return the children unchanged."""
        return tuple(children)


@attrs.frozen
class InteractiveResolver:
    """Resolve and optionally persist missing preferred child names."""

    settings: config.Config
    input_fn: Input = input
    output: TextIO = sys.stdout

    def resolve(
        self,
        children: Sequence[discovery.Child],
    ) -> tuple[discovery.Child, ...]:
        """Apply configured mappings or prompt for every unmapped child."""
        if self.settings.use_kindertales_name:
            return tuple(children)
        mappings = dict(self.settings.child_names)
        missing = tuple(child for child in children if child.name not in mappings)
        additions = {
            child.name: self._preferred_name(child.name)
            for child in missing
        }
        if additions:
            self._persist_or_exit(additions)
            mappings.update(additions)
        return tuple(
            attrs.evolve(child, name=mappings[child.name])
            for child in children
        )

    def _preferred_name(self, official: str) -> str:
        while True:
            value = self.input_fn(
                f"Preferred name for Kindertales child {official!r}: "
            ).strip()
            if value:
                return value

    def _persist_or_exit(self, additions: Mapping[str, str]) -> None:
        path = self.settings.source_path
        if path is not None:
            choice = self.input_fn(
                f"Update {path} with these preferred names? [Y/p] "
            ).strip().casefold()
            if choice in {"", "y", "yes"}:
                update_config(path, additions)
                return
        print(format_mappings(additions), file=self.output)
        raise NameConfigurationRequiredError
