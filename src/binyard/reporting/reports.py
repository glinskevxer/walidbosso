"""The reports the house actually prints, built as tables.

Each builder takes finished domain objects and returns a :class:`Table`. None
of them format numbers themselves; the domain types already know how they print
and the builders only choose which of those strings to show and in what order.

The examiner statement is the one with an external audience. A state warehouse
examiner arrives, asks for inventory against obligations on a date, and expects
the two columns and the difference. Everything else on the page is for us.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from binyard.blend.planner import BlendPlan
from binyard.discounts.apply import Assessment
from binyard.inventory.yard import Yard
from binyard.obligations.obligation import ObligationStatus, StorageObligation
from binyard.position.report import PositionReport
from binyard.reporting.table import Column, Table
from binyard.settlement.statement import Settlement

__all__ = [
    "bin_table",
    "blend_table",
    "discount_table",
    "examiner_statement",
    "obligation_table",
    "position_table",
    "settlement_table",
]


def position_table(report: PositionReport) -> Table:
    """Every crop, with what is owed out and where the house stands."""
    columns = (
        Column("commodity", "Crop"),
        Column.number("on_hand", "On hand"),
        Column.number("pile_allowance", "Pile allow"),
        Column.number("open_storage", "Open stor"),
        Column.number("warehouse_receipts", "Receipts"),
        Column.number("company_owned", "Company"),
        Column.number("unpaid", "Unpaid"),
        Column.number("house_position", "Position"),
        Column("short", "Short"),
    )
    rows = [position.describe() for position in report.positions]
    return Table(
        title=f"Position at {report.location} on {report.on.isoformat()}",
        columns=columns,
        rows=tuple(rows),
    )


def examiner_statement(report: PositionReport) -> Table:
    """The two columns an examiner asks for, and the difference between them."""
    columns = (
        Column("commodity", "Crop"),
        Column.number("measurable", "Measurable inventory"),
        Column.number("owed_out", "Obligations"),
        Column.number("house_position", "Difference"),
        Column("coverage", "Coverage"),
    )
    rows = []
    for position in report.positions:
        described: dict[str, Any] = position.describe()
        rows.append(
            {
                "commodity": described["commodity"],
                "measurable": described["measurable"],
                "owed_out": described["owed_out"],
                "house_position": described["house_position"],
                "coverage": described["coverage"],
            }
        )
    return Table(
        title=(f"Warehouse examiner statement, {report.location}, {report.on.isoformat()}"),
        columns=columns,
        rows=tuple(rows),
    )


def bin_table(yard: Yard) -> Table:
    """Every bin, what is in it and how much room is left."""
    columns = (
        Column("identifier", "Bin"),
        Column("name", "Name"),
        Column("kind", "Kind"),
        Column("commodity", "Crop"),
        Column.number("quantity", "Bushels"),
        Column.number("capacity", "Capacity"),
        Column.number("headroom", "Headroom"),
        Column.number("moisture", "Moisture"),
    )
    rows = []
    for state in yard.states():
        described: dict[str, Any] = state.describe()
        contents = described["contents"]
        quality = contents["quality"]
        rows.append(
            {
                "identifier": described["identifier"],
                "name": described["name"],
                "kind": described["kind"],
                "commodity": contents["commodity"],
                "quantity": contents["quantity"],
                "capacity": described["capacity"],
                "headroom": described["headroom"],
                "moisture": None if quality is None else quality["moisture"],
            }
        )
    return Table(title="Bins", columns=columns, rows=tuple(rows))


def obligation_table(
    obligations: list[StorageObligation], *, include_closed: bool = False
) -> Table:
    """Open obligations, oldest first, which is the order they get released in."""
    columns = (
        Column("identifier", "Obligation"),
        Column("account", "Account"),
        Column("commodity", "Crop"),
        Column("kind", "Kind"),
        Column.number("quantity", "Written for"),
        Column.number("balance", "Balance"),
        Column("opened_on", "Opened"),
        Column("status", "Status"),
    )
    kept = [
        obligation
        for obligation in obligations
        if include_closed or obligation.status is ObligationStatus.OPEN
    ]
    kept.sort(key=lambda obligation: (obligation.opened_on, obligation.identifier))
    rows = []
    for obligation in kept:
        described: dict[str, Any] = obligation.describe()
        rows.append({key: described[key] for key in dict.fromkeys(c.key for c in columns)})
    return Table(title="Storage obligations", columns=columns, rows=tuple(rows))


def settlement_table(settlement: Settlement) -> Table:
    """One statement, line by line, with the net underneath."""
    columns = (
        Column("kind", "Kind"),
        Column("description", "Description"),
        Column.number("quantity", "Bushels"),
        Column.number("rate", "Rate"),
        Column.number("amount", "Amount"),
    )
    rows = [line.describe() for line in settlement.lines]
    footers = (
        {"description": "gross", "amount": str(settlement.gross)},
        {"description": "deductions", "amount": str(settlement.deductions)},
        {"description": "net", "amount": str(settlement.net)},
    )
    return Table(
        title=f"Settlement {settlement.number} for {settlement.account}",
        columns=columns,
        rows=tuple(rows),
        footers=footers,
    )


def blend_table(plan: BlendPlan) -> Table:
    """The draw list, with the resulting quality underneath it."""
    columns = (
        Column("bin", "Bin"),
        Column.number("quantity", "Draw"),
    )
    described: dict[str, Any] = plan.describe()
    footers: list[dict[str, Any]] = [{"bin": "total", "quantity": described["quantity"]}]
    for factor, margin in described["margins"].items():
        footers.append({"bin": f"{factor} margin", "quantity": margin})
    return Table(
        title=f"Blend plan for {described['quantity']} of {plan.spec.commodity_code}",
        columns=columns,
        rows=tuple(described["draws"]),
        footers=tuple(footers),
    )


def discount_table(assessment: Assessment) -> Table:
    """What the board took off one load."""
    columns = (
        Column("source", "Factor"),
        Column("reading", "Reading"),
        Column.number("rate", "Rate"),
        Column.number("amount", "Amount"),
        Column("note", "Band"),
    )
    described: dict[str, Any] = assessment.describe()
    footers = (
        {"source": "total", "rate": described["total_rate"], "amount": described["total"]},
    )
    return Table(
        title=f"Discounts on {described['quantity']}",
        columns=columns,
        rows=tuple(described["lines"]),
        footers=footers,
    )


def dated_title(prefix: str, on: date) -> str:
    return f"{prefix} {on.isoformat()}"
