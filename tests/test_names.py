"""Tests for preferred child-name configuration."""

import io
from pathlib import Path

import pytest

from kindertales_scraper import config, discovery, names


def children() -> tuple[discovery.Child, ...]:
    """Return synthetic linked children."""
    return (
        discovery.Child("one", "Child-1", "center"),
        discovery.Child("two", "Child-2", "center"),
    )


def test_configured_and_official_names_need_no_prompt() -> None:
    """Configured mappings apply by official name and may be bypassed globally."""
    resolver = names.InteractiveResolver(
        config.Config(
            "a@example.com",
            child_names={"Child-1": "Alex", "Child-2": "Sam"},
        ),
        input_fn=lambda _prompt: pytest.fail("unexpected prompt"),
    )
    assert [child.name for child in resolver.resolve(children())] == ["Alex", "Sam"]
    official = names.InteractiveResolver(
        config.Config("a@example.com", use_kindertales_name=True),
        input_fn=lambda _prompt: pytest.fail("unexpected prompt"),
    )
    assert official.resolve(children()) == children()


def test_prompted_names_update_config_without_reformatting(tmp_path: Path) -> None:
    """Interactive additions preserve unrelated TOML comments and formatting."""
    path = tmp_path / "config.toml"
    path.write_text(
        """# leading comment
[account] # account comment
email = "a@example.com"  # keep spacing

[children]
use_kindertales_name = false # keep this

[children.names]
"Child-1" = "Alex" # existing
""",
        encoding="utf-8",
    )
    settings = config.load(path)
    answers = iter(("Sam", ""))
    resolved = names.InteractiveResolver(
        settings,
        input_fn=lambda _prompt: next(answers),
    ).resolve(children())
    updated = path.read_text(encoding="utf-8")
    assert [child.name for child in resolved] == ["Alex", "Sam"]
    assert "# leading comment" in updated
    assert 'email = "a@example.com"  # keep spacing' in updated
    assert '"Child-1" = "Alex" # existing' in updated
    assert 'Child-2 = "Sam"' in updated


@pytest.mark.parametrize(
    "initial",
    [
        '[account]\nemail = "a@example.com"\n',
        '[account]\nemail = "a@example.com"\n[children]\n',
    ],
)
def test_update_config_creates_missing_name_tables(
    tmp_path: Path,
    initial: str,
) -> None:
    """Preferred-name updates create only the missing TOML tables."""
    path = tmp_path / "config.toml"
    path.write_text(initial, encoding="utf-8")
    names.update_config(path, {"Child-2": "Mark"})
    assert config.load(path).child_names == {"Child-2": "Mark"}


def test_print_and_exit_reprompts_for_empty_name() -> None:
    """Manual mode prints valid TOML after rejecting an empty preferred name."""
    output = io.StringIO()
    answers = iter(("", "Mark", "p"))
    resolver = names.InteractiveResolver(
        config.Config("a@example.com", source_path=Path("config.toml")),
        input_fn=lambda _prompt: next(answers),
        output=output,
    )
    with pytest.raises(names.NameConfigurationRequiredError):
        resolver.resolve((discovery.Child("two", "Child-2"),))
    assert '[children.names]\nChild-2 = "Mark"' in output.getvalue()


def test_manual_configuration_when_source_path_is_unknown() -> None:
    """In-memory settings can only print a mapping for the caller to apply."""
    output = io.StringIO()
    resolver = names.InteractiveResolver(
        config.Config("a@example.com"),
        input_fn=lambda _prompt: "Mark",
        output=output,
    )
    with pytest.raises(names.NameConfigurationRequiredError):
        resolver.resolve((discovery.Child("two", "Child-2"),))
    assert "Mark" in output.getvalue()


def test_passthrough_resolver() -> None:
    """Library synchronization remains non-interactive by default."""
    assert names.PassthroughResolver().resolve(children()) == children()
