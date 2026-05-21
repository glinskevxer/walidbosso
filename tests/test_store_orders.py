"""The order store on both backends.

Orders were the last thing still held in a module dict, and the store that
replaced it was the least covered thing in the package. This is that gap.
"""

from __future__ import annotations

from datetime import date

import pytest

from binyard.bushels import bushels
from binyard.errors import Conflict, NotFound
from binyard.loadout.order import LoadoutOrder, OrderStatus
from binyard.store import MemoryUnitOfWork, SqliteUnitOfWork
from binyard.store.orders import orders_shipped_by

SHIP_FROM = date(2025, 11, 1)
SHIP_TO = date(2025, 11, 30)


@pytest.fixture(params=["memory", "sqlite"])
def orders(request):
    if request.param == "memory":
        yield MemoryUnitOfWork().orders
        return
    unit = SqliteUnitOfWork.open()
    try:
        yield unit.orders
    finally:
        unit.close()


def order(identifier="LO-1", quantity="5000", **overrides) -> LoadoutOrder:
    fields: dict[str, object] = {
        "identifier": identifier,
        "destination": "GULF TERMINAL",
        "commodity_code": "CORN",
        "quantity": bushels(quantity),
        "ship_from": SHIP_FROM,
        "ship_to": SHIP_TO,
    }
    fields.update(overrides)
    return LoadoutOrder(**fields)  # type: ignore[arg-type]


class TestRoundTrip:
    def test_an_order_comes_back_whole(self, orders):
        orders.add(order())
        found = orders.get("LO-1")
        assert found.destination == "GULF TERMINAL"
        assert found.quantity == bushels("5000")
        assert found.status is OrderStatus.OPEN

    def test_the_loads_come_back_in_sequence(self, orders):
        held = order().add_load("T-14", bushels("800")).add_load("T-15", bushels("800"))
        orders.add(held)
        found = orders.get("LO-1")
        assert [load.sequence for load in found.loads] == [1, 2]
        assert found.planned == bushels("1600")

    def test_a_shipped_load_keeps_its_ticket(self, orders):
        held = order().add_load("T-14", bushels("800"))
        held = held.ship_load(1, date(2025, 11, 3), "WBS-25-000900")
        orders.add(held)
        found = orders.get("LO-1")
        assert found.loads[0].ticket_number == "WBS-25-000900"
        assert found.shipped == bushels("800")

    def test_an_overship_tolerance_survives(self, orders):
        orders.add(order(overship_tolerance=bushels("250")))
        assert orders.get("LO-1").ceiling == bushels("5250")


class TestIdentity:
    def test_a_duplicate_identifier_is_a_conflict(self, orders):
        orders.add(order())
        with pytest.raises(Conflict):
            orders.add(order())

    def test_an_order_that_is_not_there(self, orders):
        with pytest.raises(NotFound):
            orders.get("LO-9")

    def test_exists_answers_without_raising(self, orders):
        assert not orders.exists("LO-1")
        orders.add(order())
        assert orders.exists("LO-1")

    def test_replacing_one_that_is_not_there(self, orders):
        with pytest.raises(NotFound):
            orders.replace(order())

    def test_save_adds_or_replaces(self, orders):
        orders.save(order())
        orders.save(order().add_load("T-14", bushels("800")))
        assert orders.get("LO-1").planned == bushels("800")


class TestListing:
    def test_open_and_loading_orders_are_listed(self, orders):
        orders.add(order("LO-1"))
        orders.add(order("LO-2").add_load("T-14", bushels("100")))
        assert [item.identifier for item in orders.open_orders()] == ["LO-1", "LO-2"]

    def test_a_complete_order_drops_off(self, orders):
        held = order("LO-1", quantity="800").add_load("T-14", bushels("800"))
        held = held.ship_load(1, date(2025, 11, 3), "T")
        orders.add(held)
        assert orders.open_orders() == ()

    def test_a_cancelled_order_drops_off(self, orders):
        orders.add(order().cancelled())
        assert orders.open_orders() == ()

    def test_they_come_back_oldest_window_first(self, orders):
        orders.add(order("LO-LATE", ship_from=date(2025, 12, 1), ship_to=date(2025, 12, 31)))
        orders.add(order("LO-EARLY"))
        assert [item.identifier for item in orders.open_orders()] == ["LO-EARLY", "LO-LATE"]


class TestShippedBy:
    def test_nothing_shipped_yet(self):
        assert orders_shipped_by(order(), date(2025, 11, 30)) == bushels("0")

    def test_only_loads_shipped_by_the_date_count(self):
        held = order().add_load("T-14", bushels("800")).add_load("T-15", bushels("800"))
        held = held.ship_load(1, date(2025, 11, 3), "T-1")
        held = held.ship_load(2, date(2025, 11, 20), "T-2")
        assert orders_shipped_by(held, date(2025, 11, 10)) == bushels("800")
        assert orders_shipped_by(held, date(2025, 11, 30)) == bushels("1600")

    def test_a_planned_but_unshipped_load_does_not_count(self):
        held = order().add_load("T-14", bushels("800"))
        assert orders_shipped_by(held, date(2025, 11, 30)) == bushels("0")
