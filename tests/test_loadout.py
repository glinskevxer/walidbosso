from datetime import date
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import bushels
from binyard.errors import LoadoutError, ValidationError
from binyard.loadout import (
    ArrivalReport,
    AxleConfiguration,
    Carrier,
    CarrierKind,
    LoadoutOrder,
    OrderStatus,
    Variance,
    capacity_bushels,
    capacity_for,
    fits,
    reconcile,
    split_into_loads,
)
from binyard.money import Price, dollars
from binyard.percent import percent
from binyard.units import pounds

SHIP_FROM = date(2025, 11, 1)
SHIP_TO = date(2025, 11, 30)


def truck(identifier="T-14", empty="31000", **overrides) -> Carrier:
    fields: dict[str, object] = {
        "identifier": identifier,
        "kind": CarrierKind.TRUCK,
        "empty_weight": pounds(empty),
        "axles": AxleConfiguration.TRACTOR_SEMI_5,
    }
    fields.update(overrides)
    return Carrier(**fields)  # type: ignore[arg-type]


def hopper(identifier="BNSF 460101", empty="65000", marked="222000") -> Carrier:
    return Carrier(
        identifier=identifier,
        kind=CarrierKind.RAIL_CAR,
        empty_weight=pounds(empty),
        marked_capacity=pounds(marked),
    )


def order(identifier="LO-1", quantity="50000", **overrides) -> LoadoutOrder:
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


class TestCarrierValidation:
    def test_a_truck_needs_an_axle_configuration(self):
        with pytest.raises(ValidationError):
            Carrier(identifier="T", kind=CarrierKind.TRUCK, empty_weight=pounds("31000"))

    def test_a_rail_car_needs_its_marked_capacity(self):
        with pytest.raises(ValidationError):
            Carrier(identifier="C", kind=CarrierKind.RAIL_CAR, empty_weight=pounds("65000"))

    def test_a_carrier_needs_an_identifier_and_a_weight(self):
        with pytest.raises(ValidationError):
            truck(identifier="  ")
        with pytest.raises(ValidationError):
            truck(empty="0")

    def test_a_volume_has_to_be_positive(self):
        with pytest.raises(ValidationError):
            truck(volume_cubic_feet=Decimal("0"))

    def test_a_carrier_over_its_limit_empty_is_an_error(self):
        heavy = truck(empty="90000")
        with pytest.raises(LoadoutError):
            heavy.payload_limit()


class TestCapacity:
    def test_a_five_axle_semi_runs_to_eighty_thousand_gross(self):
        assert truck().payload_limit() == pounds("49000")

    def test_a_permit_overrides_the_federal_limit(self):
        assert truck(permitted_gross=pounds("90000")).payload_limit() == pounds("59000")

    def test_a_rail_car_is_capped_by_what_is_stencilled_on_it(self):
        assert hopper().payload_limit() == pounds("222000")

    def test_weight_binds_on_corn_and_volume_binds_on_oats(self):
        light = truck(volume_cubic_feet=Decimal("1100"))
        assert capacity_for(light, commodity.CORN).binding == "weight"
        assert capacity_for(light, commodity.OATS).binding == "volume"

    def test_with_no_volume_given_weight_always_binds(self):
        check = capacity_for(truck(), commodity.OATS)
        assert check.binding == "weight"
        assert check.by_volume is None

    def test_capacity_in_bushels_matches_the_binding_limit(self):
        assert capacity_bushels(truck(), commodity.CORN) == bushels("875.00")

    def test_fits_agrees_with_the_capacity(self):
        assert fits(truck(), commodity.CORN, bushels("875"))
        assert not fits(truck(), commodity.CORN, bushels("876"))

    def test_the_check_renders(self):
        described = capacity_for(truck(volume_cubic_feet=Decimal("1100")), commodity.OATS).describe()
        assert described["binding"] == "volume"
        assert described["commodity"] == "OATS"


