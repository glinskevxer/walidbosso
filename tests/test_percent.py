from decimal import Decimal

import pytest

from binyard import commodity
from binyard.errors import IncompatibleUnits, ValidationError
from binyard.percent import Percent, percent, weighted_average


class TestConstruction:
    def test_points_land_on_hundredths(self):
        assert percent("15.5").hundredths == 1550
        assert percent(0).hundredths == 0

    def test_finer_than_a_hundredth_is_refused(self):
        with pytest.raises(ValidationError):
            percent("15.555")

    def test_a_float_is_not_a_count(self):
        with pytest.raises(ValidationError):
            Percent(15.5)  # type: ignore[arg-type]

    def test_nonsense_is_refused(self):
        with pytest.raises(ValidationError):
            percent("wet")
        with pytest.raises(ValidationError):
            percent("NaN")


class TestReading:
    def test_the_three_views_agree(self):
        value = percent("15.5")
        assert value.points == Decimal("15.50")
        assert value.fraction == Decimal("0.155")
        assert value.tenths == Decimal("15.5")

    def test_tenths_round_half_up(self):
        assert percent("15.55").tenths == Decimal("15.6")
        assert percent("15.54").tenths == Decimal("15.5")

    def test_taking_a_percentage_of_a_number(self):
        assert percent("15.5").of(1000) == Decimal("155.000")

    def test_rendering_trims_the_dead_places(self):
        assert str(percent("15.50")) == "15.5%"
        assert str(percent("15.00")) == "15%"


class TestArithmetic:
    def test_addition_and_subtraction(self):
        assert percent("15.5") + percent("0.5") == percent("16.0")
        assert percent("15.5") - percent("0.5") == percent("15.0")

    def test_mixing_with_a_bare_number_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            percent("15.5") + 1  # type: ignore[operator]
        with pytest.raises(IncompatibleUnits):
            percent("15.5") - Decimal("1")  # type: ignore[operator]

    def test_negation_and_absolute_value(self):
        assert -percent("1.5") == percent("-1.5")
        assert abs(percent("-1.5")) == percent("1.5")

    def test_scaling_rounds_half_up(self):
        assert percent("1.00").scaled_by("1.005") == percent("1.01")

    def test_clamping(self):
        low, high = percent("10"), percent("20")
        assert percent("5").clamped(low, high) == low
        assert percent("25").clamped(low, high) == high
        assert percent("15").clamped(low, high) == percent("15")

    def test_clamp_bounds_the_wrong_way_round_are_refused(self):
        with pytest.raises(ValidationError):
            percent("15").clamped(percent("20"), percent("10"))


class TestWeightedAverage:
    def test_equal_weights_give_the_mean(self):
        assert weighted_average([(percent("14.0"), 1), (percent("16.0"), 1)]) == percent("15.0")

    def test_bushels_pull_the_average(self):
        blended = weighted_average([(percent("14.0"), 9000), (percent("20.0"), 1000)])
        assert blended == percent("14.6")

    def test_a_single_lot_is_itself(self):
        assert weighted_average([(percent("17.3"), 4200)]) == percent("17.3")

    def test_the_result_stays_between_the_inputs(self):
        blended = weighted_average([(percent("11.2"), 3), (percent("19.8"), 7)])
        assert percent("11.2") <= blended <= percent("19.8")

    def test_nothing_to_average_is_refused(self):
        with pytest.raises(ValidationError):
            weighted_average([])

    def test_weights_that_sum_to_nothing_are_refused(self):
        with pytest.raises(ValidationError):
            weighted_average([(percent("14.0"), 0), (percent("16.0"), 0)])


def test_crop_moisture_is_a_percent_now():
    assert isinstance(commodity.CORN.market_moisture, Percent)
    assert commodity.CORN.market_moisture == percent("15.0")
    assert commodity.SOYBEANS.receiving_moisture_ceiling == percent("20.0")
