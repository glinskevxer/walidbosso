"""A SQLite backend for every store, and the unit of work that binds them.

SQLite because a country elevator is one building with two lanes and a handful
of terminals, and a file that can be copied to a memory stick at close of
business is worth more to that operation than a cluster. The DB-API surface
used here is ordinary, so the same statements run against Postgres with a
different placeholder style, which is why the placeholder is not hard coded in
the row builders.

Two things are worth knowing before changing anything in here.

**The ledger is rewritten wholesale on save.** Movements can be backdated, a
correction posted this afternoon can belong to Tuesday morning, so an append
only insert would put the rows in the wrong order and the fold would read a bin
overdrawn that never was. A season is a few thousand movements and the rewrite
is one statement inside the caller's transaction, so it costs less than the
bookkeeping needed to do it incrementally and correctly.

**Foreign keys are turned on per connection.** SQLite has them off by default,
and a schema full of REFERENCES clauses that are never enforced is worse than
one with none, because it reads as though somebody checked.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

from binyard.bushels import Bushels
from binyard.contracts.contract import (
    ContractStatus,
    Delivery,
    PriceKind,
    PurchaseContract,
)
from binyard.errors import Conflict, NotFound
from binyard.identity import TicketNumber
from binyard.inventory.bin import Bin, BinKind
from binyard.inventory.ledger import Ledger, Movement, MovementKind
from binyard.inventory.yard import Yard
from binyard.money import Money, Price
from binyard.obligations.obligation import (
    ObligationKind,
    ObligationStatus,
    Release,
    StorageObligation,
)
from binyard.percent import Percent
from binyard.settlement.statement import ChargeKind, Settlement, StatementLine
from binyard.store.drying import SqliteDryerStore
from binyard.store.feed import SqliteFeedLog
from binyard.store.mappers import (
    as_date,
    as_datetime,
    grade_from_json,
    grade_to_json,
    history_from_json,
    history_to_json,
    sample_from_json,
    sample_to_json,
    shrink_from_json,
    shrink_to_json,
    weights_from_row,
    weights_to_row,
)
from binyard.store.migrate import migrate
from binyard.store.orders import SqliteOrderStore
from binyard.tickets.ticket import ScaleTicket, TicketKind, TicketStatus

__all__ = ["SqliteUnitOfWork", "connect"]


def connect(
    path: str | Path = ":memory:", *, run_migrations: bool = True
) -> sqlite3.Connection:
    """Open a connection with the settings this schema assumes."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    if run_migrations:
        migrate(connection)
    return connection


class _Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def _one(self, sql: str, parameters: tuple[Any, ...]) -> sqlite3.Row | None:
        return self._connection.execute(sql, parameters).fetchone()

    def _all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return list(self._connection.execute(sql, parameters))


