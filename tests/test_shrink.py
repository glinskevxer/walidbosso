from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import Bushels, bushels
from binyard.errors import ValidationError
from binyard.percent import percent
from binyard.shrink import ShrinkPolicy, apply_shrink, water_shrink_ratio

CORN_POLICY = ShrinkPolicy.market_default(commodity.CORN)
NO_HANDLING = ShrinkPolicy(
    commodity_code="CORN", target_moisture=percent("15.0"), handling_shrink=percent("0")
)


class TestWaterRatio:
    def test_dry_grain_loses_nothing(self):
        assert water_shrink_ratio(percent("14.0"), percent("15.0")) == Decimal(0)
        assert water_shrink_ratio(percent("15.0"), percent("15.0")) == Decimal(0)

    def test_the_ratio_is_removed_over_remaining_dry_matter(self):
        ratio = water_shrink_ratio(percent("20.0"), percent("15.0"))
        assert ratio == Decimal(500) / Decimal(8500)

    def test_one_point_off_corn_is_about_one_and_a_sixth_percent(self):
        ratio = water_shrink_ratio(percent("16.0"), percent("15.0"))
        assert Decimal("0.0117") < ratio < Decimal("0.0118")

    def test_a_target_of_a_hundred_is_refused(self):
        with pytest.raises(ValidationError):
            water_shrink_ratio(percent("20"), percent("100"))


class TestPolicyValidation:
    def test_a_target_of_zero_is_refused(self):
        with pytest.raises(ValidationError):
            ShrinkPolicy(commodity_code="CORN", target_moisture=percent("0"))

    def test_negative_handling_shrink_is_refused(self):
        with pytest.raises(ValidationError):
            ShrinkPolicy(
                commodity_code="CORN",
                target_moisture=percent("15"),
                handling_shrink=percent("-1"),
            )

    def test_a_negative_posted_factor_is_refused(self):
        with pytest.raises(ValidationError):
            ShrinkPolicy(
                commodity_code="CORN",
                target_moisture=percent("15"),
                posted_factor_per_point=Decimal("-1"),
            )

    def test_a_threshold_under_the_target_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            ShrinkPolicy(
                commodity_code="CORN",
                target_moisture=percent("15"),
                shrink_threshold=percent("14"),
            )
        assert caught.value.detail["target"] == "15%"


class TestApplying:
    def test_dry_corn_is_left_alone_apart_from_handling(self):
        result = apply_shrink(bushels("1000"), percent("14.0"), NO_HANDLING)
        assert result.net == bushels("1000")
        assert not result.shrunk

    def test_a_thousand_bushels_of_twenty_percent_corn(self):
        result = apply_shrink(bushels("1000"), percent("20.0"), CORN_POLICY)
        assert result.points_removed == percent("5.0")
        assert result.water_shrink == bushels("58.82")
        assert result.handling_shrink == bushels("4.71")
        assert result.net == bushels("936.47")

    def test_the_printed_lines_add_back_to_the_gross(self):
        for moisture in ("15.0", "16.3", "18.7", "24.9"):
            result = apply_shrink(bushels("1337.55"), percent(moisture), CORN_POLICY)
            assert result.water_shrink + result.handling_shrink + result.net == result.gross

    def test_handling_is_charged_on_the_dried_quantity(self):
        result = apply_shrink(bushels("1000"), percent("20.0"), CORN_POLICY)
        dried = result.gross - result.water_shrink
        assert result.handling_shrink == dried.scaled_by("0.005")

    def test_a_posted_factor_overrides_the_ratio(self):
        posted = ShrinkPolicy(
            commodity_code="CORN",
            target_moisture=percent("15.0"),
            posted_factor_per_point=Decimal("1.18"),
        )
        result = apply_shrink(bushels("1000"), percent("20.0"), posted)
        assert result.water_shrink == bushels("59.00")

    def test_a_posted_factor_and_the_ratio_disagree_slightly(self):
        posted = ShrinkPolicy(
            commodity_code="CORN",
            target_moisture=percent("15.0"),
            posted_factor_per_point=Decimal("1.18"),
        )
        exact = apply_shrink(bushels("1000"), percent("20.0"), NO_HANDLING)
        assert posted != NO_HANDLING
        assert apply_shrink(bushels("1000"), percent("20.0"), posted).water_shrink > exact.water_shrink

    def test_a_threshold_holds_shrink_off_until_it_is_crossed(self):
        policy = ShrinkPolicy(
            commodity_code="CORN",
            target_moisture=percent("15.0"),
            shrink_threshold=percent("15.5"),
        )
        assert apply_shrink(bushels("1000"), percent("15.4"), policy).net == bushels("1000")
        crossed = apply_shrink(bushels("1000"), percent("15.6"), policy)
        assert crossed.points_removed == percent("0.6")

    def test_shrinking_nothing_gives_nothing(self):
        result = apply_shrink(Bushels.zero(), percent("22.0"), CORN_POLICY)
        assert result.net == Bushels.zero()
        assert result.water_shrink == Bushels.zero()

    def test_a_negative_load_is_refused(self):
        with pytest.raises(ValidationError):
            apply_shrink(bushels("-1"), percent("20.0"), CORN_POLICY)

    def test_shrink_needs_bushels(self):
        with pytest.raises(ValidationError):
            apply_shrink(Decimal("1000"), percent("20.0"), CORN_POLICY)

    def test_wetter_always_nets_less(self):
        previous = None
        for moisture in ("15.0", "16.0", "18.0", "20.0", "22.0", "24.0"):
            net = apply_shrink(bushels("1000"), percent(moisture), CORN_POLICY).net
            if previous is not None:
                assert net < previous
            previous = net

    def test_the_result_renders_for_a_stub(self):
        described = apply_shrink(bushels("1000"), percent("20.0"), CORN_POLICY).describe()
        assert described["net"] == "936.47 bu"
        assert described["points_removed"] == "5%"


def test_soybeans_shrink_to_thirteen():
    policy = ShrinkPolicy.market_default(commodity.SOYBEANS)
    result = apply_shrink(bushels("1000"), percent("16.0"), policy)
    assert result.points_removed == percent("3.0")
    assert result.net < bushels("970")
