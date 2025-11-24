"""``binyard migrate``: bring the database up to the shipped schema."""

from __future__ import annotations

import argparse

from binyard.cli.context import Context
from binyard.reporting.table import Column, Table
from binyard.store.migrate import migrate as apply_migrations
from binyard.store.migrate import pending

__all__ = ["handle", "register"]


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "migrate", help="apply any schema migrations the database has not seen"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="report what is outstanding and exit non zero if there is any",
    )
    parser.set_defaults(handler=handle)


def handle(context: Context, arguments: argparse.Namespace) -> int:
    outstanding = pending(context.connection)
    if arguments.check:
        context.emit(_table(outstanding, "Outstanding migrations"))
        return 1 if outstanding else 0
    applied = apply_migrations(context.connection)
    context.emit(_table(applied, "Applied migrations"))
    context.say(f"{len(applied)} applied, database is up to date")
    return 0


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
