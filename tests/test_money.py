from decimal import Decimal

import pytest

from binyard.bushels import bushels
from binyard.errors import IncompatibleUnits, ValidationError
from binyard.money import Money, Price, dollars, price


class TestMoney:
    def test_amounts_are_exact_cents(self):
        assert dollars("4.52").cents == 452
        assert dollars("-4.52").cents == -452

    def test_a_tenth_of_a_cent_is_refused(self):
        with pytest.raises(ValidationError) as caught:
            dollars("4.521")
        assert "cent" in caught.value.message

    def test_a_float_is_not_a_count_of_cents(self):
        with pytest.raises(ValidationError):
            Money(4.52)  # type: ignore[arg-type]

    def test_addition_over_many_lines_stays_exact(self):
        total = Money.zero()
        for _ in range(300):
            total = total + dollars("0.07")
        assert total == dollars("21.00")

    def test_mixing_money_and_a_number_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            dollars(1) + 1  # type: ignore[operator]

    def test_scaling_rounds_half_up(self):
        assert dollars("1.00").scaled_by("0.005") == dollars("0.01")
        assert dollars("1.00").scaled_by("0.004") == dollars("0.00")

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("$4.52", dollars("4.52")),
            ("4.52", dollars("4.52")),
            ("-$1,234.56", dollars("-1234.56")),
            ("$ 1,000", dollars("1000")),
        ],
    )
    def test_parsing_the_written_forms(self, text, expected):
        assert Money.parse(text) == expected

    @pytest.mark.parametrize("text", ["", "$", "four dollars", "1.2.3"])
    def test_parsing_refuses_the_rest(self, text):
        with pytest.raises(ValidationError):
            Money.parse(text)

    def test_rendering_carries_the_sign_outside_the_symbol(self):
        assert str(dollars("-1234.5")) == "-$1,234.50"
        assert str(dollars("1234.5")) == "$1,234.50"


class TestAllocation:
    def test_parts_sum_back_to_the_whole(self):
        parts = dollars("100.00").allocate([1, 1, 1])
        assert sum((p.cents for p in parts), 0) == 10_000

    def test_the_odd_cent_goes_to_the_largest_weight(self):
        parts = dollars("10.00").allocate([3, 1])
        assert parts == [dollars("7.50"), dollars("2.50")]
        parts = dollars("0.05").allocate([2, 1])
        assert parts == [dollars("0.04"), dollars("0.01")]

    def test_a_negative_amount_allocates_negatively(self):
        parts = dollars("-10.00").allocate([1, 1, 1])
        assert sum((p.cents for p in parts), 0) == -1000
        assert all(p.cents <= 0 for p in parts)

    def test_a_zero_weight_gets_nothing(self):
        parts = dollars("9.00").allocate([2, 0, 1])
        assert parts[1] == Money.zero()

    def test_weights_that_cannot_allocate(self):
        with pytest.raises(ValidationError):
            dollars("1").allocate([])
        with pytest.raises(ValidationError):
            dollars("1").allocate([0, 0])
        with pytest.raises(ValidationError):
            dollars("1").allocate([-1, 2])


class TestPrice:
    def test_quarter_cents_are_representable(self):
        assert price("4.5225").ticks == 45_225
        assert Price.from_cents("452.25") == price("4.5225")

    def test_finer_than_a_hundredth_of_a_cent_is_refused(self):
        with pytest.raises(ValidationError):
            price("4.52251")

    def test_extending_rounds_half_up_to_the_cent(self):
        assert price("4.5225").extend(bushels("860.71")) == dollars("3892.56")
        assert price("1.005").extend(bushels("1")) == dollars("1.01")

    def test_extending_zero_bushels_is_free(self):
        assert price("4.5225").extend(bushels(0)) == Money.zero()

    def test_a_negative_price_extends_negatively(self):
        assert price("-0.10").extend(bushels("100")) == dollars("-10.00")

    def test_extending_over_something_that_is_not_bushels_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            price("4.52").extend(Decimal("100"))  # type: ignore[arg-type]

    def test_prices_add_and_subtract(self):
        assert price("4.50") + Price.from_cents("2.25") == price("4.5225")
        assert price("4.50") - price("0.25") == price("4.25")

    def test_mixing_a_price_and_money_is_refused(self):
        with pytest.raises(IncompatibleUnits):
            price("4.50") + dollars("1")  # type: ignore[operator]

    def test_rendering_trims_the_dead_places(self):
        assert str(price("4.5000")) == "$4.5"
        assert str(price("4.5225")) == "$4.5225"

    def test_cents_view(self):
        assert price("4.5225").cents == Decimal("452.2500")


def test_reprs_round_trip():
    assert eval(repr(dollars("12.34")), {"Money": Money}) == dollars("12.34")  # noqa: S307
    assert eval(repr(price("4.5225")), {"Price": Price}) == price("4.5225")  # noqa: S307
