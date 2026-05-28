"""``binyard report``: the reports somebody actually asks for by name.

Each one is a subcommand rather than a flag on a single ``report`` command,
because they take different arguments and a flag soup of mutually exclusive
options is how a report gets run with the wrong date.

``--format csv`` on any of them writes something a spreadsheet opens, and
``--format json`` writes something another program reads. The report code does
not know which, which is the point of the table type.

``--out`` sends it to a file rather than the terminal, written whole or not at
all, and refusing to replace one that is already there unless ``--overwrite``
says so. A month end statement is written once.
"""

from __future__ import annotations

import argparse

from binyard.cli.context import Context
from binyard.reporting.reports import obligation_table, position_table
from binyard.services.reporting import (
    day_sheet,
    examiner_report,
    position_on,
    review_queue,
)

__all__ = ["register"]


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("report", help="the standing reports")
    parser.add_argument("--out", help="write to this file instead of the terminal")
    parser.add_argument(
        "--overwrite", action="store_true", help="replace an existing --out file"
    )
    inner = parser.add_subparsers(dest="report_command", required=True)

    position = inner.add_parser("position", help="inventory against obligations")
    position.set_defaults(handler=_position)

    examiner = inner.add_parser(
        "examiner", help="the two column statement a warehouse examiner asks for"
    )
    examiner.set_defaults(handler=_examiner)

    day = inner.add_parser("day", help="the close of business sheet")
    day.add_argument(
        "--only",
        choices=["tickets", "bins", "obligations", "position"],
        help="print one of the four tables instead of all of them",
    )
    day.set_defaults(handler=_day)

    review = inner.add_parser("review", help="tickets somebody has to look at")
    review.set_defaults(handler=_review)

    obligations = inner.add_parser("obligations", help="what the house owes")
    obligations.add_argument("--all", action="store_true", help="include the closed ones")
    obligations.set_defaults(handler=_obligations)


def _position(context: Context, arguments: argparse.Namespace) -> int:
    report = position_on(context.work(), on=context.on, location=context.settings.location)
    context.emit_or_write(position_table(report), arguments.out, overwrite=arguments.overwrite)
    for short in report.shorts:
        context.warn(f"{short.commodity_code} is short by {abs(short.house_position)}")
    return 1 if report.shorts else 0


def _examiner(context: Context, arguments: argparse.Namespace) -> int:
    context.emit_or_write(
        examiner_report(context.work(), on=context.on, location=context.settings.location),
        arguments.out,
        overwrite=arguments.overwrite,
    )
    return 0


def _day(context: Context, arguments: argparse.Namespace) -> int:
    sheet = day_sheet(context.work(), on=context.on, location=context.settings.location)
    chosen = {
        "tickets": sheet.tickets,
        "bins": sheet.bins,
        "obligations": sheet.obligations,
        "position": sheet.position,
    }
    if arguments.only:
        context.emit_or_write(
            chosen[arguments.only], arguments.out, overwrite=arguments.overwrite
        )
        return 0
    for table in sheet.tables:
        context.emit(table)
        context.say("")
    context.say(f"received {sheet.received}, shipped {sheet.shipped}")
    return 0


def _review(context: Context, arguments: argparse.Namespace) -> int:
    queue = review_queue(context.work())
    context.emit_or_write(queue, arguments.out, overwrite=arguments.overwrite)
    return 1 if queue.rows else 0


def _obligations(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    held = list(
        work.obligations.as_of(context.on) if arguments.all else work.obligations.all_open()
    )
    context.emit_or_write(
        obligation_table(held, include_closed=arguments.all),
        arguments.out,
        overwrite=arguments.overwrite,
    )
    return 0