class TestOrders:
    def test_an_order_needs_a_destination_and_a_quantity(self):
        with pytest.raises(ValidationError):
            order(destination="  ")
        with pytest.raises(ValidationError):
            order(quantity="0")

    def test_a_window_that_ends_first_is_refused(self):
        with pytest.raises(ValidationError):
            order(ship_from=SHIP_TO, ship_to=SHIP_FROM)

    def test_adding_a_load_moves_it_to_loading(self):
        held = order().add_load("T-14", bushels("875"))
        assert held.status is OrderStatus.LOADING
        assert held.planned == bushels("875")
        assert held.shipped == bushels("0")

    def test_loads_are_numbered_from_one(self):
        held = order().add_load("T-14", bushels("875")).add_load("T-15", bushels("875"))
        assert [load.sequence for load in held.loads] == [1, 2]

    def test_planning_past_the_tolerance_is_refused(self):
        held = order(quantity="1000")
        with pytest.raises(LoadoutError) as caught:
            held.add_load("T-14", bushels("1001"))
        assert caught.value.detail["ceiling"] == "1000.00 bu"

    def test_a_tolerance_lets_a_little_over(self):
        held = order(quantity="1000", overship_tolerance=bushels("50"))
        assert held.add_load("T-14", bushels("1050")).planned == bushels("1050")

    def test_shipping_a_load_records_the_ticket(self):
        held = order(quantity="875").add_load("T-14", bushels("875"))
        held = held.ship_load(1, date(2025, 11, 3), "WBS-25-000900")
        assert held.shipped == bushels("875")
        assert held.status is OrderStatus.COMPLETE
        assert held.loads[0].ticket_number == "WBS-25-000900"

    def test_shipping_the_same_load_twice_is_refused(self):
        held = order(quantity="875").add_load("T-14", bushels("875"))
        held = held.ship_load(1, date(2025, 11, 3), "WBS-25-000900")
        with pytest.raises(LoadoutError):
            held.ship_load(1, date(2025, 11, 3), "WBS-25-000901")

    def test_shipping_a_load_that_is_not_there(self):
        with pytest.raises(LoadoutError):
            order().ship_load(9, date(2025, 11, 3), "WBS-25-000900")

    def test_a_complete_order_takes_no_more_loads(self):
        held = order(quantity="875").add_load("T-14", bushels("875"))
        held = held.ship_load(1, date(2025, 11, 3), "T")
        with pytest.raises(LoadoutError):
            held.add_load("T-15", bushels("1"))

    def test_an_order_with_shipments_cannot_be_cancelled(self):
        held = order(quantity="875").add_load("T-14", bushels("875"))
        held = held.ship_load(1, date(2025, 11, 3), "T")
        with pytest.raises(LoadoutError):
            held.cancelled()

    def test_cancelling_clears_the_planned_loads(self):
        held = order().add_load("T-14", bushels("875")).cancelled()
        assert held.loads == ()
        assert held.status is OrderStatus.CANCELLED

    def test_the_order_renders(self):
        described = order(quantity="875").add_load("T-14", bushels("875")).describe()
        assert described["remaining"] == "875.00 bu"
        assert described["loads"][0]["carrier"] == "T-14"


class TestSplitting:
    def test_only_as_many_carriers_as_the_quantity_needs(self):
        planned = split_into_loads(
            bushels("2000"), [truck("A"), truck("B"), truck("C"), truck("D")], commodity.CORN
        )
        assert [name for name, _ in planned] == ["A", "B", "C"]

    def test_the_loads_come_out_level_rather_than_full_then_empty(self):
        planned = split_into_loads(
            bushels("2000"), [truck("A"), truck("B"), truck("C")], commodity.CORN
        )
        amounts = [amount for _, amount in planned]
        assert max(amounts) - min(amounts) <= bushels("0.01")

    def test_the_split_adds_back_to_the_quantity(self):
        planned = split_into_loads(
            bushels("2000"), [truck("A"), truck("B"), truck("C")], commodity.CORN
        )
        total = bushels("0")
        for _, quantity in planned:
            total = total + quantity
        assert total == bushels("2000")

    def test_a_tail_that_would_have_been_tiny_is_levelled_away(self):
        planned = split_into_loads(
            bushels("900"),
            [truck("A"), truck("B")],
            commodity.CORN,
            minimum_load=bushels("100"),
        )
        assert planned == (("A", bushels("450")), ("B", bushels("450")))

    def test_a_mixed_fleet_never_gets_more_than_it_can_hold(self):
        small = truck("SMALL", empty="55000")
        planned = split_into_loads(
            bushels("1200"), [small, truck("BIG")], commodity.CORN
        )
        by_name = dict(planned)
        assert by_name["SMALL"] <= capacity_bushels(small, commodity.CORN)
        total = bushels("0")
        for _, amount in planned:
            total = total + amount
        assert total == bushels("1200")

    def test_a_minimum_that_cannot_be_met_is_an_error(self):
        with pytest.raises(LoadoutError) as caught:
            split_into_loads(
                bushels("1751"),
                [truck("A"), truck("B"), truck("C")],
                commodity.CORN,
                minimum_load=bushels("700"),
            )
        assert caught.value.detail["minimum"] == "700.00 bu"

    def test_not_enough_carriers_is_an_error(self):
        with pytest.raises(LoadoutError) as caught:
            split_into_loads(bushels("5000"), [truck("A")], commodity.CORN)
        assert caught.value.detail["short"] == "4125.00 bu"

    def test_no_carriers_at_all_is_an_error(self):
        with pytest.raises(LoadoutError):
            split_into_loads(bushels("100"), [], commodity.CORN)

    def test_nothing_to_split_is_refused(self):
        with pytest.raises(ValidationError):
            split_into_loads(bushels("0"), [truck("A")], commodity.CORN)


