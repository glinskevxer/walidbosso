from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import bushels
from binyard.drying import DryerRun, FuelUse, RunStatus
from binyard.errors import Conflict, NotFound, ValidationError
from binyard.inventory import MovementKind, Yard
from binyard.money import Price, dollars
from binyard.percent import percent
from binyard.services import abandon_run, finish_run, start_run
from binyard.shrink import ShrinkPolicy
from binyard.store import MemoryUnitOfWork, SqliteUnitOfWork
from conftest import bin_for, sample_for

MORNING = datetime(2025, 10, 14, 6, 0)
EVENING = datetime(2025, 10, 14, 18, 0)
POLICY = ShrinkPolicy.market_default(commodity.CORN)


def run(**overrides) -> DryerRun:
    fields: dict[str, object] = {
        "identifier": "DR-1",
        "commodity_code": "CORN",
        "source_bin": "WET",
        "destination_bin": "DRY",
        "wet_bushels": bushels("5000"),
        "wet_moisture": percent("21.0"),
        "started_at": MORNING,
    }
    fields.update(overrides)
    return DryerRun(**fields)  # type: ignore[arg-type]


@pytest.fixture(params=["memory", "sqlite"])
def work(request):
    yard = Yard(
        bins=(
            bin_for("WET", capacity="200000"),
            bin_for("DRY", capacity="200000"),
        )
    )
    if request.param == "memory":
        unit = MemoryUnitOfWork(yard)
        unit.commit()
        yield unit
        return
    unit = SqliteUnitOfWork.open()
    unit.yard.save(yard)
    unit.commit()
    try:
        yield unit
    finally:
        unit.close()


def stock(work, quantity="20000", moisture="21.0"):
    yard = work.yard.load()
    yard = yard.receive(
        "WET",
        "CORN",
        bushels(quantity),
        sample_for(commodity.CORN, moisture=percent(moisture)),
        at=MORNING,
        reference="T-1",
        by="scalehouse",
    )
    work.yard.save(yard)
    work.commit()


class TestRunValidation:
    def test_a_run_needs_bushels(self):
        with pytest.raises(ValidationError):
            run(wet_bushels=bushels("0"))

    def test_a_run_has_to_come_out_somewhere_else(self):
        with pytest.raises(ValidationError):
            run(destination_bin="WET")

    def test_grain_does_not_come_out_wetter(self):
        with pytest.raises(ValidationError) as caught:
            run(dry_moisture=percent("22.0"))
        assert caught.value.detail["wet"] == "21%"

    def test_more_cannot_come_out_than_went_in(self):
        with pytest.raises(ValidationError):
            run(dry_bushels=bushels("5001"))

    def test_a_run_cannot_finish_before_it_starts(self):
        with pytest.raises(ValidationError):
            run(finished_at=MORNING - timedelta(hours=1))

    def test_negative_fuel_is_refused(self):
        with pytest.raises(ValidationError):
            FuelUse(gallons=Decimal("-1"), price_per_gallon=Price.of("1.80"))


class TestRunLifecycle:
    def test_a_loaded_run_can_be_lit(self):
        lit = run().lit(MORNING)
        assert lit.status is RunStatus.DRYING
        assert lit.is_open

    def test_only_a_loaded_run_can_be_lit(self):
        with pytest.raises(ValidationError):
            run().lit(MORNING).lit(MORNING)

    def test_finishing_records_what_came_out(self):
        finished = (
            run()
            .lit(MORNING)
            .finished(
                dry_bushels=bushels("4700"),
                dry_moisture=percent("15.0"),
                at=EVENING,
                fuel=FuelUse(gallons=Decimal("620"), price_per_gallon=Price.of("1.80")),
            )
        )
        assert finished.status is RunStatus.FINISHED
        assert not finished.is_open
        assert finished.points_removed == percent("6.0")
        assert finished.actual_shrink == bushels("300")
        assert finished.fuel.cost == dollars("1116.00")

    def test_only_a_running_dryer_can_finish(self):
        with pytest.raises(ValidationError):
            run().finished(
                dry_bushels=bushels("4700"), dry_moisture=percent("15.0"), at=EVENING
            )

    def test_abandoning_needs_a_reason(self):
        with pytest.raises(ValidationError):
            run().lit(MORNING).abandoned(
                "  ", dry_bushels=bushels("100"), dry_moisture=percent("18.0"), at=EVENING
            )

    def test_an_abandoned_run_keeps_what_came_out(self):
        stopped = (
            run()
            .lit(MORNING)
            .abandoned(
                "burner tripped",
                dry_bushels=bushels("1200"),
                dry_moisture=percent("18.5"),
                at=EVENING,
            )
        )
        assert stopped.status is RunStatus.ABANDONED
        assert stopped.note == "burner tripped"
        assert stopped.dry_bushels == bushels("1200")


