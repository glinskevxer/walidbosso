"""Turning applied tickets into a cheque.

A settlement covers one account, one crop and a list of tickets, and every
ticket on it has to be applied and not yet settled. That check is the whole
reason this is a service rather than a function on the statement builder: it
needs the store to know what has already been paid.

Two rules that look like bookkeeping and are actually policy:

**Storage is charged once, on the obligation, not on the ticket.** A grower
with four tickets in one obligation gets one storage line, computed off the
obligation's balance history. Charging per ticket double counts the days a
later ticket was not yet in the bin.

**Drying is charged on the ticket, not the obligation.** It is a service
performed on that load, on the day it arrived, and it does not care what
happened to the bushels afterwards.

The statement is written once and never updated. A wrong settlement is
corrected with a second one carrying the difference, for the same reason a
wrong ticket is voided rather than edited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from binyard.bushels import Bushels
from binyard.discounts.apply import assess
from binyard.discounts.schedule import ScheduleBook
from binyard.errors import TicketStateError, ValidationError
from binyard.identity import TicketNumber
from binyard.money import Money, Price
from binyard.obligations.storage import StorageTerms, accrue
from binyard.percent import Percent
from binyard.settlement.statement import ChargeKind, Checkoff, Settlement, SettlementBuilder
from binyard.store.protocol import UnitOfWork
from binyard.tickets.ticket import TicketStatus

__all__ = ["SettlementRequest", "settle_tickets"]


@dataclass(frozen=True, slots=True)
class SettlementRequest:
    """One statement's worth of work."""

    number: str
    account: str
    commodity_code: str
    on: date
    ticket_numbers: tuple[TicketNumber, ...]
    price: Price
    at: datetime
    by: str
    obligation_identifiers: tuple[str, ...] = ()
    storage_terms: StorageTerms | None = None
    drying_rate_per_point: Price = Price.zero()
    checkoffs: tuple[Checkoff, ...] = ()
    advances: Money = Money.zero()
    hauling: Money = Money.zero()


def settle_tickets(
    work: UnitOfWork, request: SettlementRequest, *, book: ScheduleBook
) -> Settlement:
    """Build, store and post one settlement across a list of tickets."""
    if not request.ticket_numbers:
        raise ValidationError("a settlement needs at least one ticket", number=request.number)

    tickets = [work.tickets.get(number) for number in request.ticket_numbers]
    quantity = Bushels.zero()
    drying_points = Percent.zero()

    for ticket in tickets:
        if ticket.account != request.account:
            raise ValidationError(
                "that ticket is on another account",
                ticket=str(ticket.number),
                account=ticket.account,
            )
        if ticket.commodity_code != request.commodity_code:
            raise ValidationError(
                "that ticket is for another crop", ticket=str(ticket.number)
            )
        if ticket.status is not TicketStatus.APPLIED:
            raise TicketStateError(
                "only an applied ticket can be settled",
                ticket=str(ticket.number),
                status=ticket.status.value,
            )
        net = ticket.net_bushels
        assert net is not None and ticket.shrink is not None
        quantity = quantity + net
        drying_points = drying_points + ticket.shrink.points_removed.scaled_by(
            net.share_of(net)
        )

    if not quantity.is_positive():
        raise ValidationError("those tickets settle to nothing", number=request.number)

    builder = SettlementBuilder(
        request.number, request.account, request.commodity_code, request.on, quantity
    )
    builder.grain(request.price)

    for ticket in tickets:
        assert ticket.grade is not None
        net = ticket.net_bushels
        assert net is not None
        schedule = book.in_effect(request.commodity_code, ticket.weighed_on)
        found = assess(ticket.grade, net, schedule)
        for line in found.lines:
            builder.charge(
                ChargeKind.DISCOUNT,
                f"{line.source} on {ticket.number}",
                line.amount,
                quantity=net,
                rate=line.rate,
            )

    if request.drying_rate_per_point != Price.zero():
        for ticket in tickets:
            assert ticket.shrink is not None
            if ticket.shrink.points_removed.is_zero():
                continue
            net = ticket.net_bushels
            assert net is not None
            per_bushel = Price(
                int(request.drying_rate_per_point.ticks * ticket.shrink.points_removed.points)
            )
            builder.charge(
                ChargeKind.DRYING,
                f"drying {ticket.shrink.points_removed} on {ticket.number}",
                per_bushel.extend(net),
                quantity=net,
                rate=per_bushel,
            )

    if request.storage_terms is not None:
        for identifier in request.obligation_identifiers:
            obligation = work.obligations.get(identifier)
            if obligation.account != request.account:
                raise ValidationError(
                    "that obligation belongs to another account", obligation=identifier
                )
            found = accrue(obligation, request.storage_terms, request.on)
            if found.total.is_zero():
                continue
            builder.charge(
                ChargeKind.STORAGE,
                f"storage on {identifier} through {request.on.isoformat()}",
                found.total,
            )

    for checkoff in request.checkoffs:
        builder.checkoff(checkoff)

    if not request.hauling.is_zero():
        builder.charge(ChargeKind.HAULING, "hauling", request.hauling)
    if not request.advances.is_zero():
        builder.charge(ChargeKind.ADVANCE, "advances already paid", request.advances)

    settlement = builder.build()
    work.settlements.add(settlement)
    for ticket in tickets:
        work.tickets.replace(
            ticket.settled(at=request.at, by=request.by, note=request.number)
        )
    work.commit()
    return settlement