class TestReconciling:
    def _report(self, quantity="874.5", grade=None, **overrides):
        fields: dict[str, object] = {
            "order_identifier": "LO-1",
            "load_sequence": 1,
            "arrived_on": date(2025, 11, 5),
            "quantity": bushels(quantity),
            "grade": grade,
        }
        fields.update(overrides)
        return ArrivalReport(**fields)  # type: ignore[arg-type]

    def test_a_small_loss_is_inside_the_allowance(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(),
            allowance=percent("0.1"),
        )
        assert found.variance is Variance.WITHIN_TOLERANCE
        assert found.excess.is_zero()
        assert found.is_clean

    def test_a_big_loss_is_short_by_the_excess(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(quantity="860"),
            allowance=percent("0.1"),
        )
        assert found.variance is Variance.SHORT
        assert found.excess == bushels("14.12")
        assert not found.is_clean

    def test_arriving_heavy_reads_as_over(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(quantity="890"),
            allowance=percent("0.1"),
        )
        assert found.variance is Variance.OVER
        assert found.difference == bushels("-15")

    def test_the_loss_rate_reads_as_a_percentage(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("1000"),
            report=self._report(quantity="995"),
            allowance=percent("0.1"),
        )
        assert found.loss_rate == Decimal("0.5")

    def test_a_report_for_another_load_is_refused(self):
        with pytest.raises(LoadoutError):
            reconcile(
                order_identifier="LO-2",
                load_sequence=1,
                shipped=bushels("875"),
                report=self._report(),
                allowance=percent("0.1"),
            )

    def test_a_grade_slip_costs_the_discount_on_the_arrived_bushels(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(quantity="875", grade=3),
            allowance=percent("0.1"),
            grade_shipped=2,
            grade_discount=Price.from_cents("5"),
        )
        assert found.grade_slipped
        assert found.grade_charge == dollars("43.75")
        assert found.protest_closes_on == date(2025, 11, 10)

    def test_arriving_at_a_better_grade_costs_nothing(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(quantity="875", grade=1),
            allowance=percent("0.1"),
            grade_shipped=2,
            grade_discount=Price.from_cents("5"),
        )
        assert not found.grade_slipped
        assert found.grade_charge == dollars("0")
        assert found.protest_closes_on is None

    def test_arriving_at_sample_grade_is_always_a_slip(self):
        found = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(quantity="875", grade=0),
            allowance=percent("0.1"),
            grade_shipped=2,
            grade_discount=Price.from_cents("25"),
        )
        assert found.grade_slipped

    def test_shipping_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            reconcile(
                order_identifier="LO-1",
                load_sequence=1,
                shipped=bushels("0"),
                report=self._report(),
                allowance=percent("0.1"),
            )

    def test_the_reconciliation_renders(self):
        described = reconcile(
            order_identifier="LO-1",
            load_sequence=1,
            shipped=bushels("875"),
            report=self._report(quantity="860"),
            allowance=percent("0.1"),
        ).describe()
        assert described["variance"] == "short"
        assert described["clean"] is False
