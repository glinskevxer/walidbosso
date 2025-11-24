"""Storing load-out orders and their loads.

Split out of :mod:`binyard.store.sqlite` rather than added to it, because that
module is already the longest in the package and an order has nothing to do
with a ticket beyond both being rows.

The loads are rewritten on every save, the same way an obligation's releases
are. A load can be added, shipped and have a ticket number attached, and all
three are edits to a small child list. Diffing it would take more code than
writing it out.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from binyard.bushels import Bushels
from binyard.errors import Conflict, NotFound
from binyard.loadout.order import Load, LoadoutOrder, OrderStatus
from binyard.store.mappers import as_date

__all__ = ["MemoryOrderStore", "OrderStore", "SqliteOrderStore"]


class OrderStore:
    """The protocol, written as a base class so both backends read the same."""

    def add(self, order: LoadoutOrder) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, identifier: str) -> LoadoutOrder:  # pragma: no cover - interface
        raise NotImplementedError

    def exists(self, identifier: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def replace(self, order: LoadoutOrder) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def open_orders(self) -> tuple[LoadoutOrder, ...]:  # pragma: no cover - interface
        raise NotImplementedError

    def save(self, order: LoadoutOrder) -> None:
        """Add or replace, whichever applies. What a CLI command wants."""
        if self.exists(order.identifier):
            self.replace(order)
        else:
            self.add(order)


class SqliteOrderStore(OrderStore):
    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, order: LoadoutOrder) -> None:
        if self.exists(order.identifier):
            raise Conflict("that order identifier is already used", identifier=order.identifier)
        self._write(order, insert=True)

    def get(self, identifier: str) -> LoadoutOrder:
        row = self._connection.execute(
            "SELECT * FROM loadout_orders WHERE identifier = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise NotFound("order", identifier)
        return self._order(row)

    def exists(self, identifier: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM loadout_orders WHERE identifier = ?", (identifier,)
            ).fetchone()
            is not None
        )

    def replace(self, order: LoadoutOrder) -> None:
        if not self.exists(order.identifier):
            raise NotFound("order", order.identifier)
        self._write(order, insert=False)

    def open_orders(self) -> tuple[LoadoutOrder, ...]:
        rows = self._connection.execute(
            "SELECT * FROM loadout_orders WHERE status IN (?, ?)"
            " ORDER BY ship_from, identifier",
            (OrderStatus.OPEN.value, OrderStatus.LOADING.value),
        ).fetchall()
        return tuple(self._order(row) for row in rows)

    def _write(self, order: LoadoutOrder, *, insert: bool) -> None:
        values = (
            order.destination,
            order.commodity_code,
            order.quantity.hundredths,
            order.ship_from.isoformat(),
            order.ship_to.isoformat(),
            order.status.value,
            order.overship_tolerance.hundredths,
            order.identifier,
        )
        if insert:
            self._connection.execute(
                "INSERT INTO loadout_orders (destination, commodity_code,"
                " quantity_hundredths, ship_from, ship_to, status, overship_tolerance,"
                " identifier) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        else:
            self._connection.execute(
                "UPDATE loadout_orders SET destination = ?, commodity_code = ?,"
                " quantity_hundredths = ?, ship_from = ?, ship_to = ?, status = ?,"
                " overship_tolerance = ? WHERE identifier = ?",
                values,
            )
        self._connection.execute(
            "DELETE FROM loadout_loads WHERE order_identifier = ?", (order.identifier,)
        )
        for load in order.loads:
            self._connection.execute(
                "INSERT INTO loadout_loads (order_identifier, sequence,"
                " carrier_identifier, quantity_hundredths, shipped_on, ticket_number)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    order.identifier,
                    load.sequence,
                    load.carrier_identifier,
                    load.quantity.hundredths,
                    None if load.shipped_on is None else load.shipped_on.isoformat(),
                    load.ticket_number,
                ),
            )

    def _order(self, row: sqlite3.Row) -> LoadoutOrder:
        mapping = dict(row)
        loads = self._connection.execute(
            "SELECT * FROM loadout_loads WHERE order_identifier = ? ORDER BY sequence",
            (mapping["identifier"],),
        ).fetchall()
        return LoadoutOrder(
            identifier=mapping["identifier"],
            destination=mapping["destination"],
            commodity_code=mapping["commodity_code"],
            quantity=Bushels(int(mapping["quantity_hundredths"])),
            ship_from=as_date(mapping["ship_from"]),
            ship_to=as_date(mapping["ship_to"]),
            status=OrderStatus(mapping["status"]),
            overship_tolerance=Bushels(int(mapping["overship_tolerance"])),
            loads=tuple(
                Load(
                    sequence=int(item["sequence"]),
                    carrier_identifier=item["carrier_identifier"],
                    quantity=Bushels(int(item["quantity_hundredths"])),
                    shipped_on=None
                    if item["shipped_on"] is None
                    else as_date(item["shipped_on"]),
                    ticket_number=item["ticket_number"],
                )
                for item in loads
            ),
        )


class MemoryOrderStore(OrderStore):
    def __init__(self, orders: dict[str, LoadoutOrder] | None = None) -> None:
        self._orders: dict[str, LoadoutOrder] = dict(orders or {})

    def add(self, order: LoadoutOrder) -> None:
        if order.identifier in self._orders:
            raise Conflict("that order identifier is already used", identifier=order.identifier)
        self._orders[order.identifier] = order

    def get(self, identifier: str) -> LoadoutOrder:
        try:
            return self._orders[identifier]
        except KeyError:
            raise NotFound("order", identifier) from None

    def exists(self, identifier: str) -> bool:
        return identifier in self._orders

    def replace(self, order: LoadoutOrder) -> None:
        if order.identifier not in self._orders:
            raise NotFound("order", order.identifier)
        self._orders[order.identifier] = order

    def open_orders(self) -> tuple[LoadoutOrder, ...]:
        return tuple(
            order
            for order in sorted(
                self._orders.values(), key=lambda item: (item.ship_from, item.identifier)
            )
            if order.status in {OrderStatus.OPEN, OrderStatus.LOADING}
        )

    def snapshot(self) -> dict[str, LoadoutOrder]:
        return dict(self._orders)

    def restore(self, snapshot: dict[str, LoadoutOrder]) -> None:
        self._orders = dict(snapshot)


def orders_shipped_by(order: LoadoutOrder, on: date) -> Bushels:
    """How much of an order had gone out by the end of ``on``."""
    total = Bushels.zero()
    for load in order.loads:
        if load.shipped_on is not None and load.shipped_on <= on:
            total = total + load.quantity
    return total
