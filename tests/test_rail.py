from datetime import date, datetime

import pytest

from binyard.errors import LoadoutError, ValidationError
from binyard.loadout import (
    CarStatus,
    DemurrageTerms,
    Placement,
    RailCar,
    bill_demurrage,
)
from binyard.money import Price, dollars

SPOTTED = datetime(2026, 1, 5, 16, 30)  # a Monday afternoon
SATURDAY = date(2026, 1, 10)
NEW_YEAR = date(2026, 1, 1)


def terms(**overrides) -> DemurrageTerms:
    fields: dict[str, object] = {
        "free_days": 3,
        "per_car_day": Price.of("150.00"),
    }
    fields.update(overrides)
    return DemurrageTerms(**fields)  # type: ignore[arg-type]


def car(mark: str = "BNSF 460101", **overrides) -> RailCar:
    fields: dict[str, object] = {"reporting_mark": mark, "placed_at": SPOTTED}
    fields.update(overrides)
    return RailCar(**fields)  # type: ignore[arg-type]


def placement(*cars: RailCar) -> Placement:
    return Placement(
        identifier="PL-1",
        order_identifier="LO-1",
        cars=cars or (car(),),
    )


class TestTerms:
    def test_negative_terms_are_refused(self):
        with pytest.raises(ValidationError):
            terms(free_days=-1)
        with pytest.raises(ValidationError):
            terms(per_car_day=Price(-1))
        with pytest.raises(ValidationError):
            terms(credit_days_per_car=-1)

    def test_a_weekday_outside_the_week_is_refused(self):
        with pytest.raises(ValidationError):
            terms(non_computing_weekdays=frozenset({7}))

    def test_every_day_counts_by_default(self):
        assert terms().computing_days(date(2026, 1, 5), date(2026, 1, 11)) == 7

    def test_a_tariff_can_skip_the_weekend(self):
        weekdays_only = terms(non_computing_weekdays=frozenset({5, 6}))
        assert weekdays_only.computing_days(date(2026, 1, 5), date(2026, 1, 11)) == 5

    def test_a_holiday_does_not_count(self):
        holiday = terms(holidays=frozenset({NEW_YEAR}))
        assert holiday.computing_days(date(2025, 12, 31), date(2026, 1, 2)) == 2

    def test_a_backwards_span_counts_nothing(self):
        assert terms().computing_days(date(2026, 1, 11), date(2026, 1, 5)) == 0


class TestCars:
    def test_a_car_needs_a_reporting_mark(self):
        with pytest.raises(ValidationError):
            car(mark="  ")

    def test_a_car_cannot_be_released_before_it_was_placed(self):
        with pytest.raises(ValidationError):
            car(released_at=datetime(2026, 1, 4, 8, 0), status=CarStatus.RELEASED)

    def test_a_finished_car_needs_a_release_time(self):
        with pytest.raises(ValidationError):
            car(status=CarStatus.RELEASED)

    def test_a_placed_car_can_start_loading(self):
        assert car().loading().status is CarStatus.LOADING

    def test_only_a_placed_car_can_start_loading(self):
        with pytest.raises(LoadoutError):
            car().loading().loading()

    def test_a_released_car_cannot_be_released_again(self):
        released = car().released(datetime(2026, 1, 7, 9, 0))
        with pytest.raises(LoadoutError):
            released.released(datetime(2026, 1, 8, 9, 0))

    def test_rejecting_a_car_needs_a_reason(self):
        with pytest.raises(ValidationError):
            car().rejected("  ", datetime(2026, 1, 6, 9, 0))

    def test_a_rejected_car_keeps_its_reason(self):
        refused = car().rejected("wrong grade", datetime(2026, 1, 6, 9, 0))
        assert refused.status is CarStatus.REJECTED
        assert refused.note == "wrong grade"


class TestTheClock:
    def test_the_clock_starts_the_day_after_placement(self):
        # Placed Monday, released Thursday: Tuesday, Wednesday and Thursday.
        released = car().released(datetime(2026, 1, 8, 9, 0))
        assert released.chargeable_days(terms(free_days=3), SATURDAY) == 0

    def test_a_day_past_the_free_period_is_chargeable(self):
        released = car().released(datetime(2026, 1, 9, 9, 0))
        assert released.chargeable_days(terms(free_days=3), SATURDAY) == 1

    def test_an_outstanding_car_is_charged_up_to_the_billing_date(self):
        assert car().chargeable_days(terms(free_days=3), date(2026, 1, 12)) == 4

    def test_the_time_of_day_does_not_matter(self):
        early = car(placed_at=datetime(2026, 1, 5, 6, 0))
        late = car(placed_at=datetime(2026, 1, 5, 23, 30))
        assert early.chargeable_days(terms(), SATURDAY) == late.chargeable_days(
            terms(), SATURDAY
        )

    def test_a_tariff_that_skips_weekends_charges_less(self):
        every_day = terms(free_days=3)
        weekdays = terms(free_days=3, non_computing_weekdays=frozenset({5, 6}))
        outstanding = car()
        assert outstanding.chargeable_days(weekdays, date(2026, 1, 12)) < (
            outstanding.chargeable_days(every_day, date(2026, 1, 12))
        )

    def test_releasing_early_earns_a_credit(self):
        released = car().released(datetime(2026, 1, 6, 9, 0))
        assert released.credit_days(terms(free_days=3)) == 1

    def test_a_credit_is_capped_by_the_tariff(self):
        released = car().released(datetime(2026, 1, 6, 9, 0))
        assert released.credit_days(terms(free_days=10, credit_days_per_car=1)) == 1
        assert released.credit_days(terms(free_days=10, credit_days_per_car=4)) == 4

    def test_an_outstanding_car_earns_no_credit(self):
        assert car().credit_days(terms()) == 0


