"""A season, end to end, through the real stores.

Every other module tests one thing. This one runs a harvest: trucks in off the
feed, wet corn through the dryer, a blend out to a terminal on rail, a
settlement, and the reports at the end. It asserts the two things that have to
be true when all of it is finished and that no single unit test can see.

**Nothing appears and nothing vanishes.** Bushels received, less bushels
shipped, less what the dryer kept, less what a measure-up wrote off, equals
what is in the bins.

**The house is not accidentally short.** Everything owed out is covered by
something on hand, on every day of the season and not only at the end.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.blend.planner import plan_blend
from binyard.blend.spec import BlendSpec, SpecLimit, sources_from
from binyard.bushels import Bushels, bushels
from binyard.contracts.contract import PriceKind, PurchaseContract
from binyard.discounts.posted import default_book
from binyard.feed.reader import read_feed
from binyard.grading.factors import FactorName
from binyard.identity import LocationCode, Sequence
from binyard.inventory import MovementKind, Yard
from binyard.loadout.carrier import AxleConfiguration, Carrier, CarrierKind
from binyard.loadout.order import LoadoutOrder
from binyard.money import Price
from binyard.percent import percent
from binyard.position import build_position
from binyard.services import (
    SettlementRequest,
    finish_run,
    import_feed,
    settle_tickets,
    ship_next_load,
    start_run,
)
from binyard.store import SqliteUnitOfWork
from binyard.units import pounds
from conftest import bin_for

LOCATION = LocationCode("WBS")
BOOK = default_book()
HARVEST = date(2025, 9, 22)


@pytest.fixture
def work():
    unit = SqliteUnitOfWork.open()
    yard = Yard(
        bins=(
            bin_for("N1", capacity="300000"),
            bin_for("N2", capacity="300000"),
            bin_for("WET", capacity="150000"),
            bin_for("DRY", capacity="300000"),
        )
    )
    unit.yard.save(yard)
    unit.contracts.add(
        PurchaseContract(
            identifier="C-2025-014",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=bushels("20000"),
            delivery_from=HARVEST,
            delivery_to=date(2025, 12, 31),
            price_kind=PriceKind.FLAT,
            flat_price=Price.of("4.5225"),
        )
    )
    unit.commit()
    try:
        yield unit
    finally:
        unit.close()


def feed_line(sequence: int, day: date, moisture: str, bin_code: str = "CORN") -> str:
    return (
        f"IN{sequence:06d}{day:%Y%m%d}{7 + sequence % 10:02d}{sequence % 60:02d}00"
        f"{'HALVORSEN FARMS':<20}{bin_code:<8}07842000030180{'00'}"
        f"{moisture:<4}{'0570':<4}{'TRAVIS':<12}"
    )


def receive_a_week(work, *, start: date, days: int, moistures: list[str]) -> Bushels:
    """Import a week of feed, one file a day, into the wet or the dry bin."""
    taken = Bushels.zero()
    sequence = Sequence(LOCATION, 2025)
    for offset in range(days):
        day = start + timedelta(days=offset)
        moisture = moistures[offset % len(moistures)]
        destination = "WET" if int(moisture) > 175 else "N1"
        text = "\n".join(feed_line(offset * 10 + index, day, moisture) for index in range(1, 5))
        result = import_feed(
            work,
            read_feed(text),
            sequence,
            source=f"scale-{day:%Y-%m-%d}.dat",
            book=BOOK,
            bin_for={"CORN": destination},
            at=datetime.combine(day, datetime.min.time()) + timedelta(hours=20),
        )
        assert result.rejected_lines == 0
        taken = taken + result.bushels_taken
    return taken


def total_of(work, code: str = "CORN") -> Bushels:
    return work.yard.load().total_of(code)


class TestASeason:
    def test_a_week_of_feed_lands_in_the_bins(self, work):
        taken = receive_a_week(work, start=HARVEST, days=5, moistures=["0150", "0195", "0160"])
        assert taken.is_positive()
        assert total_of(work) == taken
        assert len(work.tickets.open_tickets()) == 20

    def test_the_contract_fills_before_storage_opens(self, work):
        receive_a_week(work, start=HARVEST, days=2, moistures=["0150"])
        contract = work.contracts.get("C-2025-014")
        assert contract.delivered.is_positive()
        assert contract.delivered <= contract.quantity

    def test_drying_moves_the_wet_bin_into_the_dry_one_and_loses_only_water(self, work):
        receive_a_week(work, start=HARVEST, days=4, moistures=["0195"])
        before = total_of(work)
        wet = work.yard.load().contents_of("WET").quantity
        assert wet.is_positive()

        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=wet,
            at=datetime(2025, 9, 27, 6, 0),
            by="dryer",
        )
        outcome = finish_run(
            work,
            identifier="DR-1",
            dry_bushels=wet - bushels("200"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=datetime(2025, 9, 27, 20, 0),
            by="dryer",
        )
        after = total_of(work)
        assert before - after == outcome.posted_loss
        assert work.yard.load().contents_of("WET").is_empty
        assert work.yard.load().contents_of("DRY").quality.moisture == percent("15.0")

    def test_a_blend_out_of_the_dry_bins_makes_the_spec(self, work):
        receive_a_week(work, start=HARVEST, days=6, moistures=["0145", "0155"])
        spec = BlendSpec(
            commodity_code="CORN",
            quantity=bushels("5000"),
            limits=(
                SpecLimit(FactorName.MOISTURE, Decimal("15.5")),
                SpecLimit(FactorName.TEST_WEIGHT, Decimal("54.0")),
            ),
        )
        sources = sources_from(work.yard.load().states(), "CORN")
        plan = plan_blend(spec, sources)
        assert plan.quantity == bushels("5000")
        assert spec.accepts_average(plan.quality) == ()

    def test_shipping_draws_the_bins_and_the_obligations_together(self, work):
        receive_a_week(work, start=HARVEST, days=6, moistures=["0150"])
        before = total_of(work)
        owed = sum(item.balance.hundredths for item in work.obligations.all_open())

        order = LoadoutOrder(
            identifier="LO-1",
            destination="GULF TERMINAL",
            commodity_code="CORN",
            quantity=bushels("800"),
            ship_from=date(2025, 11, 1),
            ship_to=date(2025, 11, 30),
        )
        work.orders.add(order)
        work.commit()

        carrier = Carrier(
            identifier="T-14",
            kind=CarrierKind.TRUCK,
            empty_weight=pounds("31000"),
            axles=AxleConfiguration.TRACTOR_SEMI_5,
        )
        moved, ticket = ship_next_load(
            work,
            Sequence(LOCATION, 2025),
            order=order,
            standard=commodity.CORN,
            carrier=carrier,
            quantity=bushels("800"),
            draws=(("N1", bushels("800")),),
            on=date(2025, 11, 3),
            at=datetime(2025, 11, 3, 9, 0),
            by="loadout",
        )
        assert moved.shipped == bushels("800")
        assert ticket.kind.value == "outbound"
        assert before - total_of(work) == bushels("800")
        assert sum(i.balance.hundredths for i in work.obligations.all_open()) == owed - 80000

    def test_a_measure_up_shortfall_is_posted_rather_than_hidden(self, work):
        receive_a_week(work, start=HARVEST, days=3, moistures=["0150"])
        before = total_of(work)
        yard = work.yard.load().adjust(
            "N1",
            bushels("-350"),
            "annual measure up",
            at=datetime(2026, 1, 5, 9, 0),
            reference="MU-2026",
            by="manager",
        )
        work.yard.save(yard)
        work.commit()
        assert before - total_of(work) == bushels("350")
        kinds = [entry.kind for entry in work.yard.load().ledger.for_bin("N1")]
        assert MovementKind.ADJUSTMENT in kinds

    def test_settling_a_growers_tickets_closes_them(self, work):
        receive_a_week(work, start=HARVEST, days=2, moistures=["0150"])
        numbers = tuple(ticket.number for ticket in work.tickets.open_tickets())
        settlement = settle_tickets(
            work,
            SettlementRequest(
                number="S-2025-001",
                account="HALVORSEN FARMS",
                commodity_code="CORN",
                on=date(2025, 10, 1),
                ticket_numbers=numbers,
                price=Price.of("4.5225"),
                at=datetime(2025, 10, 1, 10, 0),
                by="accounting",
            ),
            book=BOOK,
        )
        assert settlement.quantity.is_positive()
        assert settlement.net.cents > 0
        assert work.tickets.open_tickets() == ()


class TestTheSeasonReconciles:
    def test_the_bins_hold_what_came_in_less_what_went_out(self, work):
        received = receive_a_week(
            work, start=HARVEST, days=8, moistures=["0150", "0195", "0165"]
        )

        wet = work.yard.load().contents_of("WET").quantity
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=wet,
            at=datetime(2025, 10, 2, 6, 0),
            by="dryer",
        )
        dryer = finish_run(
            work,
            identifier="DR-1",
            dry_bushels=wet - bushels("310"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=datetime(2025, 10, 2, 21, 0),
            by="dryer",
        )

        order = LoadoutOrder(
            identifier="LO-1",
            destination="GULF TERMINAL",
            commodity_code="CORN",
            quantity=bushels("1600"),
            ship_from=date(2025, 11, 1),
            ship_to=date(2025, 11, 30),
        )
        work.orders.add(order)
        work.commit()
        carrier = Carrier(
            identifier="T-14",
            kind=CarrierKind.TRUCK,
            empty_weight=pounds("31000"),
            axles=AxleConfiguration.TRACTOR_SEMI_5,
        )
        shipped = Bushels.zero()
        for index in range(2):
            order, _ = ship_next_load(
                work,
                Sequence(LOCATION, 2025),
                order=order,
                standard=commodity.CORN,
                carrier=carrier,
                quantity=bushels("800"),
                draws=(("N1", bushels("800")),),
                on=date(2025, 11, 3 + index),
                at=datetime(2025, 11, 3 + index, 9, 0),
                by="loadout",
            )
            shipped = shipped + bushels("800")

        yard = work.yard.load().adjust(
            "N1",
            bushels("-120"),
            "measure up",
            at=datetime(2026, 1, 5, 9, 0),
            reference="MU-2026",
            by="manager",
        )
        work.yard.save(yard)
        work.commit()

        on_hand = total_of(work)
        assert received - shipped - dryer.posted_loss - bushels("120") == on_hand

    def test_the_house_is_never_short_on_any_day_of_the_season(self, work):
        receive_a_week(work, start=HARVEST, days=10, moistures=["0150", "0165"])
        for offset in range(0, 12):
            day = HARVEST + timedelta(days=offset)
            report = build_position(
                on=day,
                location=str(LOCATION),
                yard=work.yard.load(),
                obligations=list(work.obligations.as_of(day)),
            )
            assert report.is_clean, f"short on {day}"

    def test_the_examiner_can_be_answered_for_any_day(self, work):
        receive_a_week(work, start=HARVEST, days=5, moistures=["0150"])
        for offset in (0, 2, 4, 30):
            day = HARVEST + timedelta(days=offset)
            report = build_position(
                on=day,
                location=str(LOCATION),
                yard=work.yard.load(),
                obligations=list(work.obligations.as_of(day)),
            )
            corn = report.position_for("CORN")
            if corn is not None:
                assert corn.measurable >= corn.owed_out
