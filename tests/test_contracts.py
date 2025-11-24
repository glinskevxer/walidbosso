from datetime import date

import pytest

from binyard.bushels import bushels
from binyard.contracts import (
    ContractStatus,
    Delivery,
    FillState,
    PriceKind,
    Priority,
    PurchaseContract,
    eligible,
    plan_application,
)
from binyard.errors import ValidationError
from binyard.money import Price, dollars
from binyard.percent import percent

WINDOW_FROM = date(2025, 10, 1)
WINDOW_TO = date(2025, 12, 31)
DELIVERED_ON = date(2025, 10, 14)


def contract(identifier: str = "C-1", **overrides) -> PurchaseContract:
    fields: dict[str, object] = {
        "identifier": identifier,
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "quantity": bushels("5000"),
        "delivery_from": WINDOW_FROM,
        "delivery_to": WINDOW_TO,
        "price_kind": PriceKind.FLAT,
        "flat_price": Price.of("4.52"),
    }
    fields.update(overrides)
    return PurchaseContract(**fields)  # type: ignore[arg-type]


class TestValidation:
    def test_a_window_that_ends_first_is_refused(self):
        with pytest.raises(ValidationError):
            contract(delivery_from=WINDOW_TO, delivery_to=WINDOW_FROM)

    def test_a_flat_contract_needs_a_price(self):
        with pytest.raises(ValidationError):
            contract(flat_price=None)

    def test_a_basis_contract_needs_a_basis_and_a_month(self):
        with pytest.raises(ValidationError):
            contract(price_kind=PriceKind.BASIS, flat_price=None, basis=Price.of("-0.20"))
        with pytest.raises(ValidationError):
            contract(price_kind=PriceKind.BASIS, flat_price=None, futures_month="CZ25")

    def test_only_a_flat_contract_carries_a_flat_price(self):
        with pytest.raises(ValidationError):
            contract(price_kind=PriceKind.OPEN)

    def test_a_contract_needs_a_positive_quantity(self):
        with pytest.raises(ValidationError):
            contract(quantity=bushels("0"))


class TestTolerance:
    def test_the_band_sits_either_side_of_the_quantity(self):
        held = contract(tolerance=percent("5.0"))
        assert held.floor == bushels("4750")
        assert held.ceiling == bushels("5250")

    def test_nothing_delivered_reads_empty(self):
        assert contract().fill_state is FillState.EMPTY

    def test_under_the_floor_is_short(self):
        held = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("4000")))
        assert held.fill_state is FillState.SHORT
        assert held.shortfall == bushels("750")
        assert held.excess.is_zero()

    def test_inside_the_band_is_filled(self):
        held = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("4800")))
        assert held.fill_state is FillState.FILLED
        assert held.status is ContractStatus.FILLED
        assert held.shortfall.is_zero()

    def test_exactly_on_the_floor_is_filled(self):
        held = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("4750")))
        assert held.fill_state is FillState.FILLED

    def test_over_the_ceiling_is_over(self):
        held = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("5400")))
        assert held.fill_state is FillState.OVER
        assert held.excess == bushels("150")

    def test_a_zero_tolerance_contract_has_to_be_exact(self):
        held = contract(tolerance=percent("0"))
        assert held.floor == held.ceiling == bushels("5000")
        assert (
            held.deliver(Delivery(DELIVERED_ON, "T-1", bushels("4999"))).fill_state
            is FillState.SHORT
        )

    def test_remaining_never_goes_negative(self):
        held = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("6000")))
        assert held.remaining.is_zero()


class TestPricing:
    def test_a_flat_contract_is_priced_from_the_start(self):
        assert contract().is_priced
        assert contract().value_of(bushels("1000")) == dollars("4520.00")

    def test_a_basis_contract_is_unpriced_until_the_futures_are_fixed(self):
        held = contract(
            price_kind=PriceKind.BASIS,
            flat_price=None,
            basis=Price.of("-0.20"),
            futures_month="CZ25",
        )
        assert not held.is_priced
        with pytest.raises(ValidationError):
            held.value_of(bushels("1000"))
        fixed = held.fix_futures(Price.of("4.70"))
        assert fixed.price() == Price.of("4.50")
        assert fixed.value_of(bushels("1000")) == dollars("4500.00")

    def test_only_a_basis_contract_has_futures_to_fix(self):
        with pytest.raises(ValidationError):
            contract().fix_futures(Price.of("4.70"))

    def test_an_open_priced_contract_stays_unpriced(self):
        held = contract(price_kind=PriceKind.OPEN, flat_price=None)
        assert not held.is_priced
        assert held.price() is None


class TestLifecycle:
    def test_a_cancelled_contract_takes_no_deliveries(self):
        cancelled = contract().cancelled()
        with pytest.raises(ValidationError):
            cancelled.deliver(Delivery(DELIVERED_ON, "T-1", bushels("100")))

    def test_a_contract_with_deliveries_cannot_be_cancelled(self):
        held = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("100")))
        with pytest.raises(ValidationError):
            held.cancelled()

    def test_a_cancelled_contract_cannot_be_closed(self):
        with pytest.raises(ValidationError):
            contract().cancelled().closed()

    def test_a_delivery_needs_a_ticket_and_a_quantity(self):
        with pytest.raises(ValidationError):
            Delivery(DELIVERED_ON, "  ", bushels("100"))
        with pytest.raises(ValidationError):
            Delivery(DELIVERED_ON, "T-1", bushels("0"))

    def test_the_window_covers_its_own_edges(self):
        held = contract()
        assert held.covers(WINDOW_FROM)
        assert held.covers(WINDOW_TO)
        assert not held.covers(date(2026, 1, 1))


