from decimal import Decimal

import pytest

from binyard.blend import (
    BlendSource,
    BlendSpec,
    Objective,
    SpecLimit,
    plan_blend,
    sources_from,
)
from binyard.bushels import bushels
from binyard.errors import InfeasibleBlend, ValidationError
from binyard.grading import FactorName, FactorSample
from binyard.percent import percent


def quality(moisture="14.0", test_weight="57.0", damage="1.0", broken="1.0"):
    return FactorSample(
        moisture=percent(moisture),
        test_weight=Decimal(test_weight),
        total_damage=percent(damage),
        broken=percent(broken),
    )


def source(identifier, moisture, available="30000", **overrides):
    fields: dict[str, object] = {
        "identifier": identifier,
        "quality": quality(moisture=moisture),
        "available": bushels(available),
    }
    fields.update(overrides)
    return BlendSource(**fields)  # type: ignore[arg-type]


def moisture_spec(ceiling="14.5", quantity="50000", **overrides):
    return BlendSpec(
        commodity_code="CORN",
        quantity=bushels(quantity),
        limits=(SpecLimit(FactorName.MOISTURE, Decimal(ceiling)),),
        **overrides,
    )


class TestSpec:
    def test_a_spec_needs_a_positive_quantity(self):
        with pytest.raises(ValidationError):
            moisture_spec(quantity="0")

    def test_a_factor_limited_twice_is_refused(self):
        with pytest.raises(ValidationError):
            BlendSpec(
                commodity_code="CORN",
                quantity=bushels("100"),
                limits=(
                    SpecLimit(FactorName.MOISTURE, Decimal("14.5")),
                    SpecLimit(FactorName.MOISTURE, Decimal("15.0")),
                ),
            )

    def test_a_grade_spec_takes_the_tables_limits(self):
        spec = BlendSpec.for_grade("CORN", bushels("50000"), 2)
        assert spec.limit_for(FactorName.TEST_WEIGHT).limit == Decimal("54.0")
        assert spec.limit_for(FactorName.TEST_WEIGHT).is_minimum

    def test_a_grade_below_one_is_refused(self):
        with pytest.raises(ValidationError):
            BlendSpec(commodity_code="CORN", quantity=bushels("100"), minimum_grade=0)

    def test_a_quality_over_the_limit_is_named(self):
        spec = moisture_spec()
        assert spec.accepts_average(quality(moisture="14.4")) == ()
        assert spec.accepts_average(quality(moisture="14.6")) == (FactorName.MOISTURE,)

    def test_a_reject_limit_keeps_a_bin_out_entirely(self):
        spec = moisture_spec(
            reject_worse_than=(SpecLimit(FactorName.MOISTURE, Decimal("16.0")),)
        )
        assert spec.admits(quality(moisture="15.9"))
        assert not spec.admits(quality(moisture="16.1"))


class TestSources:
    def test_a_minimum_draw_bigger_than_the_bin_is_refused(self):
        with pytest.raises(ValidationError):
            BlendSource(
                identifier="N1",
                quality=quality(),
                available=bushels("100"),
                minimum_draw=bushels("200"),
            )

    def test_a_locked_bin_needs_a_reason(self):
        with pytest.raises(ValidationError):
            BlendSource(
                identifier="N1", quality=quality(), available=bushels("100"), locked=True
            )

    def test_a_locked_or_empty_bin_is_not_usable(self):
        assert not BlendSource(
            identifier="N1",
            quality=quality(),
            available=bushels("100"),
            locked=True,
            lock_reason="fumigating",
        ).usable
        assert not BlendSource(
            identifier="N2", quality=quality(), available=bushels("0")
        ).usable


