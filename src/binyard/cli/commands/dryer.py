"""``binyard dryer``: light a run, finish it, and see what it cost.

The three subcommands map onto the three moments the operator is at the panel.
``dryer status`` is the fourth, and it is the one that gets run most, because
at shift change somebody has to know what is on the dryer and how long it has
been there.

Finishing prints the variance without being asked. The number the operator
cares about is not how many bushels came back, it is how many came back
against what the formula said, and making them ask for it means nobody does.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal

from binyard.bushels import bushels
from binyard.cli.context import Context
from binyard.commodity import lookup
from binyard.drying.run import FuelUse
from binyard.money import Price
from binyard.percent import percent
from binyard.reporting.table import Column, Table
from binyard.services.drying import abandon_run, finish_run, start_run
from binyard.shrink import ShrinkPolicy

__all__ = ["register"]


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register this subcommand's parser and its handler."""
    parser = subparsers.add_parser("dryer", help="run the dryer")
    inner = parser.add_subparsers(dest="dryer_command", required=True)

    status = inner.add_parser("status", help="what is on the dryer now")
    status.set_defaults(handler=_status)

    start = inner.add_parser("start", help="draw wet grain and light it")
    start.add_argument("identifier")
    start.add_argument("--from", dest="source", required=True, metavar="BIN")
    start.add_argument("--to", dest="destination", required=True, metavar="BIN")
    start.add_argument("--quantity", required=True, help="bushels")
    start.set_defaults(handler=_start)

    finish = inner.add_parser("finish", help="weigh out and put the dry grain away")
    finish.add_argument("identifier")
    finish.add_argument("--bushels", required=True, help="what came back")
    finish.add_argument("--moisture", required=True, help="percent, coming out")
    finish.add_argument("--fuel", help="gallons burned")
    finish.add_argument("--fuel-price", default="0", help="dollars a gallon")
    finish.set_defaults(handler=_finish)

    abandon = inner.add_parser("abandon", help="stop a run early")
    abandon.add_argument("identifier")
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--bushels", required=True, help="what came back")
    abandon.add_argument("--moisture", required=True, help="percent, coming out")
    abandon.set_defaults(handler=_abandon)


def _status(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    runs = work.dryer.open_runs()
    rows = []
    for run in runs:
        described = run.describe()
        rows.append(
            {
                "identifier": described["identifier"],
                "from": described["from"],
                "to": described["to"],
                "wet_bushels": described["wet_bushels"],
                "wet_moisture": described["wet_moisture"],
                "started": run.started_at.isoformat(sep=" ", timespec="minutes"),
                "hours": f"{(datetime.now() - run.started_at).total_seconds() / 3600:.1f}",
            }
        )
    context.emit(
        Table(
            title="On the dryer",
            columns=(
                Column("identifier", "Run"),
                Column("from", "From"),
                Column("to", "To"),
                Column.number("wet_bushels", "Bushels"),
                Column.number("wet_moisture", "Wet"),
                Column("started", "Lit"),
                Column.number("hours", "Hours"),
            ),
            rows=tuple(rows),
        )
    )
    return 0


def _start(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    run = start_run(
        work,
        identifier=arguments.identifier,
        source_bin=arguments.source,
        destination_bin=arguments.destination,
        quantity=bushels(arguments.quantity),
        at=datetime.combine(context.on, datetime.now().time()),
        by="cli",
    )
    context.say(
        f"{run.identifier} lit with {run.wet_bushels} at {run.wet_moisture}"
        f" from {run.source_bin}"
    )
    return _status(context, arguments)


def _finish(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    run = work.dryer.get(arguments.identifier)
    standard = lookup(run.commodity_code)
    fuel = (
        FuelUse(
            gallons=Decimal(arguments.fuel),
            price_per_gallon=Price.of(arguments.fuel_price),
        )
        if arguments.fuel is not None
        else None
    )
    outcome = finish_run(
        work,
        identifier=arguments.identifier,
        dry_bushels=bushels(arguments.bushels),
        dry_moisture=percent(arguments.moisture),
        standard=standard,
        at=datetime.combine(context.on, datetime.now().time()),
        by="cli",
        fuel=fuel,
    )
    context.emit(_outcome_table(outcome, ShrinkPolicy.market_default(standard)))
    return 0


def _abandon(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    run = abandon_run(
        work,
        identifier=arguments.identifier,
        reason=arguments.reason,
        dry_bushels=bushels(arguments.bushels),
        dry_moisture=percent(arguments.moisture),
        at=datetime.combine(context.on, datetime.now().time()),
        by="cli",
    )
    context.warn(f"{run.identifier} abandoned: {run.note}")
    context.say(f"{run.dry_bushels} came back at {run.dry_moisture}")
    return 0


def _outcome_table(outcome, policy: ShrinkPolicy) -> Table:
    described = outcome.describe(policy)
    run = described["run"]
    variance = run["variance"] or {}
    rows = [
        {"field": "run", "value": run["identifier"]},
        {"field": "went in", "value": run["wet_bushels"]},
        {"field": "at", "value": run["wet_moisture"]},
        {"field": "came back", "value": run["dry_bushels"]},
        {"field": "at", "value": run["dry_moisture"]},
        {"field": "points out", "value": run["points_removed"]},
        {"field": "dryer kept", "value": described["posted_loss"]},
        {"field": "the formula allowed", "value": variance.get("expected_out")},
        {"field": "difference", "value": variance.get("difference")},
    ]
    if run["fuel_gallons"] is not None:
        rows.append({"field": "fuel", "value": f"{run['fuel_gallons']} gal"})
        rows.append({"field": "fuel cost", "value": run["fuel_cost"]})
        rows.append({"field": "gal per point bushel", "value": run["gallons_per_point_bushel"]})
    return Table(
        title=f"Run {run['identifier']}",
        columns=(Column("field", "Field"), Column("value", "Value")),
        rows=tuple(rows),
    )
