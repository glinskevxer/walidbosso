"""Laws the money-shaped parts have to obey.

Storage, demurrage, contracts and settlements all turn a quantity and a rate
into a charge, and every one of them has an invariant that is obvious when
written down and easy to break with one sign or one boundary.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from binyard.bushels import Bushels
from binyard.contracts.contract import (
    ContractStatus,
    Delivery,
    FillState,
    PriceKind,
    PurchaseContract,
)
from binyard.loadout.rail import DemurrageTerms, Placement, RailCar, bill_demurrage
from binyard.money import Money, Price
from binyard.obligations.obligation import ObligationKind, Release, StorageObligation
from binyard.obligations.storage import Accrual, StorageTerms, accrue
from binyard.percent import Percent
from binyard.settlement.statement import ChargeKind, SettlementBuilder

OPENED = date(2025, 10, 15)

quantities = st.integers(min_value=1, max_value=10_000_000).map(Bushels)
rates = st.integers(min_value=0, max_value=100_000).map(Price)
days = st.integers(min_value=0, max_value=500)
tolerances = st.integers(min_value=0, max_value=2000).map(Percent)


def obligation(quantity: Bushels, **overrides) -> StorageObligation:
    fields: dict[str, object] = {
        "identifier": "OS-1",
        "account": "HALVORSEN FARMS",
        "commodity_code": "CORN",
        "kind": ObligationKind.OPEN_STORAGE,
        "quantity": quantity,
        "opened_on": OPENED,
    }
    fields.update(overrides)
    return StorageObligation(**fields)  # type: ignore[arg-type]


class TestStorageLaws:
    @given(quantities, rates, days)
    @settings(max_examples=80, deadline=None)
    def test_storage_is_never_negative(self, quantity, rate, offset):
        terms = StorageTerms(rate_per_bushel_month=rate, free_days=30)
        found = accrue(obligation(quantity), terms, OPENED + timedelta(days=offset))
        assert found.total.cents >= 0

    @given(quantities, rates, days)
    @settings(max_examples=80, deadline=None)
    def test_the_segments_always_add_to_the_total(self, quantity, rate, offset):
        terms = StorageTerms(rate_per_bushel_month=rate, free_days=30)
        found = accrue(obligation(quantity), terms, OPENED + timedelta(days=offset))
        assume(not found.minimum_applied)
        running = Money.zero()
        for segment in found.segments:
            running = running + segment.charge
        assert running == found.total

    @given(quantities, rates, st.integers(min_value=31, max_value=400))
    @settings(max_examples=60, deadline=None)
    def test_storing_longer_never_costs_less(self, quantity, rate, offset):
        terms = StorageTerms(rate_per_bushel_month=rate, free_days=30)
        held = obligation(quantity)
        shorter = accrue(held, terms, OPENED + timedelta(days=offset))
        longer = accrue(held, terms, OPENED + timedelta(days=offset + 40))
        assert longer.total >= shorter.total

    @given(quantities, rates, st.integers(min_value=31, max_value=300))
    @settings(max_examples=60, deadline=None)
    def test_a_release_never_costs_more_than_holding_on(self, quantity, rate, offset):
        terms = StorageTerms(rate_per_bushel_month=rate, free_days=30)
        held = obligation(quantity)
        half = Bushels(quantity.hundredths // 2)
        assume(half.is_positive())
        drawn = held.release(Release(OPENED + timedelta(days=45), half, "LO-1"))
        through = OPENED + timedelta(days=offset + 60)
        assert accrue(drawn, terms, through).total <= accrue(held, terms, through).total

    @given(quantities, rates, days)
    @settings(max_examples=60, deadline=None)
    def test_per_day_never_charges_more_than_per_month_started(self, quantity, rate, offset):
        by_day = StorageTerms(rate_per_bushel_month=rate, free_days=30, accrual=Accrual.PER_DAY)
        by_month = StorageTerms(rate_per_bushel_month=rate, free_days=30)
        through = OPENED + timedelta(days=offset)
        held = obligation(quantity)
        assert accrue(held, by_day, through).total <= accrue(held, by_month, through).total


class TestContractLaws:
    @given(quantities, tolerances, quantities)
    def test_the_fill_state_follows_the_band(self, quantity, tolerance, delivered):
        contract = PurchaseContract(
            identifier="C-1",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=quantity,
            delivery_from=OPENED,
            delivery_to=OPENED + timedelta(days=90),
            price_kind=PriceKind.FLAT,
            flat_price=Price.of("4.52"),
            tolerance=tolerance,
        ).deliver(Delivery(OPENED, "T-1", delivered))

        if delivered < contract.floor:
            assert contract.fill_state is FillState.SHORT
        elif delivered > contract.ceiling:
            assert contract.fill_state is FillState.OVER
        else:
            assert contract.fill_state is FillState.FILLED

    @given(quantities, tolerances, quantities)
    def test_a_contract_is_never_both_short_and_over(self, quantity, tolerance, delivered):
        contract = PurchaseContract(
            identifier="C-1",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=quantity,
            delivery_from=OPENED,
            delivery_to=OPENED + timedelta(days=90),
            price_kind=PriceKind.FLAT,
            flat_price=Price.of("4.52"),
            tolerance=tolerance,
        ).deliver(Delivery(OPENED, "T-1", delivered))
        assert not (contract.shortfall.is_positive() and contract.excess.is_positive())

    @given(quantities, quantities)
    def test_delivering_enough_marks_the_contract_filled(self, quantity, extra):
        contract = PurchaseContract(
            identifier="C-1",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=quantity,
            delivery_from=OPENED,
            delivery_to=OPENED + timedelta(days=90),
            price_kind=PriceKind.FLAT,
            flat_price=Price.of("4.52"),
        ).deliver(Delivery(OPENED, "T-1", quantity + extra))
        assert contract.status is ContractStatus.FILLED
        assert contract.remaining.is_zero()


class TestDemurrageLaws:
    @given(
        st.integers(min_value=1, max_value=40),
        st.integers(min_value=0, max_value=10),
        rates,
        st.integers(min_value=0, max_value=60),
    )
    @settings(max_examples=60, deadline=None)
    def test_a_bill_is_never_negative(self, cars, free, rate, offset):
        placement = Placement(
            identifier="PL-1",
            order_identifier="LO-1",
            cars=tuple(
                RailCar(reporting_mark=f"C{index}", placed_at=datetime(2026, 1, 5, 9, 0))
                for index in range(cars)
            ),
        )
        terms = DemurrageTerms(free_days=free, per_car_day=rate)
        bill = bill_demurrage(placement, terms, date(2026, 1, 5) + timedelta(days=offset))
        assert bill.net_days >= 0
        assert bill.amount.cents >= 0

    @given(st.integers(min_value=1, max_value=20), st.integers(min_value=1, max_value=60))
    @settings(max_examples=40, deadline=None)
    def test_more_cars_never_cost_less(self, cars, offset):
        terms = DemurrageTerms(free_days=2, per_car_day=Price.of("150.00"))
        through = date(2026, 1, 5) + timedelta(days=offset)

        def bill_for(count: int) -> Money:
            placement = Placement(
                identifier="PL-1",
                order_identifier="LO-1",
                cars=tuple(
                    RailCar(reporting_mark=f"C{index}", placed_at=datetime(2026, 1, 5, 9, 0))
                    for index in range(count)
                ),
            )
            return bill_demurrage(placement, terms, through).amount

        assert bill_for(cars) <= bill_for(cars + 1)


class TestSettlementLaws:
    @given(quantities, rates)
    def test_the_net_is_the_gross_plus_the_deductions(self, quantity, price):
        settlement = (
            SettlementBuilder("S-1", "HALVORSEN FARMS", "CORN", OPENED, quantity)
            .grain(price)
            .discount("moisture", Price.from_cents("6"))
            .charge(ChargeKind.STORAGE, "storage", Money.of("12.00"))
            .build()
        )
        assert settlement.net == settlement.gross + settlement.deductions

    @given(quantities, rates)
    def test_deductions_are_never_a_credit(self, quantity, price):
        settlement = (
            SettlementBuilder("S-1", "HALVORSEN FARMS", "CORN", OPENED, quantity)
            .grain(price)
            .charge(ChargeKind.HAULING, "hauling", Money.of("30.00"))
            .build()
        )
        assert settlement.deductions.cents <= 0

    @given(quantities, rates)
    def test_the_lines_always_add_to_the_net(self, quantity, price):
        settlement = (
            SettlementBuilder("S-1", "HALVORSEN FARMS", "CORN", OPENED, quantity)
            .grain(price)
            .discount("moisture", Price.from_cents("6"))
            .discount("damage", Price.from_cents("2"))
            .build()
        )
        running = Money.zero()
        for line in settlement.lines:
            running = running + line.amount
        assert running == settlement.net