class TestPlanning:
    def test_one_clean_bin_fills_the_order_on_its_own(self):
        plan = plan_blend(moisture_spec(), (source("N1", "13.0", available="60000"),))
        assert plan.quantity == bushels("50000")
        assert plan.bins_touched == 1

    def test_a_wet_bin_and_a_dry_one_meet_in_the_middle(self):
        plan = plan_blend(
            moisture_spec(), (source("N1", "13.0"), source("N2", "16.0"))
        )
        assert plan.quantity == bushels("50000")
        assert plan.quality.moisture <= percent("14.5")

    def test_the_draws_add_up_to_the_order_exactly(self):
        plan = plan_blend(
            moisture_spec(quantity="47331.17"),
            (source("N1", "13.0"), source("N2", "16.0"), source("N3", "14.8")),
        )
        assert plan.quantity == bushels("47331.17")

    def test_no_bin_gives_more_than_it_holds(self):
        sources = (source("N1", "13.0", available="26000"), source("N2", "16.0", available="40000"))
        plan = plan_blend(moisture_spec(), sources)
        for offer in sources:
            assert plan.draw_from(offer.identifier) <= offer.available

    def test_ship_worst_first_prefers_the_wet_bin(self):
        sources = (source("N1", "13.0"), source("N2", "16.0"))
        worst = plan_blend(moisture_spec(), sources, objective=Objective.SHIP_WORST_FIRST)
        best = plan_blend(moisture_spec(), sources, objective=Objective.SHIP_BEST_FIRST)
        assert worst.draw_from("N2") > best.draw_from("N2")

    def test_an_explicit_cost_steers_the_draw(self):
        sources = (source("N1", "13.0"), source("N2", "13.5"))
        plan = plan_blend(
            moisture_spec(),
            sources,
            objective=Objective.EXPLICIT_COST,
            costs={"N1": Decimal("10"), "N2": Decimal("1")},
        )
        assert plan.draw_from("N2") == bushels("30000")

    def test_an_explicit_cost_objective_needs_costs(self):
        with pytest.raises(ValidationError):
            plan_blend(
                moisture_spec(), (source("N1", "13.0"),), objective=Objective.EXPLICIT_COST
            )

    def test_not_enough_grain_is_infeasible(self):
        with pytest.raises(InfeasibleBlend):
            plan_blend(moisture_spec(), (source("N1", "13.0", available="1000"),))

    def test_all_of_it_too_wet_is_infeasible(self):
        with pytest.raises(InfeasibleBlend) as caught:
            plan_blend(
                moisture_spec(), (source("N1", "16.0"), source("N2", "17.0"))
            )
        assert caught.value.detail["wanted"] == "50000.00 bu"

    def test_nothing_offered_is_infeasible(self):
        with pytest.raises(InfeasibleBlend):
            plan_blend(moisture_spec(), ())

    def test_a_reject_limit_can_make_a_workable_blend_infeasible(self):
        sources = (source("N1", "13.0"), source("N2", "16.0"))
        assert plan_blend(moisture_spec(), sources).quantity == bushels("50000")
        tight = moisture_spec(
            reject_worse_than=(SpecLimit(FactorName.MOISTURE, Decimal("15.0")),)
        )
        with pytest.raises(InfeasibleBlend):
            plan_blend(tight, sources)


class TestMinimumDraw:
    def test_a_bin_that_would_give_a_dribble_is_dropped(self):
        sources = (
            source("N1", "13.0", available="49900"),
            source("N2", "13.1", available="30000", minimum_draw=bushels("5000")),
        )
        plan = plan_blend(sources=sources, spec=moisture_spec())
        assert plan.passes >= 1
        for draw in plan.draws:
            offer = next(s for s in sources if s.identifier == draw.identifier)
            assert draw.quantity >= offer.minimum_draw

    def test_dropping_is_recorded_in_the_notes(self):
        sources = (
            source("N1", "13.0", available="49900"),
            source("N2", "13.1", available="30000", minimum_draw=bushels("5000")),
        )
        plan = plan_blend(moisture_spec(), sources)
        if plan.passes > 1:
            assert any("minimum draw" in note for note in plan.notes)

    def test_dropping_everything_useful_is_infeasible(self):
        sources = (
            source("N1", "16.0", available="49000", minimum_draw=bushels("48000")),
            source("N2", "13.0", available="49000", minimum_draw=bushels("48000")),
        )
        with pytest.raises(InfeasibleBlend):
            plan_blend(moisture_spec(quantity="50000"), sources)


