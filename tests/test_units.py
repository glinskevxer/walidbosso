from decimal import Decimal

import pytest

from binyard.errors import IncompatibleUnits, ValidationError
from binyard.units import Weight, cwt, parse_weight, pounds, tons


class TestConstruction:
    def test_pounds_land_on_hundredths(self):
        assert pounds("48200").hundredths == 4_820_000
        assert pounds("1.25").hundredths == 125

    def test_cwt_and_tons_agree_with_pounds(self):
        assert cwt(1) == pounds(100)
        assert tons(1) == pounds(2000)
        assert tons("0.5") == pounds(1000)

    def test_zero_is_zero_however_it_is_built(self):
        assert Weight.zero() == pounds(0) == cwt(0) == tons(0)
        assert Weight.zero().is_zero()

    def test_finer_than_a_hundredth_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            pounds("1.005")
        assert "hundredth" in caught.value.message

    def test_a_float_masquerading_as_a_count_is_refused(self):
        with pytest.raises(ValidationError):
            Weight(1.5)  # type: ignore[arg-type]

    def test_a_bool_is_not_a_count_of_hundredths(self):
        with pytest.raises(ValidationError):
            Weight(True)  # type: ignore[arg-type]

    def test_nonsense_is_refused(self):
        with pytest.raises(ValidationError):
            pounds("heavy")
        with pytest.raises(ValidationError):
            pounds("Infinity")


class TestReading:
    def test_conversions_are_exact(self):
        w = pounds("48200")
        assert w.pounds == Decimal("48200")
        assert w.cwt == Decimal("482")
        assert w.tons == Decimal("24.1")

    def test_whole_pounds_rounds_half_up(self):
        assert pounds("1.50").whole_pounds == 2
        assert pounds("1.49").whole_pounds == 1
        assert pounds("2.50").whole_pounds == 3

    def test_sign_predicates(self):
        assert pounds(1).is_positive()
        assert not pounds(0).is_positive()
        assert not pounds(-1).is_positive()


class TestArithmetic:
    def test_addition_and_subtraction_stay_exact(self):
        gross, tare = pounds("78420"), pounds("30180")
        assert gross - tare == pounds("48240")
        assert (gross - tare) + tare == gross

    def test_a_hundred_tenths_of_a_pound_add_to_ten_pounds(self):
        total = Weight.zero()
        for _ in range(100):
            total = total + pounds("0.10")
        assert total == pounds("10")

    def test_negation_and_absolute_value(self):
        assert -pounds(5) == pounds(-5)
        assert abs(pounds(-5)) == pounds(5)

    def test_adding_a_bare_number_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            pounds(1) + 1  # type: ignore[operator]

    def test_subtracting_a_bare_number_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            pounds(1) - "1"  # type: ignore[operator]

    def test_scaling_rounds_half_up_at_the_hundredth(self):
        assert pounds("100").scaled_by("0.015") == pounds("1.5")
        assert pounds("1").scaled_by("0.005") == pounds("0.01")

    def test_ratio_is_dimensionless(self):
        assert pounds(50) .ratio_to(pounds(200)) == Decimal("0.25")

    def test_ratio_against_zero_is_refused(self):
        with pytest.raises(ValidationError):
            pounds(1).ratio_to(Weight.zero())

    def test_ratio_against_a_number_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            pounds(1).ratio_to(4)  # type: ignore[arg-type]


class TestOrderingAndIdentity:
    def test_weights_order_by_magnitude(self):
        assert pounds(1) < pounds(2) < tons(1)
        assert max([pounds(10), cwt(1), pounds(5)]) == cwt(1)

    def test_equal_weights_hash_together(self):
        assert len({pounds(100), cwt(1), tons("0.05")}) == 1


class TestParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("48200", pounds(48200)),
            ("48200 lb", pounds(48200)),
            ("  48200lbs ", pounds(48200)),
            ("482 CWT", pounds(48200)),
            ("24.1 tons", pounds(48200)),
            ("-120 lb", pounds(-120)),
            ("+120 lb", pounds(120)),
        ],
    )
    def test_written_forms(self, text, expected):
        assert parse_weight(text) == expected

    @pytest.mark.parametrize("text", ["", "lb", "48,200", "48200 bushels", "1e5"])
    def test_forms_that_are_not_weights(self, text):
        with pytest.raises(ValidationError):
            parse_weight(text)


def test_repr_round_trips():
    w = pounds("48200.55")
    assert eval(repr(w), {"Weight": Weight}) == w  # noqa: S307


def test_str_reads_like_a_ticket_line():
    assert str(pounds("48200")) == "48200 lb"
