"""Laws the quantity types have to obey, checked over generated values.

The example based tests say what one weight does. These say what every weight
does, which is the only way to be sure a rounding rule holds at the boundary
nobody thought to write down.
"""

from __future__ import annotations

from decimal import Decimal

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from binyard import commodity
from binyard.bushels import Bushels, from_weight, to_weight
from binyard.money import Money, Price
from binyard.percent import Percent, weighted_average
from binyard.shrink import ShrinkPolicy, apply_shrink
from binyard.units import Weight

CROPS = st.sampled_from(commodity.STANDARDS)

weights = st.integers(min_value=-10_000_000, max_value=10_000_000).map(Weight)
positive_weights = st.integers(min_value=1, max_value=10_000_000).map(Weight)
quantities = st.integers(min_value=0, max_value=100_000_000).map(Bushels)
amounts = st.integers(min_value=-10_000_000, max_value=10_000_000).map(Money)
prices = st.integers(min_value=0, max_value=1_000_000).map(Price)
percents = st.integers(min_value=0, max_value=10_000).map(Percent)


class TestWeightLaws:
    @given(weights, weights)
    def test_addition_commutes(self, one, two):
        assert one + two == two + one

    @given(weights, weights, weights)
    def test_addition_associates(self, one, two, three):
        assert (one + two) + three == one + (two + three)

    @given(weights, weights)
    def test_subtraction_undoes_addition(self, one, two):
        assert (one + two) - two == one

    @given(weights)
    def test_negation_is_its_own_inverse(self, one):
        assert -(-one) == one  # noqa: B002

    @given(weights)
    def test_the_pounds_view_round_trips(self, one):
        assert Weight.from_pounds(one.pounds) == one

    @given(weights, weights)
    def test_ordering_agrees_with_the_underlying_count(self, one, two):
        assert (one < two) == (one.hundredths < two.hundredths)


class TestBushelLaws:
    @given(quantities, quantities)
    def test_addition_commutes(self, one, two):
        assert one + two == two + one

    @given(quantities, st.integers(min_value=1, max_value=97))
    def test_a_split_sums_back_to_the_whole(self, quantity, parts):
        pieces = quantity.split(parts)
        assert sum(piece.hundredths for piece in pieces) == quantity.hundredths

    @given(quantities, st.integers(min_value=1, max_value=97))
    def test_the_pieces_of_a_split_differ_by_at_most_one(self, quantity, parts):
        pieces = quantity.split(parts)
        assert max(p.hundredths for p in pieces) - min(p.hundredths for p in pieces) <= 1

    @given(positive_weights, CROPS)
    def test_the_conversion_round_trips_inside_half_a_bushel(self, weight, standard):
        back = to_weight(from_weight(weight, standard), standard)
        limit = standard.pounds_per_bushel * 100 / 2 + 1
        assert abs(back.hundredths - weight.hundredths) <= limit

    @given(positive_weights, positive_weights, CROPS)
    def test_conversion_is_monotonic(self, one, two, standard):
        assume(one != two)
        heavier, lighter = max(one, two), min(one, two)
        assert from_weight(heavier, standard) >= from_weight(lighter, standard)


class TestMoneyLaws:
    @given(amounts, st.lists(st.integers(min_value=0, max_value=1000), min_size=1, max_size=12))
    def test_an_allocation_sums_back_to_the_whole(self, amount, weights_list):
        assume(sum(weights_list) > 0)
        parts = amount.allocate(weights_list)
        assert sum(part.cents for part in parts) == amount.cents

    @given(amounts, st.lists(st.integers(min_value=1, max_value=1000), min_size=1, max_size=12))
    def test_every_part_shares_the_sign_of_the_whole(self, amount, weights_list):
        parts = amount.allocate(weights_list)
        if amount.cents >= 0:
            assert all(part.cents >= 0 for part in parts)
        else:
            assert all(part.cents <= 0 for part in parts)

    @given(prices, quantities)
    def test_extending_is_never_further_than_a_cent_from_the_exact_product(
        self, price, quantity
    ):
        exact = Decimal(price.ticks) * Decimal(quantity.hundredths) / Decimal(10_000)
        assert abs(Decimal(price.extend(quantity).cents) - exact) <= Decimal("0.5")

    @given(prices, quantities, quantities)
    def test_extending_is_monotonic_in_quantity(self, price, one, two):
        smaller, larger = min(one, two), max(one, two)
        assert price.extend(smaller) <= price.extend(larger)


class TestPercentLaws:
    @given(st.lists(st.tuples(percents, st.integers(1, 10_000)), min_size=1, max_size=10))
    def test_a_weighted_average_lands_between_the_inputs(self, pairs):
        blended = weighted_average(pairs)
        lowest = min(value for value, _ in pairs)
        highest = max(value for value, _ in pairs)
        assert lowest <= blended <= highest

    @given(percents, st.integers(1, 10_000))
    def test_averaging_one_value_returns_it(self, value, weight):
        assert weighted_average([(value, weight)]) == value

    @given(percents)
    def test_the_fraction_view_agrees_with_the_points_view(self, value):
        assert value.fraction * 100 == value.points


class TestShrinkLaws:
    @settings(max_examples=200)
    @given(
        st.integers(min_value=0, max_value=10_000_000).map(Bushels),
        st.integers(min_value=0, max_value=3000).map(Percent),
        CROPS,
    )
    def test_the_lines_always_add_back_to_the_gross(self, gross, moisture, standard):
        policy = ShrinkPolicy.market_default(standard)
        found = apply_shrink(gross, moisture, policy)
        assert found.water_shrink + found.handling_shrink + found.net == found.gross

    @given(
        st.integers(min_value=1, max_value=10_000_000).map(Bushels),
        st.integers(min_value=0, max_value=3000).map(Percent),
        CROPS,
    )
    def test_shrink_never_takes_more_than_there_was(self, gross, moisture, standard):
        policy = ShrinkPolicy.market_default(standard)
        found = apply_shrink(gross, moisture, policy)
        assert found.net.hundredths >= 0
        assert found.total_shrink <= found.gross

    @given(
        st.integers(min_value=1_000, max_value=10_000_000).map(Bushels),
        st.integers(min_value=0, max_value=2000),
        st.integers(min_value=0, max_value=2000),
        CROPS,
    )
    def test_wetter_never_nets_more(self, gross, one, two, standard):
        policy = ShrinkPolicy.market_default(standard)
        drier, wetter = Percent(min(one, two)), Percent(max(one, two))
        assert apply_shrink(gross, wetter, policy).net <= apply_shrink(gross, drier, policy).net
