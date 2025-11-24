from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import Bushels, bushels, from_weight, to_weight
from binyard.errors import IncompatibleUnits, UnknownCommodity, ValidationError
from binyard.units import pounds


class TestCropStandards:
    def test_every_standard_is_reachable_by_its_code(self):
        for code in commodity.codes():
            assert commodity.lookup(code).code == code

    def test_the_scale_house_spellings_resolve(self):
        assert commodity.lookup("milo") is commodity.GRAIN_SORGHUM
        assert commodity.lookup(" beans ") is commodity.SOYBEANS
        assert commodity.lookup("hrw") is commodity.HRW_WHEAT
        assert commodity.lookup("Yellow Corn") is commodity.CORN

    def test_an_unhandled_crop_names_what_is_handled(self):
        with pytest.raises(UnknownCommodity) as caught:
            commodity.lookup("rice")
        assert "CORN" in caught.value.detail["handled"]

    def test_an_empty_name_is_refused(self):
        with pytest.raises(UnknownCommodity):
            commodity.lookup("   ")

    def test_the_ceiling_always_sits_above_the_market_moisture(self):
        for standard in commodity.STANDARDS:
            assert standard.receiving_moisture_ceiling > standard.market_moisture

    def test_only_sunflower_prices_by_the_hundredweight(self):
        by_cwt = [s.code for s in commodity.STANDARDS if not s.priced_by_the_bushel]
        assert by_cwt == ["SUNFLOWER"]

    def test_wheat_and_sorghum_lose_dockage_before_grading(self):
        assert commodity.HRW_WHEAT.dockage_removed_before_grading
        assert commodity.GRAIN_SORGHUM.dockage_removed_before_grading
        assert not commodity.CORN.dockage_removed_before_grading


class TestBushelArithmetic:
    def test_construction_and_reading(self):
        assert bushels("1000.25").hundredths == 100_025
        assert bushels("1000.25").amount == Decimal("1000.25")

    def test_whole_rounds_half_up(self):
        assert bushels("10.50").whole == 11
        assert bushels("10.49").whole == 10

    def test_finer_than_a_hundredth_is_refused(self):
        with pytest.raises(ValidationError):
            bushels("1.001")

    def test_addition_is_exact_over_many_loads(self):
        total = Bushels.zero()
        for _ in range(1000):
            total = total + bushels("0.01")
        assert total == bushels("10.00")

    def test_mixing_with_a_bare_number_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            bushels(1) + 1  # type: ignore[operator]

    def test_share_of_zero_is_refused(self):
        with pytest.raises(ValidationError):
            bushels(1).share_of(Bushels.zero())

    def test_split_sums_back_exactly(self):
        parts = bushels("100.00").split(3)
        assert sum((p.hundredths for p in parts), 0) == 10_000
        assert parts[0].hundredths - parts[-1].hundredths == 1

    def test_split_into_one_part_is_the_whole(self):
        assert bushels("100").split(1) == (bushels("100"),)

    def test_split_into_nothing_is_refused(self):
        with pytest.raises(ValidationError):
            bushels("100").split(0)


class TestConversion:
    def test_a_full_hopper_of_corn(self):
        assert from_weight(pounds("48200"), commodity.CORN) == bushels("860.71")

    def test_soybeans_use_sixty_pounds(self):
        assert from_weight(pounds("48000"), commodity.SOYBEANS) == bushels("800.00")

    def test_oats_use_thirty_two_pounds(self):
        assert from_weight(pounds("32000"), commodity.OATS) == bushels("1000.00")

    def test_the_round_trip_is_within_half_a_pound(self):
        for weight in (pounds("48200"), pounds("13337"), pounds("1")):
            back = to_weight(from_weight(weight, commodity.CORN), commodity.CORN)
            assert abs(back.hundredths - weight.hundredths) <= 28

    def test_zero_weighs_nothing_and_is_no_bushels(self):
        assert from_weight(pounds(0), commodity.CORN) == Bushels.zero()
        assert to_weight(Bushels.zero(), commodity.CORN) == pounds(0)

    def test_a_negative_weight_converts_to_negative_bushels(self):
        assert from_weight(pounds("-5600"), commodity.CORN) == bushels("-100")

    def test_handing_it_the_wrong_type_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            from_weight(48200, commodity.CORN)  # type: ignore[arg-type]
        with pytest.raises(IncompatibleUnits):
            to_weight(800, commodity.CORN)  # type: ignore[arg-type]


def test_bushels_repr_round_trips():
    quantity = bushels("860.71")
    assert eval(repr(quantity), {"Bushels": Bushels}) == quantity
