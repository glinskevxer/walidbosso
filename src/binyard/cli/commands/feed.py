"""``binyard feed``: read the indicator file and turn it into tickets.

This is the command the nightly job runs. It takes a path, or reads standard
input, and it is safe to run on the same file every hour of the day because the
importer recognises what it has already seen.

The exit code says something useful rather than only whether the process
crashed. Nothing to do is zero, work done cleanly is zero, and anything refused
or unreadable is two, so a cron entry that mails on failure mails on the right
days.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime

from binyard.cli.context import Context
from binyard.contracts.application import Priority
from binyard.discounts.posted import default_book
from binyard.errors import ValidationError
from binyard.feed.reader import read_feed
from binyard.identity import Sequence, crop_year_of
from binyard.reporting.table import Column, Table
from binyard.services.importing import ImportResult, Outcome, import_feed

__all__ = ["handle", "register"]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register this subcommand's parser and its handler."""
    parser = subparsers.add_parser("feed", help="import the scale house feed")
    parser.add_argument(
        "path", nargs="?", default="-", help="the feed file, or - for standard input"
    )
    parser.add_argument(
        "--bin",
        dest="bins",
        action="append",
        default=[],
        required=True,
        metavar="CROP=BIN",
        help="where a crop goes, repeatable",
    )
    parser.add_argument(
        "--priority",
        choices=[item.value for item in Priority],
        default=Priority.EARLIEST_DEADLINE.value,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse and report without writing anything",
    )
    parser.set_defaults(handler=handle)


def handle(context: Context, arguments: argparse.Namespace) -> int:
    """Run the command and return the exit code."""
    text = sys.stdin.read() if arguments.path == "-" else _read(arguments.path)
    batch = read_feed(text)

    if arguments.dry_run:
        context.emit(_parse_table(batch))
        context.say(f"{batch.accepted} readable, {batch.refused} not")
        return 2 if batch.refused else 0

    work = context.work()
    result = import_feed(
        work,
        batch,
        Sequence(context.settings.location, crop_year_of(context.on)),
        source=arguments.path,
        book=default_book(),
        bin_for=_bins(arguments.bins),
        at=datetime.now(),
        priority=Priority(arguments.priority),
    )
    context.emit(_result_table(result))
    context.say(
        f"{result.taken} taken, {result.duplicates} already seen, {result.bushels_taken} in"
    )
    for record in result.records:
        if record.outcome in {Outcome.REFUSED, Outcome.NOT_GRADEABLE}:
            context.warn(f"line {record.line_number}: {record.reason}")
    return 0 if result.is_clean else 2


def _read(path: str) -> str:
    from pathlib import Path

    where = Path(path)
    if not where.is_file():
        raise ValidationError("no such feed file", path=path)
    return where.read_text(encoding="ascii", errors="replace")


def _bins(pairs: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for text in pairs:
        if "=" not in text:
            raise ValidationError("a bin is written CROP=BIN", got=text)
        crop, _, identifier = text.partition("=")
        mapping[crop.strip().upper()] = identifier.strip()
    return mapping


def _parse_table(batch) -> Table:
    rows = [record.describe() for record in batch.records]
    return Table(
        title="Feed records read",
        columns=(
            Column.number("line", "Line"),
            Column("type", "Type"),
            Column("account", "Account"),
            Column("commodity", "Crop"),
            Column.number("net", "Net"),
            Column.number("moisture", "Moisture"),
            Column("gradeable", "Gradeable"),
        ),
        rows=tuple(rows),
        footers=tuple(
            {"line": item.line_number, "account": "unreadable", "type": item.reason}
            for item in batch.rejections
        ),
    )


def _result_table(result: ImportResult) -> Table:
    return Table(
        title=f"Imported {result.source}",
        columns=(
            Column.number("line", "Line"),
            Column("outcome", "Outcome"),
            Column("ticket", "Ticket"),
            Column("reason", "Why"),
        ),
        rows=tuple(record.describe() for record in result.records),
        footers=(
            {
                "outcome": "taken",
                "ticket": str(result.taken),
                "reason": str(result.bushels_taken),
            },
            {"outcome": "already seen", "ticket": str(result.duplicates)},
            {"outcome": "unreadable lines", "ticket": str(result.rejected_lines)},
        ),
    )
