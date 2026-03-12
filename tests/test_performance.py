"""Guards on the things that could quietly become slow.

None of these is a benchmark in the sense of a number to optimise. Each one is
a ceiling generous enough that ordinary variation between machines cannot trip
it, and tight enough that an accidental quadratic cannot hide under it.

The ceilings are stated in terms of what the operation is, not in seconds
alone. A blend over forty bins is a real question at a large elevator; a fold
over a season of movements happens on every page load of the bin board. If one
of these starts failing, the fix is the algorithm, not the ceiling.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.blend.planner import Objective, plan_blend
from binyard.blend.spec import BlendSource, BlendSpec, SpecLimit
from binyard.bushels import bushels
from binyard.grading.factors import FactorName, FactorSample
from binyard.grading.grade import determine
from binyard.inventory import Yard
from binyard.percent import percent
from binyard.store import SqliteUnitOfWork
from conftest import bin_for, sample_for

pytestmark = pytest.mark.benchmark

SEASON_START = datetime(2025, 9, 22, 7, 0)


def elapsed(work) -> float:
    started = time.perf_counter()
    work()
    return time.perf_counter() - started


def season_yard(bins: int = 8, loads: int = 900) -> Yard:
    yard = Yard(bins=tuple(bin_for(f"B{index}", capacity="400000") for index in range(bins)))
    at = SEASON_START
    quality = sample_for(commodity.CORN)
    for index in range(loads):
        yard = yard.receive(
            f"B{index % bins}",
            "CORN",
            bushels("860.71"),
            quality,
            at=at,
            reference=f"WBS-25-{index:06d}",
            by="scalehouse",
        )
        at += timedelta(minutes=7)
    return yard


class TestFolding:
    @pytest.mark.slow
    def test_a_season_of_movements_folds_in_well_under_a_second(self):
        yard = season_yard()
        assert len(yard.ledger) == 900
        assert elapsed(lambda: yard.states()) < 2.0

    @pytest.mark.slow
    def test_reading_history_is_no_slower_than_reading_now(self):
        yard = season_yard(loads=600)
        now = elapsed(lambda: yard.states())
        history = elapsed(lambda: yard.states(when=SEASON_START + timedelta(days=10)))
        assert history < now * 4 + 0.5

    def test_posting_a_movement_does_not_walk_the_whole_ledger_twice(self):
        small = season_yard(bins=2, loads=100)
        large = season_yard(bins=2, loads=400)
        one = elapsed(
            lambda: small.receive(
                "B0",
                "CORN",
                bushels("100"),
                sample_for(commodity.CORN),
                at=SEASON_START + timedelta(days=30),
                reference="X",
                by="t",
            )
        )
        four = elapsed(
            lambda: large.receive(
                "B0",
                "CORN",
                bushels("100"),
                sample_for(commodity.CORN),
                at=SEASON_START + timedelta(days=30),
                reference="X",
                by="t",
            )
        )
        # Four times the ledger should not be sixteen times the work.
        assert four < one * 10 + 0.05


class TestBlending:
    def _sources(self, count: int) -> tuple[BlendSource, ...]:
        return tuple(
            BlendSource(
                identifier=f"B{index}",
                quality=FactorSample(
                    moisture=percent(f"{13 + index % 9}.{index % 10}"),
                    test_weight=Decimal(54 + index % 6),
                    total_damage=percent(f"{index % 5}.0"),
                ),
                available=bushels(str(20_000 + index * 137)),
            )
            for index in range(count)
        )

    def test_a_forty_bin_blend_solves_quickly(self):
        spec = BlendSpec(
            commodity_code="CORN",
            quantity=bushels("400000"),
            limits=(
                SpecLimit(FactorName.MOISTURE, Decimal("17.0")),
                SpecLimit(FactorName.TEST_WEIGHT, Decimal("55.0")),
                SpecLimit(FactorName.TOTAL_DAMAGE, Decimal("3.0")),
            ),
        )
        sources = self._sources(40)
        taken = elapsed(lambda: plan_blend(spec, sources, objective=Objective.SHIP_WORST_FIRST))
        assert taken < 5.0

    def test_the_solver_does_not_blow_up_between_ten_and_thirty_bins(self):
        def spec_for(bins: int) -> BlendSpec:
            # Half of what the bins hold, so both sizes are comfortably feasible
            # and the only thing that changes between them is the column count.
            return BlendSpec(
                commodity_code="CORN",
                quantity=bushels(str(10_000 * bins)),
                limits=(SpecLimit(FactorName.MOISTURE, Decimal("17.0")),),
            )

        ten = elapsed(lambda: plan_blend(spec_for(10), self._sources(10)))
        thirty = elapsed(lambda: plan_blend(spec_for(30), self._sources(30)))
        assert thirty < ten * 40 + 1.0

    def test_a_solved_blend_is_still_correct_at_forty_bins(self):
        spec = BlendSpec(
            commodity_code="CORN",
            quantity=bushels("400000"),
            limits=(SpecLimit(FactorName.MOISTURE, Decimal("17.0")),),
        )
        plan = plan_blend(spec, self._sources(40))
        assert plan.quantity == bushels("400000")
        assert spec.accepts_average(plan.quality) == ()


class TestGrading:
    def test_grading_ten_thousand_samples_stays_linear(self):
        samples = [
            FactorSample(
                moisture=percent("15.0"),
                test_weight=Decimal(50 + index % 10),
                total_damage=percent(f"{index % 12}.0"),
                broken=percent(f"{index % 6}.0"),
            )
            for index in range(10_000)
        ]
        taken = elapsed(lambda: [determine(sample, commodity.CORN) for sample in samples])
        assert taken < 5.0


class TestStorage:
    @pytest.mark.slow
    def test_saving_a_season_and_loading_it_back(self):
        yard = season_yard(loads=800)
        work = SqliteUnitOfWork.open()
        try:
            assert elapsed(lambda: work.yard.save(yard)) < 5.0
            work.commit()
            loaded = None

            def load() -> None:
                nonlocal loaded
                loaded = work.yard.load()

            assert elapsed(load) < 5.0
            assert loaded is not None
            assert len(loaded.ledger) == len(yard.ledger)
        finally:
            work.close()