class SqliteTicketStore(_Store):
    _COLUMNS = (
        "number, kind, commodity_code, account, weighed_on, gross_hundredths,"
        " tare_hundredths, tare_source, gross_at, tare_at, status, hauler,"
        " sample_json, grade_json, shrink_json, voids, void_reason, history_json"
    )

    def add(self, ticket: ScaleTicket) -> None:
        if self.exists(ticket.number):
            raise Conflict("that ticket number is already used", number=str(ticket.number))
        row = self._row(ticket)
        placeholders = ", ".join("?" for _ in row)
        self._connection.execute(
            f"INSERT INTO tickets ({', '.join(row)}) VALUES ({placeholders})",
            tuple(row.values()),
        )

    def get(self, number: TicketNumber) -> ScaleTicket:
        row = self._one(f"SELECT {self._COLUMNS} FROM tickets WHERE number = ?", (str(number),))
        if row is None:
            raise NotFound("ticket", str(number))
        return self._ticket(row)

    def exists(self, number: TicketNumber) -> bool:
        return self._one("SELECT 1 FROM tickets WHERE number = ?", (str(number),)) is not None

    def replace(self, ticket: ScaleTicket) -> None:
        if not self.exists(ticket.number):
            raise NotFound("ticket", str(ticket.number))
        row = self._row(ticket)
        assignments = ", ".join(f"{name} = ?" for name in row if name != "number")
        values = [value for name, value in row.items() if name != "number"]
        self._connection.execute(
            f"UPDATE tickets SET {assignments} WHERE number = ?",
            (*values, str(ticket.number)),
        )

    def for_account(self, account: str) -> tuple[ScaleTicket, ...]:
        rows = self._all(
            f"SELECT {self._COLUMNS} FROM tickets WHERE account = ? ORDER BY number",
            (account,),
        )
        return tuple(self._ticket(row) for row in rows)

    def weighed_on(self, day: date) -> tuple[ScaleTicket, ...]:
        rows = self._all(
            f"SELECT {self._COLUMNS} FROM tickets WHERE weighed_on = ? ORDER BY number",
            (day.isoformat(),),
        )
        return tuple(self._ticket(row) for row in rows)

    def open_tickets(self) -> tuple[ScaleTicket, ...]:
        rows = self._all(
            f"SELECT {self._COLUMNS} FROM tickets WHERE status NOT IN (?, ?) ORDER BY number",
            (TicketStatus.SETTLED.value, TicketStatus.VOID.value),
        )
        return tuple(self._ticket(row) for row in rows)

    def last_issued(self) -> TicketNumber | None:
        row = self._one("SELECT MAX(number) AS highest FROM tickets", ())
        if row is None or row["highest"] is None:
            return None
        return TicketNumber.parse(str(row["highest"]))

    @staticmethod
    def _row(ticket: ScaleTicket) -> dict[str, Any]:
        weights = weights_to_row(ticket.weights)
        return {
            "number": str(ticket.number),
            "kind": ticket.kind.value,
            "commodity_code": ticket.commodity_code,
            "account": ticket.account,
            "weighed_on": ticket.weighed_on.isoformat(),
            **weights,
            "status": ticket.status.value,
            "hauler": ticket.hauler,
            "sample_json": sample_to_json(ticket.sample),
            "grade_json": grade_to_json(ticket.grade),
            "shrink_json": shrink_to_json(ticket.shrink),
            "voids": None if ticket.voids is None else str(ticket.voids),
            "void_reason": ticket.void_reason,
            "history_json": history_to_json(ticket.history),
        }

    @staticmethod
    def _ticket(row: sqlite3.Row) -> ScaleTicket:
        mapping = dict(row)
        return ScaleTicket(
            number=TicketNumber.parse(mapping["number"]),
            kind=TicketKind(mapping["kind"]),
            commodity_code=mapping["commodity_code"],
            account=mapping["account"],
            weighed_on=as_date(mapping["weighed_on"]),
            weights=weights_from_row(mapping),
            status=TicketStatus(mapping["status"]),
            hauler=mapping["hauler"],
            sample=sample_from_json(mapping["sample_json"]),
            grade=grade_from_json(mapping["grade_json"]),
            shrink=shrink_from_json(mapping["shrink_json"]),
            voids=None if mapping["voids"] is None else TicketNumber.parse(mapping["voids"]),
            void_reason=mapping["void_reason"],
            history=history_from_json(mapping["history_json"]),
        )


