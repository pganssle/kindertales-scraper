"""Command-line interface for kindertales-scraper."""

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path

from . import __version__, auth, config, credentials, sync


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line parser."""
    parser = argparse.ArgumentParser(prog="kindertales-scraper")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    configure = subparsers.add_parser("configure", help="create local configuration")
    configure.add_argument("--config", type=Path, default=Path("config.toml"))
    configure.add_argument("--email")
    credential_parser = subparsers.add_parser("credentials")
    credential_commands = credential_parser.add_subparsers(dest="credential_command")
    delete = credential_commands.add_parser("delete")
    delete.add_argument("--config", type=Path, default=Path("config.toml"))
    synchronize = subparsers.add_parser("sync", help="discover and archive media")
    synchronize.add_argument("--config", type=Path, default=Path("config.toml"))
    synchronize.add_argument("--from", dest="from_date", type=sync.parse_date)
    synchronize.add_argument("--through", dest="through_date", type=sync.parse_date)
    synchronize.add_argument("--dry-run", action="store_true")
    synchronize.add_argument("--headed", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line interface."""
    arguments = build_parser().parse_args(argv)
    if arguments.command == "configure":
        email = arguments.email or input("Kindertales account email: ")
        config.write_initial(arguments.config, email)
        credentials.password(email)
    elif (
        arguments.command == "credentials" and arguments.credential_command == "delete"
    ):
        settings = config.load(arguments.config)
        auth.SessionCache(settings).delete()
        credentials.delete(settings.email)
    elif arguments.command == "sync":
        settings = config.load(arguments.config)
        summary = asyncio.run(
            sync.run_configured(
                settings,
                sync.Bounds(arguments.from_date, arguments.through_date),
                dry_run=arguments.dry_run,
                headed=arguments.headed,
            )
        )
        print(  # noqa: T201 - this is the command-line presentation boundary.
            f"{summary.children} children, {summary.activities} activities, "
            f"{summary.media} media" + (" (dry run)" if summary.dry_run else "")
        )
    return 0
