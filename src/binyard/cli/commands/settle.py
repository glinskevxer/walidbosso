"""``binyard settle``: turn applied tickets into a statement.

Two subcommands. ``settle preview`` builds the statement and prints it without
writing, which is what the counter runs while a grower is standing there.
``settle post`` writes it and marks the tickets settled.

They take the same arguments on purpose. A preview that took different
arguments from the thing it previews is not a preview.
"""

from __future__ import annotations

import argparse
from datetime import datetime

from binyard.cli.context import Context
from binyard.discounts.posted import default_book
from binyard.errors import ValidationError
from binyard.identity import TicketNumber
from binyard.money import Money, Price
from binyard.reporting.reports import settlement_table
from binyard.services.settling import SettlementRequest, settle_tickets
from binyard.settlement.statement import Checkoff
from binyard.store.memory import MemoryUnitOfWork

__all__ = ["register"]


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("number", help="the settlement number to write")
    parser.add_argument("--account", required=True)
    parser.add_argument("--crop", required=True)
    parser.add_argument("--price", required=True, help="dollars a bushel")
    parser.add_argument(
        "--ticket",
        dest="tickets",
        action="append",
        required=True,
        metavar="NUMBER",
        help="a ticket to settle, repeatable",
    )
    parser.add_argument(
        "--obligation",
        dest="obligations",
        action="append",
        default=[],
        metavar="ID",
        help="accrue storage on this obligation, repeatable",
    )
    parser.add_argument("--drying-rate", default=None, help="cents a point a bushel")
    parser.add_argument(
        "--checkoff",
        dest="checkoffs",
        action="append",
        default=[],
        metavar="NAME=RATE",
        help="a per bushel checkoff, repeatable",
    )
    parser.add_argument("--advance", default="0", help="dollars already paid out")
    parser.add_argument("--hauling", default="0", help="dollars of hauling to deduct")


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register this subcommand's parser and its handler."""
    parser = subparsers.add_parser("settle", help="settle applied tickets")
    inner = parser.add_subparsers(dest="settle_command", required=True)

    preview = inner.add_parser("preview", help="build the statement without writing")
    _common(preview)
    preview.set_defaults(handler=_preview)

    post = inner.add_parser("post", help="write the statement and settle the tickets")
    _common(post)
    post.set_defaults(handler=_post)


def _request(context: Context, arguments: argparse.Namespace) -> SettlementRequest:
    from binyard.commodity import lookup

    standard = lookup(arguments.crop)
    terms = context.settings.storage_terms if arguments.obligations else None
    drying = (
        Price.from_cents(arguments.drying_rate)
        if arguments.drying_rate is not None
        else context.settings.drying_rate_per_point
    )
    return SettlementRequest(
        number=arguments.number,
        account=arguments.account,
        commodity_code=standard.code,
        on=context.on,
        ticket_numbers=tuple(TicketNumber.parse(item) for item in arguments.tickets),
        price=Price.of(arguments.price),
        at=datetime.combine(context.on, datetime.now().time()),
        by="cli",
        obligation_identifiers=tuple(arguments.obligations),
        storage_terms=terms,
        drying_rate_per_point=drying,
        checkoffs=tuple(_checkoff(item) for item in arguments.checkoffs),
        advances=Money.of(arguments.advance),
        hauling=Money.of(arguments.hauling),
    )


def _preview(context: Context, arguments: argparse.Namespace) -> int:
    """Build the statement against a scratch copy so nothing is written.

    The tickets and obligations are copied into an in-memory unit of work and
    the real one is never touched. That is heavier than passing a flag down
    into the service, and it is worth it: a preview that shares a code path
    with a write is one boolean away from writing.
    """
    live = context.work()
    request = _request(context, arguments)
    scratch = MemoryUnitOfWork(live.yard.load())
    for number in request.ticket_numbers:
        scratch.tickets.add(live.tickets.get(number))
    for identifier in request.obligation_identifiers:
        scratch.obligations.add(live.obligations.get(identifier))
    scratch.commit()

    settlement = settle_tickets(scratch, request, book=default_book())
    context.emit(settlement_table(settlement))
    context.say(f"net {settlement.net} on {settlement.quantity}")
    return 0


def _post(context: Context, arguments: argparse.Namespace) -> int:
    work = context.work()
    settlement = settle_tickets(work, _request(context, arguments), book=default_book())
    context.emit(settlement_table(settlement))
    context.say(f"settlement {settlement.number} written, net {settlement.net}")
    return 0


def _checkoff(text: str) -> Checkoff:
    if "=" not in text:
        raise ValidationError("a checkoff is written NAME=RATE", got=text)
    name, _, rate = text.partition("=")
    return Checkoff(name.strip(), per_bushel=Price.from_cents(rate.strip()))