class SqliteObligationStore(_Store):
    def add(self, obligation: StorageObligation) -> None:
        if self.exists(obligation.identifier):
            raise Conflict(
                "that obligation identifier is already used",
                identifier=obligation.identifier,
            )
        self._write(obligation, insert=True)

    def get(self, identifier: str) -> StorageObligation:
        row = self._one("SELECT * FROM obligations WHERE identifier = ?", (identifier,))
        if row is None:
            raise NotFound("obligation", identifier)
        return self._obligation(row)

    def exists(self, identifier: str) -> bool:
        return (
            self._one("SELECT 1 FROM obligations WHERE identifier = ?", (identifier,))
            is not None
        )

    def replace(self, obligation: StorageObligation) -> None:
        if not self.exists(obligation.identifier):
            raise NotFound("obligation", obligation.identifier)
        self._write(obligation, insert=False)

    def open_for(self, account: str, commodity_code: str) -> tuple[StorageObligation, ...]:
        rows = self._all(
            "SELECT * FROM obligations WHERE account = ? AND commodity_code = ?"
            " AND status = ? ORDER BY opened_on, identifier",
            (account, commodity_code, ObligationStatus.OPEN.value),
        )
        return tuple(self._obligation(row) for row in rows)

    def all_open(self) -> tuple[StorageObligation, ...]:
        rows = self._all(
            "SELECT * FROM obligations WHERE status = ? ORDER BY opened_on, identifier",
            (ObligationStatus.OPEN.value,),
        )
        return tuple(self._obligation(row) for row in rows)

    def as_of(self, day: date) -> tuple[StorageObligation, ...]:
        rows = self._all(
            "SELECT * FROM obligations WHERE opened_on <= ? ORDER BY opened_on, identifier",
            (day.isoformat(),),
        )
        return tuple(self._obligation(row) for row in rows)

    def _write(self, obligation: StorageObligation, *, insert: bool) -> None:
        values = {
            "identifier": obligation.identifier,
            "account": obligation.account,
            "commodity_code": obligation.commodity_code,
            "kind": obligation.kind.value,
            "quantity_hundredths": obligation.quantity.hundredths,
            "opened_on": obligation.opened_on.isoformat(),
            "free_days": obligation.free_days,
            "storage_rate_ticks": obligation.storage_rate.ticks,
            "priced_at_ticks": None
            if obligation.priced_at is None
            else obligation.priced_at.ticks,
            "priced_on": None
            if obligation.priced_on is None
            else obligation.priced_on.isoformat(),
            "status": obligation.status.value,
            "superseded_by": obligation.superseded_by,
        }
        if insert:
            placeholders = ", ".join("?" for _ in values)
            self._connection.execute(
                f"INSERT INTO obligations ({', '.join(values)}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        else:
            assignments = ", ".join(f"{name} = ?" for name in values if name != "identifier")
            rest = [value for name, value in values.items() if name != "identifier"]
            self._connection.execute(
                f"UPDATE obligations SET {assignments} WHERE identifier = ?",
                (*rest, obligation.identifier),
            )
        self._connection.execute(
            "DELETE FROM obligation_releases WHERE obligation = ?", (obligation.identifier,)
        )
        for release in obligation.releases:
            self._connection.execute(
                "INSERT INTO obligation_releases"
                " (obligation, released_on, quantity_hundredths, reference, note)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    obligation.identifier,
                    release.on.isoformat(),
                    release.quantity.hundredths,
                    release.reference,
                    release.note,
                ),
            )

    def _obligation(self, row: sqlite3.Row) -> StorageObligation:
        mapping = dict(row)
        releases = self._all(
            "SELECT * FROM obligation_releases WHERE obligation = ? ORDER BY released_on, id",
            (mapping["identifier"],),
        )
        return StorageObligation(
            identifier=mapping["identifier"],
            account=mapping["account"],
            commodity_code=mapping["commodity_code"],
            kind=ObligationKind(mapping["kind"]),
            quantity=Bushels(int(mapping["quantity_hundredths"])),
            opened_on=as_date(mapping["opened_on"]),
            free_days=int(mapping["free_days"]),
            storage_rate=Price(int(mapping["storage_rate_ticks"])),
            priced_at=None
            if mapping["priced_at_ticks"] is None
            else Price(int(mapping["priced_at_ticks"])),
            priced_on=None if mapping["priced_on"] is None else as_date(mapping["priced_on"]),
            status=ObligationStatus(mapping["status"]),
            releases=tuple(
                Release(
                    on=as_date(item["released_on"]),
                    quantity=Bushels(int(item["quantity_hundredths"])),
                    reference=item["reference"],
                    note=item["note"],
                )
                for item in releases
            ),
            superseded_by=mapping["superseded_by"],
        )