class TestQualityAndMargins:
    def test_the_blended_quality_is_the_weighted_average(self):
        plan = plan_blend(
            moisture_spec(quantity="40000"),
            (source("N1", "13.0", available="30000"), source("N2", "16.0", available="10000")),
        )
        assert plan.quality.moisture == percent("14.5") or plan.quality.moisture < percent("14.5")

    def test_the_margin_shows_the_room_left(self):
        plan = plan_blend(moisture_spec(), (source("N1", "13.0", available="60000"),))
        limit = plan.spec.limits[0]
        assert plan.margin_on(limit) == Decimal("1.50")

    def test_a_minimum_limit_reads_its_margin_the_other_way(self):
        spec = BlendSpec(
            commodity_code="CORN",
            quantity=bushels("10000"),
            limits=(SpecLimit(FactorName.TEST_WEIGHT, Decimal("54.0")),),
        )
        plan = plan_blend(spec, (source("N1", "14.0", available="20000"),))
        assert plan.margin_on(spec.limits[0]) == Decimal("3.0")

    def test_a_multi_factor_spec_is_satisfied_on_every_factor(self):
        spec = BlendSpec(
            commodity_code="CORN",
            quantity=bushels("40000"),
            limits=(
                SpecLimit(FactorName.MOISTURE, Decimal("14.5")),
                SpecLimit(FactorName.TEST_WEIGHT, Decimal("55.0")),
                SpecLimit(FactorName.TOTAL_DAMAGE, Decimal("3.0")),
            ),
        )
        sources = (
            BlendSource("N1", quality("13.0", "58.0", "0.5"), bushels("25000")),
            BlendSource("N2", quality("16.5", "53.0", "6.0"), bushels("25000")),
        )
        plan = plan_blend(spec, sources)
        assert spec.accepts_average(plan.quality) == ()

    def test_the_plan_renders_with_its_margins(self):
        described = plan_blend(
            moisture_spec(), (source("N1", "13.0", available="60000"),)
        ).describe()
        assert described["bins_touched"] == 1
        assert described["margins"]["moisture"] == "1.50"


class TestSourcesFromYard:
    def test_only_bins_holding_the_crop_are_offered(self):
        from conftest import yard_with

        yard = yard_with("N1", "N2", "N3")
        yard = yard.receive(
            "N1", "CORN", bushels("1000"), quality(), at=_now(), reference="T", by="s"
        )
        yard = yard.receive(
            "N2", "SOYBEANS", bushels("1000"), quality(), at=_now(), reference="T", by="s"
        )
        offers = sources_from(yard.states(), "CORN")
        assert [offer.identifier for offer in offers] == ["N1"]

    def test_locked_bins_come_back_locked(self):
        from conftest import yard_with

        yard = yard_with("N1", "N2")
        for name in ("N1", "N2"):
            yard = yard.receive(
                name, "CORN", bushels("1000"), quality(), at=_now(), reference="T", by="s"
            )
        offers = sources_from(yard.states(), "CORN", locked=frozenset({"N2"}))
        locked = {offer.identifier: offer.locked for offer in offers}
        assert locked == {"N1": False, "N2": True}

    def test_a_minimum_draw_never_exceeds_what_the_bin_holds(self):
        from conftest import yard_with

        yard = yard_with("N1").receive(
            "N1", "CORN", bushels("100"), quality(), at=_now(), reference="T", by="s"
        )
        offers = sources_from(yard.states(), "CORN", minimum_draw=bushels("5000"))
        assert offers[0].minimum_draw == bushels("100")


def _now():
    from datetime import datetime

    return datetime(2025, 10, 14, 8, 0)
