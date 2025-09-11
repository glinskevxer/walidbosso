"""``binyard receive``: take a load over the scale from the command line.

Every factor is a flag, which reads badly on paper and well in practice,
because the scale house runs this from a shell script fed by the moisture meter
and the ticket printer. The flags are named after what is written on the sample
envelope, not after the attribute names in the package.

The command prints the ticket, the discounts and where the bushels went, in
that order, because that is the order the person at the counter is asked about
them.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal

from binyard import commodity
from binyard.cli.context import Context
from binyard.contracts.application import Priority
from binyard.discounts.posted import default_book
from binyard.grading.factors import FactorSample, Odor
from binyard.identity import Sequence, crop_year_of
from binyard.percent import percent
from binyard.reporting.reports import discount_table
from binyard.reporting.table import Column, Table
from binyard.services.receiving import ReceiptRequest, receive_load
from binyard.tickets.weights import ScaleWeights, TareSource
from binyard.units import parse_weight

__all__ = ["register", "handle"]


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser("receive", help="take one load in")
    parser.add_argument("account", help="the grower or account the load is for")
    parser.add_argument("--crop", required=True, help="corn, beans, hrw and so on")
    parser.add_argument("--bin", dest="bin_identifier", required=True)
    parser.add_argument("--gross", required=True, help="scale gross, pounds")
    parser.add_argument("--tare", required=True, help="scale tare, pounds")
    parser.add_argument(
        "--tare-source",
        choices=[item.value for item in TareSource],
        default=TareSource.WEIGHED.value,
    )
    parser.add_argument("--moisture", required=True, help="percent")
    parser.add_argument("--test-weight", required=True, help="pounds per bushel")
    parser.add_argument("--damage", default="0", help="total damage, percent")
    parser.add_argument("--heat", default="0", help="heat damage, percent")
    parser.add_argument("--foreign", default="0", help="foreign material, percent")
    parser.add_argument("--broken", default="0", help="broken or splits, percent")
    parser.add_argument("--dockage", default="0", help="percent")
    parser.add_argument("--shrunken", default="0", help="shrunken and broken, percent")
    parser.add_argument("--other-classes", default="0", help="percent")
    parser.add_argument(
        "--odor", choices=[item.value for item in Odor], default=Odor.NONE.value
    )
    parser.add_argument("--infested", action="store_true")
    parser.add_argument("--hauler")
    parser.add_argument(
        "--priority",
        choices=[item.value for item in Priority],
        default=Priority.EARLIEST_DEADLINE.value,
        help="which contract gets filled first",
    )
    parser.add_argument(
        "--obligation", help="top this storage obligation up instead of opening one"
    )
    parser.set_defaults(handler=handle)


def handle(context: Context, arguments: argparse.Namespace) -> int:
    standard = commodity.lookup(arguments.crop)
    weights = ScaleWeights(
        gross=parse_weight(arguments.gross),
        tare=parse_weight(arguments.tare),
        tare_source=TareSource(arguments.tare_source),
    )
    sample = FactorSample(
        moisture=percent(arguments.moisture),
        test_weight=Decimal(arguments.test_weight),
        total_damage=percent(arguments.damage),
        heat_damage=percent(arguments.heat),
        foreign_material=percent(arguments.foreign),
        broken=percent(arguments.broken),
        dockage=percent(arguments.dockage),
        shrunken_broken=percent(arguments.shrunken),
        other_classes=percent(arguments.other_classes),
        odor=Odor(arguments.odor),
        infested=arguments.infested,
    )
    work = context.work()
    sequence = Sequence(context.settings.location, crop_year_of(context.on))
    receipt = receive_load(
        work,
        sequence,
        ReceiptRequest(
            account=arguments.account,
            standard=standard,
            weights=weights,
            sample=sample,
            bin_identifier=arguments.bin_identifier,
            weighed_on=context.on,
            at=datetime.combine(context.on, datetime.now().time()),
            by="cli",
            hauler=arguments.hauler,
            priority=Priority(arguments.priority),
            obligation_identifier=arguments.obligation,
        ),
        book=default_book(),
    )

    context.emit(_ticket_table(receipt))
    if receipt.assessment.lines:
        context.emit(discount_table(receipt.assessment))
    context.emit(_application_table(receipt))
    if receipt.ticket.needs_review:
        context.warn(
            "this ticket needs review: "
            + "; ".join(receipt.ticket.weights.review_reasons())
        )
    return 0


def _ticket_table(receipt) -> Table:
    ticket = receipt.ticket
    assert ticket.grade is not None and ticket.shrink is not None
    rows = [
        {"field": "ticket", "value": str(ticket.number)},
        {"field": "account", "value": ticket.account},
        {"field": "crop", "value": ticket.commodity_code},
        {"field": "net weight", "value": str(ticket.weights.net)},
        {"field": "gross bushels", "value": str(ticket.gross_bushels)},
        {"field": "water shrink", "value": str(ticket.shrink.water_shrink)},
        {"field": "handling shrink", "value": str(ticket.shrink.handling_shrink)},
        {"field": "net bushels", "value": str(ticket.net_bushels)},
        {"field": "grade", "value": ticket.grade.label},
    ]
    for item in ticket.grade.limiting:
        rows.append({"field": "held back by", "value": item.describe()})
    return Table(
        title=f"Ticket {ticket.number}",
        columns=(Column("field", "Field"), Column("value", "Value")),
        rows=tuple(rows),
    )


def _application_table(receipt) -> Table:
    rows = [
        {
            "target": allocation.contract_identifier or "storage",
            "quantity": str(allocation.quantity),
            "reason": allocation.reason,
        }
        for allocation in receipt.application.allocations
    ]
    if receipt.obligation is not None:
        rows.append(
            {
                "target": receipt.obligation.identifier,
                "quantity": str(receipt.obligation.balance),
                "reason": f"{receipt.obligation.kind.value} balance after this load",
            }
        )
    return Table(
        title="Where the bushels went",
        columns=(
            Column("target", "Applied to"),
            Column.number("quantity", "Bushels"),
            Column("reason", "Why"),
        ),
        rows=tuple(rows),
    )