class SqliteContractStore(_Store):
    def add(self, contract: PurchaseContract) -> None:
        if self.exists(contract.identifier):
            raise Conflict(
                "that contract identifier is already used", identifier=contract.identifier
            )
        self._write(contract, insert=True)

    def get(self, identifier: str) -> PurchaseContract:
        row = self._one("SELECT * FROM contracts WHERE identifier = ?", (identifier,))
        if row is None:
            raise NotFound("contract", identifier)
        return self._contract(row)

    def exists(self, identifier: str) -> bool:
        return (
            self._one("SELECT 1 FROM contracts WHERE identifier = ?", (identifier,)) is not None
        )

    def replace(self, contract: PurchaseContract) -> None:
        if not self.exists(contract.identifier):
            raise NotFound("contract", contract.identifier)
        self._write(contract, insert=False)

    def open_for(
        self, account: str, commodity_code: str, on: date
    ) -> tuple[PurchaseContract, ...]:
        rows = self._all(
            "SELECT * FROM contracts WHERE account = ? AND commodity_code = ?"
            " AND status IN (?, ?) AND delivery_from <= ? AND delivery_to >= ?"
            " ORDER BY identifier",
            (
                account,
                commodity_code,
                ContractStatus.OPEN.value,
                ContractStatus.FILLED.value,
                on.isoformat(),
                on.isoformat(),
            ),
        )
        return tuple(self._contract(row) for row in rows)

    def _write(self, contract: PurchaseContract, *, insert: bool) -> None:
        values = {
            "identifier": contract.identifier,
            "account": contract.account,
            "commodity_code": contract.commodity_code,
            "quantity_hundredths": contract.quantity.hundredths,
            "delivery_from": contract.delivery_from.isoformat(),
            "delivery_to": contract.delivery_to.isoformat(),
            "price_kind": contract.price_kind.value,
            "flat_price_ticks": None
            if contract.flat_price is None
            else contract.flat_price.ticks,
            "basis_ticks": None if contract.basis is None else contract.basis.ticks,
            "futures_month": contract.futures_month,
            "futures_price_ticks": None
            if contract.futures_price is None
            else contract.futures_price.ticks,
            "tolerance_hundredths": contract.tolerance.hundredths,
            "status": contract.status.value,
            "written_on": None
            if contract.written_on is None
            else contract.written_on.isoformat(),
        }
        if insert:
            placeholders = ", ".join("?" for _ in values)
            self._connection.execute(
                f"INSERT INTO contracts ({', '.join(values)}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        else:
            assignments = ", ".join(f"{name} = ?" for name in values if name != "identifier")
            rest = [value for name, value in values.items() if name != "identifier"]
            self._connection.execute(
                f"UPDATE contracts SET {assignments} WHERE identifier = ?",
                (*rest, contract.identifier),
            )
        self._connection.execute(
            "DELETE FROM contract_deliveries WHERE contract = ?", (contract.identifier,)
        )
        for delivery in contract.deliveries:
            self._connection.execute(
                "INSERT INTO contract_deliveries"
                " (contract, delivered_on, ticket_number, quantity_hundredths)"
                " VALUES (?, ?, ?, ?)",
                (
                    contract.identifier,
                    delivery.on.isoformat(),
                    delivery.ticket_number,
                    delivery.quantity.hundredths,
                ),
            )

    def _contract(self, row: sqlite3.Row) -> PurchaseContract:
        mapping = dict(row)
        deliveries = self._all(
            "SELECT * FROM contract_deliveries WHERE contract = ? ORDER BY delivered_on, id",
            (mapping["identifier"],),
        )
        return PurchaseContract(
            identifier=mapping["identifier"],
            account=mapping["account"],
            commodity_code=mapping["commodity_code"],
            quantity=Bushels(int(mapping["quantity_hundredths"])),
            delivery_from=as_date(mapping["delivery_from"]),
            delivery_to=as_date(mapping["delivery_to"]),
            price_kind=PriceKind(mapping["price_kind"]),
            flat_price=None
            if mapping["flat_price_ticks"] is None
            else Price(int(mapping["flat_price_ticks"])),
            basis=None
            if mapping["basis_ticks"] is None
            else Price(int(mapping["basis_ticks"])),
            futures_month=mapping["futures_month"],
            futures_price=None
            if mapping["futures_price_ticks"] is None
            else Price(int(mapping["futures_price_ticks"])),
            tolerance=Percent(int(mapping["tolerance_hundredths"])),
            status=ContractStatus(mapping["status"]),
            written_on=None
            if mapping["written_on"] is None
            else as_date(mapping["written_on"]),
            deliveries=tuple(
                Delivery(
                    on=as_date(item["delivered_on"]),
                    ticket_number=item["ticket_number"],
                    quantity=Bushels(int(item["quantity_hundredths"])),
                )
                for item in deliveries
            ),
        )


class SqliteSettlementStore(_Store):
    def add(self, settlement: Settlement) -> None:
        if self._one("SELECT 1 FROM settlements WHERE number = ?", (settlement.number,)):
            raise Conflict("that settlement number is already used", number=settlement.number)
        self._connection.execute(
            "INSERT INTO settlements"
            " (number, account, commodity_code, settled_on, quantity_hundredths, net_cents)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                settlement.number,
                settlement.account,
                settlement.commodity_code,
                settlement.on.isoformat(),
                settlement.quantity.hundredths,
                settlement.net.cents,
            ),
        )
        for position, line in enumerate(settlement.lines):
            self._connection.execute(
                "INSERT INTO settlement_lines"
                " (settlement, position, kind, description, quantity_hundredths,"
                "  rate_ticks, amount_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    settlement.number,
                    position,
                    line.kind.value,
                    line.description,
                    None if line.quantity is None else line.quantity.hundredths,
                    None if line.rate is None else line.rate.ticks,
                    line.amount.cents,
                ),
            )

    def get(self, number: str) -> Settlement:
        row = self._one("SELECT * FROM settlements WHERE number = ?", (number,))
        if row is None:
            raise NotFound("settlement", number)
        return self._settlement(row)

    def for_account(self, account: str) -> tuple[Settlement, ...]:
        rows = self._all(
            "SELECT * FROM settlements WHERE account = ? ORDER BY number", (account,)
        )
        return tuple(self._settlement(row) for row in rows)

    def _settlement(self, row: sqlite3.Row) -> Settlement:
        mapping = dict(row)
        lines = self._all(
            "SELECT * FROM settlement_lines WHERE settlement = ? ORDER BY position",
            (mapping["number"],),
        )
        return Settlement(
            number=mapping["number"],
            account=mapping["account"],
            commodity_code=mapping["commodity_code"],
            on=as_date(mapping["settled_on"]),
            quantity=Bushels(int(mapping["quantity_hundredths"])),
            lines=tuple(
                StatementLine(
                    kind=ChargeKind(item["kind"]),
                    description=item["description"],
                    amount=Money(int(item["amount_cents"])),
                    quantity=None
                    if item["quantity_hundredths"] is None
                    else Bushels(int(item["quantity_hundredths"])),
                    rate=None if item["rate_ticks"] is None else Price(int(item["rate_ticks"])),
                )
                for item in lines
            ),
        )