class TestVariance:
    def test_the_formula_and_the_dryer_are_compared(self):
        finished = (
            run()
            .lit(MORNING)
            .finished(dry_bushels=bushels("4650"), dry_moisture=percent("15.0"), at=EVENING)
        )
        variance = finished.variance(POLICY)
        assert variance is not None
        assert variance.expected_out == run().lit(MORNING).expected_out(POLICY)
        assert variance.difference == variance.expected_out - bushels("4650")

    def test_a_dryer_that_gave_back_more_than_the_formula_favours_the_house(self):
        finished = (
            run()
            .lit(MORNING)
            .finished(dry_bushels=bushels("4990"), dry_moisture=percent("15.0"), at=EVENING)
        )
        variance = finished.variance(POLICY)
        assert variance.favours_the_house
        assert variance.rate() < 0

    def test_an_unfinished_run_has_no_variance(self):
        assert run().lit(MORNING).variance(POLICY) is None

    def test_the_fuel_rate_is_gallons_per_point_per_bushel(self):
        finished = (
            run()
            .lit(MORNING)
            .finished(
                dry_bushels=bushels("4700"),
                dry_moisture=percent("15.0"),
                at=EVENING,
                fuel=FuelUse(gallons=Decimal("600"), price_per_gallon=Price.of("1.80")),
            )
        )
        # 600 gallons over 5000 bushels and six points.
        assert finished.fuel_per_point_bushel() == Decimal("0.020000")

    def test_no_fuel_means_no_rate(self):
        finished = (
            run()
            .lit(MORNING)
            .finished(dry_bushels=bushels("4700"), dry_moisture=percent("15.0"), at=EVENING)
        )
        assert finished.fuel_per_point_bushel() is None

    def test_the_run_renders_with_its_variance(self):
        finished = (
            run()
            .lit(MORNING)
            .finished(dry_bushels=bushels("4700"), dry_moisture=percent("15.0"), at=EVENING)
        )
        described = finished.describe(POLICY)
        assert described["points_removed"] == "6%"
        assert described["variance"]["actual_out"] == "4700.00 bu"


class TestService:
    def test_lighting_a_run_takes_the_grain_out_of_the_wet_bin(self, work):
        stock(work)
        lit = start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        assert lit.status is RunStatus.DRYING
        assert work.yard.load().contents_of("WET").quantity == bushels("15000")
        assert work.yard.load().contents_of("DRY").is_empty

    def test_a_run_off_an_empty_bin_is_refused(self, work):
        with pytest.raises(ValidationError):
            start_run(
                work,
                identifier="DR-1",
                source_bin="WET",
                destination_bin="DRY",
                quantity=bushels("5000"),
                at=MORNING,
                by="dryer",
            )

    def test_two_runs_cannot_share_an_identifier(self, work):
        stock(work)
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        with pytest.raises(Conflict):
            start_run(
                work,
                identifier="DR-1",
                source_bin="WET",
                destination_bin="DRY",
                quantity=bushels("1000"),
                at=MORNING,
                by="dryer",
            )

    def test_finishing_fills_the_dry_bin_and_posts_the_loss(self, work):
        stock(work)
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        outcome = finish_run(
            work,
            identifier="DR-1",
            dry_bushels=bushels("4700"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=EVENING,
            by="dryer",
        )
        assert outcome.posted_loss == bushels("300")
        assert work.yard.load().contents_of("DRY").quantity == bushels("4700")
        kinds = [entry.kind for entry in work.yard.load().ledger.for_bin("DRY")]
        assert MovementKind.DRYER_LOSS in kinds

    def test_the_dry_bin_carries_the_new_moisture(self, work):
        stock(work, moisture="21.0")
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        finish_run(
            work,
            identifier="DR-1",
            dry_bushels=bushels("4700"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=EVENING,
            by="dryer",
        )
        assert work.yard.load().contents_of("DRY").quality.moisture == percent("15.0")

    def test_nothing_is_lost_across_the_whole_run(self, work):
        stock(work, quantity="20000")
        before = work.yard.load().total_of("CORN")
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        outcome = finish_run(
            work,
            identifier="DR-1",
            dry_bushels=bushels("4700"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=EVENING,
            by="dryer",
        )
        after = work.yard.load().total_of("CORN")
        assert before - after == outcome.posted_loss

    def test_finishing_a_run_that_is_not_running(self, work):
        stock(work)
        with pytest.raises(NotFound):
            finish_run(
                work,
                identifier="DR-9",
                dry_bushels=bushels("1"),
                dry_moisture=percent("15.0"),
                standard=commodity.CORN,
                at=EVENING,
                by="dryer",
            )

    def test_finishing_against_the_wrong_crop(self, work):
        stock(work)
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        with pytest.raises(ValidationError):
            finish_run(
                work,
                identifier="DR-1",
                dry_bushels=bushels("4700"),
                dry_moisture=percent("15.0"),
                standard=commodity.SOYBEANS,
                at=EVENING,
                by="dryer",
            )

    def test_abandoning_puts_what_came_out_away(self, work):
        stock(work)
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        stopped = abandon_run(
            work,
            identifier="DR-1",
            reason="burner tripped",
            dry_bushels=bushels("1200"),
            dry_moisture=percent("18.5"),
            at=EVENING,
            by="dryer",
        )
        assert stopped.status is RunStatus.ABANDONED
        assert work.yard.load().contents_of("DRY").quantity == bushels("1200")

    def test_open_runs_are_listed_until_they_finish(self, work):
        stock(work)
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        assert [item.identifier for item in work.dryer.open_runs()] == ["DR-1"]
        finish_run(
            work,
            identifier="DR-1",
            dry_bushels=bushels("4700"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=EVENING,
            by="dryer",
        )
        assert work.dryer.open_runs() == ()

    def test_a_run_round_trips_through_the_store(self, work):
        stock(work)
        start_run(
            work,
            identifier="DR-1",
            source_bin="WET",
            destination_bin="DRY",
            quantity=bushels("5000"),
            at=MORNING,
            by="dryer",
        )
        finish_run(
            work,
            identifier="DR-1",
            dry_bushels=bushels("4700"),
            dry_moisture=percent("15.0"),
            standard=commodity.CORN,
            at=EVENING,
            by="dryer",
            fuel=FuelUse(gallons=Decimal("612.5"), price_per_gallon=Price.of("1.80")),
        )
        fetched = work.dryer.get("DR-1")
        assert fetched.dry_bushels == bushels("4700")
        assert fetched.fuel.gallons == Decimal("612.5")
        assert fetched.fuel.cost == dollars("1102.50")