class TestPlacements:
    def test_a_placement_needs_cars(self):
        with pytest.raises(ValidationError):
            Placement(identifier="PL-1", order_identifier="LO-1", cars=())

    def test_a_car_cannot_appear_twice(self):
        with pytest.raises(ValidationError):
            placement(car("A"), car("A"))

    def test_a_car_can_be_looked_up_by_mark(self):
        held = placement(car("A"), car("B"))
        assert held.car("B").reporting_mark == "B"

    def test_a_car_that_is_not_there(self):
        with pytest.raises(LoadoutError):
            placement(car("A")).car("Z")

    def test_replacing_a_car_leaves_the_rest_alone(self):
        held = placement(car("A"), car("B"))
        moved = held.with_car(held.car("A").loading())
        assert moved.car("A").status is CarStatus.LOADING
        assert moved.car("B").status is CarStatus.PLACED

    def test_outstanding_cars_are_the_ones_still_here(self):
        held = placement(car("A"), car("B"))
        held = held.with_car(held.car("A").released(datetime(2026, 1, 7, 9, 0)))
        assert [item.reporting_mark for item in held.outstanding] == ["B"]


class TestBilling:
    def test_a_placement_released_inside_free_time_owes_nothing(self):
        held = placement(*[car(f"C{index}") for index in range(5)])
        held = Placement(
            identifier="PL-1",
            order_identifier="LO-1",
            cars=tuple(item.released(datetime(2026, 1, 7, 9, 0)) for item in held.cars),
        )
        bill = bill_demurrage(held, terms(free_days=3), SATURDAY)
        assert bill.is_clean
        assert bill.amount == dollars("0")

    def test_a_late_placement_is_billed_per_car_day(self):
        held = placement(*[car(f"C{index}") for index in range(3)])
        bill = bill_demurrage(held, terms(free_days=3), date(2026, 1, 11))
        assert bill.debit_days == 9
        assert bill.credit_days == 0
        assert bill.amount == dollars("1350.00")

    def test_credits_net_against_debits_across_the_placement(self):
        early = car("EARLY").released(datetime(2026, 1, 6, 9, 0))
        late = car("LATE")
        bill = bill_demurrage(placement(early, late), terms(free_days=3), date(2026, 1, 10))
        assert bill.credit_days == 1
        assert bill.net_days == bill.debit_days - 1

    def test_credits_never_take_a_bill_below_nothing(self):
        early = car("EARLY").released(datetime(2026, 1, 6, 9, 0))
        second = car("ALSO EARLY").released(datetime(2026, 1, 6, 9, 0))
        bill = bill_demurrage(placement(early, second), terms(free_days=3), date(2026, 1, 20))
        assert bill.credit_days == 2
        assert bill.net_days == 0
        assert bill.amount == dollars("0")

    def test_a_rejected_car_still_ran_the_clock(self):
        refused = car("BAD").rejected("wrong grade", datetime(2026, 1, 11, 9, 0))
        bill = bill_demurrage(placement(refused), terms(free_days=3), date(2026, 1, 12))
        assert bill.debit_days == 3

    def test_the_bill_renders(self):
        held = placement(*[car(f"C{index}") for index in range(3)])
        described = bill_demurrage(held, terms(free_days=3), date(2026, 1, 11)).describe()
        assert described["net_days"] == 9
        assert described["amount"] == "$1,350.00"
        assert described["per_car_day"] == "$150"


def test_a_shuttle_train_bills_the_whole_string_at_once():
    cars = tuple(car(f"BNSF 4601{index:02d}") for index in range(110))
    held = Placement(identifier="PL-SHUTTLE", order_identifier="LO-9", cars=cars)
    bill = bill_demurrage(held, terms(free_days=2), date(2026, 1, 9))
    assert bill.debit_days == 110 * 2
    assert bill.amount == dollars("33000.00")