class SqliteYardStore(_Store):
    def load(self) -> Yard:
        bins = tuple(
            self._bin(row) for row in self._all("SELECT * FROM bins ORDER BY identifier")
        )
        movements = tuple(
            self._movement(row)
            for row in self._all("SELECT * FROM movements ORDER BY occurred_at, id")
        )
        return Yard(bins=bins, ledger=Ledger(entries=movements))

    def save(self, yard: Yard) -> None:
        self._connection.execute("DELETE FROM movements")
        known = {row["identifier"] for row in self._all("SELECT identifier FROM bins")}
        for structure in yard.bins:
            values = (
                structure.name,
                structure.kind.value,
                structure.capacity.hundredths,
                ",".join(sorted(structure.allowed_commodities)),
                1 if structure.aerated else 0,
                structure.pile_allowance.hundredths,
                structure.identifier,
            )
            if structure.identifier in known:
                self._connection.execute(
                    "UPDATE bins SET name = ?, kind = ?, capacity_hundredths = ?,"
                    " allowed_commodities = ?, aerated = ?, pile_allowance = ?"
                    " WHERE identifier = ?",
                    values,
                )
            else:
                self._connection.execute(
                    "INSERT INTO bins (name, kind, capacity_hundredths,"
                    " allowed_commodities, aerated, pile_allowance, identifier)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    values,
                )
        for entry in yard.ledger:
            self._connection.execute(
                "INSERT INTO movements (occurred_at, bin_identifier, kind, commodity_code,"
                " quantity_hundredths, reference, posted_by, quality_json, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.at.isoformat(),
                    entry.bin_identifier,
                    entry.kind.value,
                    entry.commodity_code,
                    entry.quantity.hundredths,
                    entry.reference,
                    entry.by,
                    sample_to_json(entry.quality),
                    entry.note,
                ),
            )

    @staticmethod
    def _bin(row: sqlite3.Row) -> Bin:
        mapping = dict(row)
        allowed = mapping["allowed_commodities"]
        return Bin(
            identifier=mapping["identifier"],
            name=mapping["name"],
            kind=BinKind(mapping["kind"]),
            capacity=Bushels(int(mapping["capacity_hundredths"])),
            allowed_commodities=frozenset(part for part in allowed.split(",") if part),
            aerated=bool(mapping["aerated"]),
            pile_allowance=Percent(int(mapping["pile_allowance"])),
        )

    @staticmethod
    def _movement(row: sqlite3.Row) -> Movement:
        mapping = dict(row)
        at = as_datetime(mapping["occurred_at"])
        assert at is not None
        return Movement(
            at=at,
            bin_identifier=mapping["bin_identifier"],
            kind=MovementKind(mapping["kind"]),
            commodity_code=mapping["commodity_code"],
            quantity=Bushels(int(mapping["quantity_hundredths"])),
            reference=mapping["reference"],
            by=mapping["posted_by"],
            quality=sample_from_json(mapping["quality_json"]),
            note=mapping["note"],
        )


class SqliteUnitOfWork:
    """One SQLite transaction spanning every store."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self.tickets = SqliteTicketStore(connection)
        self.obligations = SqliteObligationStore(connection)
        self.contracts = SqliteContractStore(connection)
        self.settlements = SqliteSettlementStore(connection)
        self.orders = SqliteOrderStore(connection)
        self.dryer = SqliteDryerStore(connection)
        self.feed = SqliteFeedLog(connection)
        self.yard = SqliteYardStore(connection)

    @classmethod
    def open(cls, path: str | Path = ":memory:") -> SqliteUnitOfWork:
        return cls(connect(path))

    def __enter__(self) -> SqliteUnitOfWork:
        return self

    def __exit__(self, *exception: Any) -> None:
        self.rollback()

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()
