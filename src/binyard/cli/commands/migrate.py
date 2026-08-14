"""``binyard migrate``: bring the database up to the shipped schema.

Safe to run twice, and safe to run on start, because the runner applies only
what the database has not seen and refuses anything that has been edited since
it was applied. The container's entrypoint runs it every time it starts for
exactly that reason.

``--check`` reports what is outstanding and exits non zero if there is any,
which is what a deployment gate wants. ``--environment`` also prints where the
installed versions differ from the pins, because a migration that behaves
strangely is usually a library that is not the one the build was tested with.
"""

from __future__ import annotations

import argparse

from binyard.cli.context import Context
from binyard.environment import inspect_environment
from binyard.reporting.table import Column, Table
from binyard.store.migrate import migrate as apply_migrations
from binyard.store.migrate import pending

__all__ = ["handle", "register"]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register this subcommand's parser and its handler."""
    parser = subparsers.add_parser(
        "migrate", help="apply any schema migrations the database has not seen"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is outstanding and exit non zero if there is any",
    )
    parser.add_argument(
        "--environment",
        action="store_true",
        help="also report where the installed versions differ from the pins",
    )
    parser.set_defaults(handler=handle)


def handle(context: Context, arguments: argparse.Namespace) -> int:
    """Run the command and return the exit code."""
    outstanding = pending(context.connection)
    if arguments.check:
        context.emit(_table(outstanding, "Outstanding migrations"))
        return 1 if outstanding else 0
    applied = apply_migrations(context.connection)
    context.emit(_table(applied, "Applied migrations"))
    context.say(f"{len(applied)} applied, database is up to date")
    return 0


def _environment_table() -> Table:
    """What is installed against what this build was pinned to."""
    return Table(
        title="Installed against pinned",
        columns=(
            Column("name", "Package"),
            Column("declared", "Pinned"),
            Column("found", "Installed"),
            Column("note", "Note"),
        ),
        rows=tuple(item.describe() for item in inspect_environment()),
    )


def _table(migrations, title: str) -> Table:
    columns = (
        Column.number("version", "Version"),
        Column("name", "Name"),
        Column("checksum", "Checksum"),
    )
    rows = tuple(
        {
            "version": migration.version,
            "name": migration.name,
            "checksum": migration.checksum[:12],
        }
        for migration in migrations
    )
    return Table(title=title, columns=columns, rows=rows)
