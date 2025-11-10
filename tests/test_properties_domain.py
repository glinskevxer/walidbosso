"""Laws the grading, commingling and blending code has to obey.

These are the invariants that are obvious once written down and easy to break
by accident. A grade that improves when a defect gets worse, a bin whose
moisture lands outside both of the lots that made it, a blend that fills the
order but misses the spec: none of those would be caught by an example test
unless somebody guessed the example.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from binyard import commodity
from binyard.blend.planner import Objective, plan_blend
from binyard.blend.spec import BlendSource, BlendSpec, SpecLimit
from binyard.bushels import Bushels
from binyard.discounts.posted import CORN_SCHEDULE
from binyard.discounts.apply import assess
from binyard.errors import InfeasibleBlend
from binyard.grading.factors import FactorName, FactorSample
from binyard.grading.grade import determine
from binyard.money import Money
from binyard.inventory.bin import commingle
from binyard.percent import Percent

QUIET = settings(
    max_examples=150, suppress_health_check=[HealthCheck.function_scoped_fixture]
)

test_weights = st.decimals(
    min_value=Decimal("35"), max_value=Decimal("65"), places=1, allow_nan=False
)
small_percents = st.integers(min_value=0, max_value=1500).map(Percent)


def corn_sample(moisture, test_weight, damage, broken):
    return FactorSample(
        moisture=moisture,
        test_weight=test_weight,
        total_damage=damage,
        heat_damage=Percent.zero(),
        broken=broken,
    )


samples = st.builds(
    corn_sample,
    moisture=st.integers(min_value=800, max_value=2400).map(Percent),
    test_weight=test_weights,
    damage=small_percents,
    broken=small_percents,
)


class TestGradingLaws:
    @given(samples)
    def test_a_grade_is_a_listed_one_or_sample_grade(self, sample):
        result = determine(sample, commodity.CORN)
        assert result.grade in {0, 1, 2, 3, 4, 5}

    @given(samples, st.integers(min_value=1, max_value=500))
    def test_more_damage_never_improves_the_grade(self, sample, extra):
        worse = FactorSample(
            moisture=sample.moisture,
            test_weight=sample.test_weight,
            total_damage=Percent(min(sample.total_damage.hundredths + extra, 10_000)),
            heat_damage=sample.heat_damage,
            broken=sample.broken,
        )
        first = determine(sample, commodity.CORN)
        second = determine(worse, commodity.CORN)
        assert _rank(second) >= _rank(first)

    @given(samples, st.decimals(min_value=Decimal("0.1"), max_value=Decimal("5"), places=1))
    def test_heavier_test_weight_never_worsens_the_grade(self, sample, extra):
        assume(sample.test_weight + extra < Decimal("100"))
        better = FactorSample(
            moisture=sample.moisture,
            test_weight=sample.test_weight + extra,
            total_damage=sample.total_damage,
            heat_damage=sample.heat_damage,
            broken=sample.broken,
        )
        assert _rank(determine(better, commodity.CORN)) <= _rank(
            determine(sample, commodity.CORN)
        )

    @given(samples)
    def test_a_graded_sample_names_what_held_it_back_unless_it_is_the_best(self, sample):
        result = determine(sample, commodity.CORN)
        if result.grade == 1:
            assert result.limiting == ()
        elif not result.is_sample_grade:
            assert result.limiting != ()

    @settings(deadline=None)
    @given(samples, st.integers(min_value=1, max_value=10**7).map(Bushels))
    def test_the_charge_is_the_printed_lines_added_up(self, sample, quantity):
        result = determine(sample, commodity.CORN)
        found = assess(result, quantity, CORN_SCHEDULE)
        assume(not found.rejected)
        running = Money.zero()
        for line in found.lines:
            running = running + line.amount
        assert found.total == running

    @settings(deadline=None)
    @given(samples, st.integers(min_value=1, max_value=10**7).map(Bushels))
    def test_the_charge_is_within_a_cent_a_line_of_the_summed_rate(self, sample, quantity):
        result = determine(sample, commodity.CORN)
        found = assess(result, quantity, CORN_SCHEDULE)
        assume(not found.rejected)
        gap = abs(found.total.cents - found.total_rate.extend(quantity).cents)
        assert gap <= max(len(found.lines), 1)


class TestComminglingLaws:
    @given(samples, samples, st.integers(1, 10**6), st.integers(1, 10**6))
    def test_moisture_lands_between_the_two_lots(self, one, two, left, right):
        blended = commingle((one, left), (two, right))
        assert min(one.moisture, two.moisture) <= blended.moisture
        assert blended.moisture <= max(one.moisture, two.moisture)

    @given(samples, samples, st.integers(1, 10**6), st.integers(1, 10**6))
    def test_test_weight_lands_between_the_two_lots(self, one, two, left, right):
        blended = commingle((one, left), (two, right))
        low = min(one.test_weight, two.test_weight)
        high = max(one.test_weight, two.test_weight)
        assert low - Decimal("0.1") <= blended.test_weight <= high + Decimal("0.1")

    @given(samples, st.integers(1, 10**6), st.integers(1, 10**6))
    def test_commingling_a_lot_with_itself_changes_nothing(self, sample, left, right):
        blended = commingle((sample, left), (sample, right))
        assert blended.moisture == sample.moisture
        assert blended.test_weight == sample.test_weight

    @given(samples, samples, st.integers(1, 10**6), st.integers(1, 10**6))
    def test_infestation_survives_any_dilution(self, one, two, left, right):
        infested = FactorSample(
            moisture=one.moisture, test_weight=one.test_weight, infested=True
        )
        assert commingle((infested, left), (two, right)).infested


class TestBlendLaws:
    @given(
        st.lists(
            st.tuples(
                st.integers(min_value=1000, max_value=2400).map(Percent),
                st.integers(min_value=100_000, max_value=5_000_000).map(Bushels),
            ),
            min_size=1,
            max_size=6,
        ),
        st.integers(min_value=1000, max_value=2000).map(Percent),
        st.integers(min_value=10_000, max_value=2_000_000).map(Bushels),
    )
    @settings(max_examples=60, deadline=None)
    def test_a_plan_that_comes_back_fills_the_order_and_makes_the_spec(
        self, offers, ceiling, wanted
    ):
        sources = tuple(
            BlendSource(
                identifier=f"B{index}",
                quality=FactorSample(moisture=moisture, test_weight=Decimal("57.0")),
                available=available,
            )
            for index, (moisture, available) in enumerate(offers)
        )
        spec = BlendSpec(
            commodity_code="CORN",
            quantity=wanted,
            limits=(SpecLimit(FactorName.MOISTURE, ceiling.points),),
        )
        try:
            plan = plan_blend(spec, sources, objective=Objective.SHIP_WORST_FIRST)
        except InfeasibleBlend:
            return
        assert plan.quantity == wanted
        assert spec.accepts_average(plan.quality) == ()
        for source in sources:
            assert plan.draw_from(source.identifier) <= source.available


def _rank(result) -> int:
    """Sample grade is worse than every numbered grade."""
    return 99 if result.is_sample_grade else result.grade