class TestEligibility:
    def test_only_this_growers_contracts_for_this_crop_are_eligible(self):
        others = [
            contract("C-2", account="OTHER FARMS"),
            contract("C-3", commodity_code="SOYBEANS"),
            contract("C-4"),
        ]
        found = eligible(others, "HALVORSEN FARMS", "CORN", DELIVERED_ON)
        assert [c.identifier for c in found] == ["C-4"]

    def test_a_contract_outside_its_window_is_not_eligible(self):
        late = contract("C-5", delivery_from=date(2026, 1, 1), delivery_to=date(2026, 3, 1))
        assert eligible([late], "HALVORSEN FARMS", "CORN", DELIVERED_ON) == []

    def test_a_full_contract_is_not_eligible(self):
        full = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("5000")))
        assert eligible([full], "HALVORSEN FARMS", "CORN", DELIVERED_ON) == []

    def test_a_cancelled_contract_is_not_eligible(self):
        assert eligible([contract().cancelled()], "HALVORSEN FARMS", "CORN", DELIVERED_ON) == []


class TestPlanning:
    def _plan(self, contracts, quantity="860.71", priority=Priority.EARLIEST_DEADLINE):
        return plan_application(
            ticket_number="WBS-25-000001",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=bushels(quantity),
            on=DELIVERED_ON,
            contracts=contracts,
            priority=priority,
        )

    def test_with_no_contracts_it_all_goes_to_storage(self):
        plan = self._plan([])
        assert plan.to_storage == bushels("860.71")
        assert plan.allocations[0].is_storage
        assert "no open contract" in plan.allocations[0].reason

    def test_one_contract_takes_the_whole_load(self):
        plan = self._plan([contract()])
        assert not plan.is_split
        assert plan.to_contracts == bushels("860.71")
        assert plan.allocations[0].contract_identifier == "C-1"

    def test_the_earliest_deadline_is_filled_first(self):
        early = contract("C-EARLY", delivery_to=date(2025, 11, 15))
        late = contract("C-LATE", delivery_to=date(2025, 12, 31))
        plan = self._plan([late, early])
        assert plan.allocations[0].contract_identifier == "C-EARLY"
        assert "2025-11-15" in plan.allocations[0].reason

    def test_the_best_price_can_be_filled_first_instead(self):
        cheap = contract("C-CHEAP", flat_price=Price.of("4.20"))
        dear = contract("C-DEAR", flat_price=Price.of("4.80"))
        plan = self._plan([cheap, dear], priority=Priority.BEST_PRICE)
        assert plan.allocations[0].contract_identifier == "C-DEAR"
        assert "pays $4.8" in plan.allocations[0].reason

    def test_an_unpriced_contract_goes_last_under_best_price(self):
        priced = contract("C-PRICED")
        unpriced = contract("C-OPEN", price_kind=PriceKind.OPEN, flat_price=None)
        plan = self._plan([unpriced, priced], quantity="9000", priority=Priority.BEST_PRICE)
        assert plan.allocations[0].contract_identifier == "C-PRICED"
        assert plan.allocations[1].contract_identifier == "C-OPEN"

    def test_listed_order_is_respected_when_asked_for(self):
        first = contract("C-FIRST", delivery_to=date(2025, 12, 31))
        second = contract("C-SECOND", delivery_to=date(2025, 11, 1))
        plan = self._plan([first, second], priority=Priority.AS_LISTED)
        assert plan.allocations[0].contract_identifier == "C-FIRST"

    def test_a_load_bigger_than_one_contract_splits(self):
        small = contract("C-SMALL", quantity=bushels("500"), delivery_to=date(2025, 11, 1))
        big = contract("C-BIG", quantity=bushels("5000"), delivery_to=date(2025, 12, 31))
        plan = self._plan([small, big], quantity="1000")
        assert plan.is_split
        assert plan.allocations[0].quantity == bushels("500")
        assert plan.allocations[1].quantity == bushels("500")

    def test_the_overflow_lands_in_storage(self):
        small = contract("C-SMALL", quantity=bushels("500"))
        plan = self._plan([small], quantity="1000")
        assert plan.to_contracts == bushels("500")
        assert plan.to_storage == bushels("500")
        assert "every covering contract is full" in plan.allocations[-1].reason

    def test_the_allocations_add_back_to_the_load(self):
        contracts = [
            contract("C-A", quantity=bushels("300")),
            contract("C-B", quantity=bushels("400")),
        ]
        plan = self._plan(contracts, quantity="1000")
        total = bushels("0")
        for allocation in plan.allocations:
            total = total + allocation.quantity
        assert total == plan.quantity

    def test_a_non_positive_load_is_refused(self):
        with pytest.raises(ValidationError):
            self._plan([contract()], quantity="0")

    def test_the_plan_renders_with_every_line(self):
        described = self._plan([contract()]).describe()
        assert described["priority"] == "earliest_deadline"
        assert described["allocations"][0]["contract"] == "C-1"


def test_a_contract_renders_for_the_desk():
    described = contract().deliver(Delivery(DELIVERED_ON, "T-1", bushels("4000"))).describe()
    assert described["fill_state"] == "short"
    assert described["shortfall"] == "750.00 bu"
    assert described["price"] == "$4.52"
