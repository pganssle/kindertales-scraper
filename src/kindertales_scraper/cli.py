"""Command-line interface for kindertales-scraper."""

import argparse
from collections.abc import Sequence

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="kindertales-scraper")
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    build_parser().parse_args(argv)
    return 0
