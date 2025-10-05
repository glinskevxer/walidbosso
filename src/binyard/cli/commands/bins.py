"""``binyard bins``: add a bin, list them, look inside one."""

from __future__ import annotations

import argparse

from binyard.bushels import bushels
from binyard.cli.context import Context
from binyard.inventory.bin import Bin, BinKind
from binyard.percent import percent
from binyard.reporting.reports import bin_table
from binyard.reporting.table import Column, Table

__all__ = ["register", "handle_add", "handle_list", "handle_show"]


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("bins", help="bins and what is in them")
    inner = parser.add_subparsers(dest="bins_command", required=True)

    add = inner.add_parser("add", help="register a bin")
    add.add_argument("identifier")
    add.add_argument("--name", required=True)
    add.add_argument(
        "--kind", choices=[item.value for item in BinKind], default=BinKind.UPRIGHT.value
    )
    add.add_argument("--capacity", required=True, help="in bushels")
    add.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="CROP",
        help="restrict the bin to a crop, repeatable",
    )
    add.add_argument("--pile-allowance", default="0", help="percent, ground piles only")
    add.set_defaults(handler=handle_add)

    listing = inner.add_parser("list", help="every bin and what it holds")
    listing.set_defaults(handler=handle_list)

    show = inner.add_parser("show", help="one bin in detail")
    show.add_argument("identifier")
    show.set_defaults(handler=handle_show)


def handle_add(context: Context, arguments: argparse.Namespace) -> int:
    structure = Bin(
        identifier=arguments.identifier,
        name=arguments.name,
        kind=BinKind(arguments.kind),
        capacity=bushels(arguments.capacity),
        allowed_commodities=frozenset(item.upper() for item in arguments.only),
        pile_allowance=percent(arguments.pile_allowance),
    )
    work = context.work()
    yard = work.yard.load()
    work.yard.save(yard.with_bin(structure))
    work.commit()
    context.say(f"added bin {structure.identifier} at {structure.capacity}")
    context.emit(bin_table(work.yard.load()))
    return 0


def handle_list(context: Context, arguments: argparse.Namespace) -> int:
    context.emit(bin_table(context.work().yard.load()))
    return 0


def handle_show(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    state = work.yard.load().state_of(arguments.identifier)
    described = state.describe()
    contents = described["contents"]
    quality = contents["quality"] or {}
    rows = [
        {"field": "name", "value": described["name"]},
        {"field": "kind", "value": described["kind"]},
        {"field": "capacity", "value": described["capacity"]},
        {"field": "holding", "value": contents["commodity"]},
        {"field": "quantity", "value": contents["quantity"]},
        {"field": "headroom", "value": described["headroom"]},
    ]
    rows.extend(
        {"field": key, "value": value}
        for key, value in sorted(quality.items())
    )
    context.emit(
        Table(
            title=f"Bin {state.identifier}",
            columns=(Column("field", "Field"), Column("value", "Value")),
            rows=tuple(rows),
        )
    )
    return 0
