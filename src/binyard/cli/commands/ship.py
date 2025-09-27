"""``binyard ship``: open an order, plan it, and put loads on the road.

Shipping from a command line is three separate things and they are three
subcommands, because the middle one is run over and over across days:

``ship order`` writes the order down. ``ship plan`` shows the blend and the
split without moving anything, and is safe to run twenty times. ``ship load``
actually draws a carrier's worth out of the named bins.

``ship load`` takes its draw list explicitly rather than recomputing the blend,
and that is deliberate. The blend that was planned on Monday is not the blend
the yard supports on Thursday, and quietly re-planning under the operator would
mean the paperwork in their hand and the grain in the truck disagree.

Orders are held in this module for now, which means they do not survive the
process. That is fine for a single shell session and wrong for anything else,
and it is the next thing to fix here.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from binyard.blend.planner import Objective
from binyard.bushels import bushels
from binyard.cli.commands.blend import build_spec
from binyard.cli.context import Context
from binyard.commodity import lookup
from binyard.errors import NotFound, ValidationError
from binyard.identity import Sequence, crop_year_of
from binyard.loadout.carrier import AxleConfiguration, Carrier, CarrierKind
from binyard.loadout.order import LoadoutOrder
from binyard.reporting.reports import blend_table
from binyard.reporting.table import Column, Table
from binyard.services.shipping import ShipmentRequest, plan_shipment, ship_next_load
from binyard.units import parse_weight

__all__ = ["register"]

_ORDERS: dict[str, LoadoutOrder] = {}


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("ship", help="load out against an order")
    inner = parser.add_subparsers(dest="ship_command", required=True)

    order = inner.add_parser("order", help="write an order down")
    order.add_argument("identifier")
    order.add_argument("--destination", required=True)
    order.add_argument("--crop", required=True)
    order.add_argument("--quantity", required=True, help="bushels")
    order.add_argument("--from", dest="ship_from", required=True, help="YYYY-MM-DD")
    order.add_argument("--to", dest="ship_to", required=True, help="YYYY-MM-DD")
    order.set_defaults(handler=_order)

    plan = inner.add_parser("plan", help="show the blend and the split, writing nothing")
    plan.add_argument("identifier")
    plan.add_argument("--crop", required=True)
    plan.add_argument("--quantity", required=True)
    plan.add_argument("--grade", type=int)
    plan.add_argument("--max", dest="maximums", action="append", default=[])
    plan.add_argument("--min", dest="minimums", action="append", default=[])
    plan.add_argument("--reject", dest="rejects", action="append", default=[])
    plan.add_argument(
        "--carrier",
        dest="carriers",
        action="append",
        default=[],
        metavar="ID:EMPTY_LB",
        help="a carrier and its empty weight, repeatable",
    )
    plan.add_argument(
        "--objective",
        choices=[item.value for item in Objective if item is not Objective.EXPLICIT_COST],
        default=Objective.SHIP_WORST_FIRST.value,
    )
    plan.set_defaults(handler=_plan)

    load = inner.add_parser("load", help="draw one carrier's worth and ticket it")
    load.add_argument("identifier")
    load.add_argument("--crop", required=True)
    load.add_argument("--destination", required=True)
    load.add_argument("--carrier", required=True, metavar="ID:EMPTY_LB")
    load.add_argument(
        "--draw",
        dest="draws",
        action="append",
        required=True,
        metavar="BIN=BUSHELS",
        help="where the load comes from, repeatable",
    )
    load.set_defaults(handler=_load)


def _order(context: Context, arguments: argparse.Namespace) -> int:
    from datetime import date

    standard = lookup(arguments.crop)
    order = LoadoutOrder(
        identifier=arguments.identifier,
        destination=arguments.destination,
        commodity_code=standard.code,
        quantity=bushels(arguments.quantity),
        ship_from=date.fromisoformat(arguments.ship_from),
        ship_to=date.fromisoformat(arguments.ship_to),
    )
    _ORDERS[order.identifier] = order
    context.emit(_order_table(order))
    return 0


def _plan(context: Context, arguments: argparse.Namespace) -> int:
    standard = lookup(arguments.crop)
    order = _ORDERS.get(arguments.identifier)
    if order is None:
        raise NotFound("order", arguments.identifier)
    spec = build_spec(arguments, standard.code)
    shipment = plan_shipment(
        context.work(),
        ShipmentRequest(
            order=order,
            spec=spec,
            standard=standard,
            carriers=tuple(_carrier(text) for text in arguments.carriers),
            at=datetime.combine(context.on, datetime.min.time()),
            by="cli",
            objective=Objective(arguments.objective),
        ),
    )
    context.emit(blend_table(shipment.blend))
    context.emit(
        Table(
            title="Loads",
            columns=(Column("carrier", "Carrier"), Column.number("quantity", "Bushels")),
            rows=tuple(
                {"carrier": carrier, "quantity": str(quantity)}
                for carrier, quantity in shipment.loads
            ),
        )
    )
    return 0


def _load(context: Context, arguments: argparse.Namespace) -> int:
    standard = lookup(arguments.crop)
    order = _ORDERS.get(arguments.identifier)
    if order is None:
        raise NotFound("order", arguments.identifier)
    draws = tuple(_draw(text) for text in arguments.draws)
    quantity = bushels(0)
    for _, amount in draws:
        quantity = quantity + amount
    carrier = _carrier(arguments.carrier)
    work = context.work()
    moved, ticket = ship_next_load(
        work,
        Sequence(context.settings.location, crop_year_of(context.on)),
        order=order,
        standard=standard,
        carrier=carrier,
        quantity=quantity,
        draws=draws,
        on=context.on,
        at=datetime.combine(context.on, datetime.now().time()),
        by="cli",
    )
    _ORDERS[order.identifier] = moved
    context.emit(_order_table(moved))
    context.say(f"ticket {ticket.number} on {carrier.identifier}")
    return 0


def _order_table(order: LoadoutOrder) -> Table:
    described = order.describe()
    rows = [
        {"field": key, "value": described[key]}
        for key in ("identifier", "destination", "commodity", "quantity", "shipped", "remaining", "status")
    ]
    return Table(
        title=f"Order {order.identifier}",
        columns=(Column("field", "Field"), Column("value", "Value")),
        rows=tuple(rows),
    )


def _carrier(text: str) -> Carrier:
    if ":" not in text:
        raise ValidationError("a carrier is written ID:EMPTY_LB", got=text)
    identifier, _, empty = text.partition(":")
    return Carrier(
        identifier=identifier.strip(),
        kind=CarrierKind.TRUCK,
        empty_weight=parse_weight(empty.strip()),
        axles=AxleConfiguration.TRACTOR_SEMI_5,
    )


def _draw(text: str) -> tuple[str, object]:
    if "=" not in text:
        raise ValidationError("a draw is written BIN=BUSHELS", got=text)
    identifier, _, amount = text.partition("=")
    return identifier.strip(), bushels(amount.strip())
