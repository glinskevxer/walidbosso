"""Reading the stores and handing back reports.

Everything here is a query. Nothing writes, nothing commits, and nothing takes
a sequence. That separation is worth keeping strictly: a report that quietly
posted a shrink adjustment while it ran would be almost impossible to find
later, because nobody suspects the report.

The day sheet is the one the manager reads. It is deliberately several small
tables rather than one wide one, because it gets printed on a stub printer at
close of business and a hundred and twenty column table comes out shredded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.identity import LocationCode
from binyard.position.report import PositionReport, build_position
from binyard.reporting.reports import (
    bin_table,
    examiner_statement,
    obligation_table,
    position_table,
)
from binyard.reporting.table import Column, Table
from binyard.store.protocol import UnitOfWork
from binyard.tickets.ticket import ScaleTicket, TicketStatus

__all__ = ["DaySheet", "day_sheet", "examiner_report", "position_on", "review_queue"]


@dataclass(frozen=True, slots=True)
class DaySheet:
    """Everything the manager reads at close of business."""

    on: date
    location: str
    tickets: Table
    bins: Table
    obligations: Table
    position: Table
    received: Bushels
    shipped: Bushels

    @property
    def tables(self) -> tuple[Table, ...]:
        return (self.tickets, self.bins, self.obligations, self.position)

    def describe(self) -> dict[str, object]:
        return {
            "on": self.on.isoformat(),
            "location": self.location,
            "received": str(self.received),
            "shipped": str(self.shipped),
            "tables": [table.title for table in self.tables],
        }


def position_on(work: UnitOfWork, *, on: date, location: LocationCode | str) -> PositionReport:
    """The position as it stood at the end of ``on``."""
    return build_position(
        on=on,
        location=str(location),
        yard=work.yard.load(),
        obligations=list(work.obligations.as_of(on)),
        as_of=datetime.combine(on, datetime.max.time()),
    )


def examiner_report(work: UnitOfWork, *, on: date, location: LocationCode | str) -> Table:
    """The two column statement an examiner asks for."""
    return examiner_statement(position_on(work, on=on, location=location))


def day_sheet(work: UnitOfWork, *, on: date, location: LocationCode | str) -> DaySheet:
    """The close of business sheet, as four small tables."""
    tickets = work.tickets.weighed_on(on)
    yard = work.yard.load()
    report = position_on(work, on=on, location=location)

    received = Bushels.zero()
    shipped = Bushels.zero()
    for ticket in tickets:
        net = ticket.net_bushels
        if net is None or ticket.status is TicketStatus.VOID:
            continue
        if ticket.kind.value == "outbound":
            shipped = shipped + net
        else:
            received = received + net

    return DaySheet(
        on=on,
        location=str(location),
        tickets=_ticket_table(tickets, on),
        bins=bin_table(yard),
        obligations=obligation_table(list(work.obligations.all_open())),
        position=position_table(report),
        received=received,
        shipped=shipped,
    )


def review_queue(work: UnitOfWork) -> Table:
    """Open tickets somebody has to look at before they can be settled."""
    columns = (
        Column("number", "Ticket"),
        Column("account", "Account"),
        Column("commodity", "Crop"),
        Column("status", "Status"),
        Column("reason", "Why"),
    )
    rows = []
    for ticket in work.tickets.open_tickets():
        if not ticket.needs_review:
            continue
        rows.append(
            {
                "number": str(ticket.number),
                "account": ticket.account,
                "commodity": ticket.commodity_code,
                "status": ticket.status.value,
                "reason": "; ".join(ticket.review_reasons()),
            }
        )
    return Table(title="Tickets needing review", columns=columns, rows=tuple(rows))


def _ticket_table(tickets: tuple[ScaleTicket, ...], on: date) -> Table:
    if not isinstance(on, date):  # pragma: no cover - guard against a stray datetime
        raise ValidationError("a day sheet is for one date")
    columns = (
        Column("number", "Ticket"),
        Column("account", "Account"),
        Column("commodity", "Crop"),
        Column.number("net", "Net"),
        Column("grade", "Grade"),
        Column.number("moisture", "Moisture"),
        Column("status", "Status"),
    )
    rows = []
    total = Bushels.zero()
    for ticket in tickets:
        net = ticket.net_bushels
        if net is not None and ticket.status is not TicketStatus.VOID:
            total = total + net
        rows.append(
            {
                "number": str(ticket.number),
                "account": ticket.account,
                "commodity": ticket.commodity_code,
                "net": None if net is None else str(net),
                "grade": None if ticket.grade is None else ticket.grade.label,
                "moisture": None if ticket.sample is None else str(ticket.sample.moisture),
                "status": ticket.status.value,
            }
        )
    return Table(
        title=f"Tickets on {on.isoformat()}",
        columns=columns,
        rows=tuple(rows),
        footers=({"account": "net received", "net": str(total)},),
    )
