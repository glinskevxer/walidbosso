from datetime import date

import pytest

from binyard.bushels import bushels
from binyard.errors import ValidationError
from binyard.money import Money, Price, dollars
from binyard.percent import percent
from binyard.settlement import (
    ChargeKind,
    Checkoff,
    Settlement,
    SettlementBuilder,
    StatementLine,
)

DAY = date(2025, 10, 14)
LOAD = bushels("857.12")

CORN_CHECKOFF = Checkoff("Iowa corn checkoff", per_bushel=Price.from_cents("1"))
SOY_CHECKOFF = Checkoff("national soybean checkoff", percent_of_value=percent("0.5"))


def builder(quantity=LOAD) -> SettlementBuilder:
    return SettlementBuilder("S-1", "HALVORSEN FARMS", "CORN", DAY, quantity)


class TestCheckoff:
    def test_a_checkoff_is_one_thing_or_the_other(self):
        with pytest.raises(ValidationError):
            Checkoff("both", per_bushel=Price.from_cents("1"), percent_of_value=percent("0.5"))
        with pytest.raises(ValidationError):
            Checkoff("neither")

    def test_a_per_bushel_checkoff_ignores_the_value(self):
        assert CORN_CHECKOFF.assess(bushels("1000"), dollars("4520")) == dollars("10.00")
        assert CORN_CHECKOFF.assess(bushels("1000"), Money.zero()) == dollars("10.00")

    def test_a_percentage_checkoff_follows_the_value(self):
        assert SOY_CHECKOFF.assess(bushels("1000"), dollars("10000")) == dollars("50.00")

    def test_a_percentage_checkoff_rounds_half_up(self):
        assert SOY_CHECKOFF.assess(bushels("1"), dollars("1.01")) == dollars("0.01")


class TestLines:
    def test_a_line_needs_a_description(self):
        with pytest.raises(ValidationError):
            StatementLine(ChargeKind.STORAGE, "  ", dollars("-1"))

    def test_a_grain_line_cannot_be_negative(self):
        with pytest.raises(ValidationError):
            StatementLine(ChargeKind.GRAIN, "grain", dollars("-1"))

    def test_only_grain_is_a_credit(self):
        credits = [kind for kind in ChargeKind if kind.is_credit]
        assert credits == [ChargeKind.GRAIN]


class TestBuilding:
    def test_a_settlement_needs_bushels(self):
        with pytest.raises(ValidationError):
            SettlementBuilder("S-1", "H", "CORN", DAY, bushels("0"))

    def test_a_settlement_needs_a_number_and_an_account(self):
        with pytest.raises(ValidationError):
            Settlement(number=" ", account="H", commodity_code="CORN", on=DAY, quantity=LOAD)
        with pytest.raises(ValidationError):
            Settlement(number="S-1", account=" ", commodity_code="CORN", on=DAY, quantity=LOAD)

    def test_grain_alone_settles_at_the_price(self):
        settled = builder().grain(Price.of("4.52")).build()
        assert settled.gross == dollars("3874.18")
        assert settled.deductions == Money.zero()
        assert settled.net == settled.gross

    def test_a_charge_comes_off_however_it_is_signed(self):
        positive = (
            builder()
            .grain(Price.of("4.52"))
            .charge(ChargeKind.STORAGE, "storage", dollars("12.00"))
            .build()
        )
        negative = (
            builder()
            .grain(Price.of("4.52"))
            .charge(ChargeKind.STORAGE, "storage", dollars("-12.00"))
            .build()
        )
        assert positive.net == negative.net

    def test_a_credit_cannot_be_pushed_through_charge(self):
        with pytest.raises(ValidationError):
            builder().charge(ChargeKind.GRAIN, "grain", dollars("10"))

    def test_discounts_are_charged_on_the_settled_bushels(self):
        settled = (
            builder()
            .grain(Price.of("4.52"))
            .discount("moisture", Price.from_cents("6"))
            .build()
        )
        line = settled.lines_of(ChargeKind.DISCOUNT)[0]
        assert line.quantity == LOAD
        assert line.amount == dollars("-51.43")

    def test_drying_multiplies_the_rate_by_the_points(self):
        settled = (
            builder()
            .grain(Price.of("4.52"))
            .drying(percent("5.0"), Price.from_cents("2"))
            .build()
        )
        line = settled.lines_of(ChargeKind.DRYING)[0]
        assert line.rate == Price.from_cents("10")
        assert "5% at $0.02 a point" in line.description

    def test_no_points_means_no_drying_line(self):
        settled = (
            builder()
            .grain(Price.of("4.52"))
            .drying(percent("0"), Price.from_cents("2"))
            .build()
        )
        assert settled.lines_of(ChargeKind.DRYING) == ()

    def test_the_lines_come_out_in_the_fixed_order(self):
        settled = (
            builder()
            .charge(ChargeKind.STORAGE, "storage", dollars("12"))
            .checkoff(CORN_CHECKOFF)
            .discount("moisture", Price.from_cents("6"))
            .grain(Price.of("4.52"))
            .build()
        )
        assert [line.kind for line in settled.lines] == [
            ChargeKind.GRAIN,
            ChargeKind.DISCOUNT,
            ChargeKind.STORAGE,
            ChargeKind.CHECKOFF,
        ]

    def test_the_net_is_the_gross_less_everything_else(self):
        settled = (
            builder()
            .grain(Price.of("4.52"))
            .discount("moisture", Price.from_cents("6"))
            .charge(ChargeKind.STORAGE, "storage", dollars("12.00"))
            .charge(ChargeKind.HAULING, "hauling", dollars("30.00"))
            .build()
        )
        assert settled.net == settled.gross + settled.deductions
        assert settled.net == dollars("3780.75")

    def test_a_percentage_checkoff_sees_the_discounts_above_it(self):
        with_discount = (
            builder()
            .grain(Price.of("4.52"))
            .discount("moisture", Price.from_cents("50"))
            .checkoff(SOY_CHECKOFF)
            .build()
        )
        without = builder().grain(Price.of("4.52")).checkoff(SOY_CHECKOFF).build()
        assert abs(with_discount.total_of(ChargeKind.CHECKOFF)) < abs(
            without.total_of(ChargeKind.CHECKOFF)
        )

    def test_a_per_bushel_checkoff_does_not_care_about_order(self):
        first = builder().checkoff(CORN_CHECKOFF).grain(Price.of("4.52")).build()
        second = builder().grain(Price.of("4.52")).checkoff(CORN_CHECKOFF).build()
        assert first.total_of(ChargeKind.CHECKOFF) == second.total_of(ChargeKind.CHECKOFF)

    def test_the_average_price_is_the_net_over_the_bushels(self):
        settled = (
            builder()
            .grain(Price.of("4.52"))
            .discount("moisture", Price.from_cents("6"))
            .build()
        )
        assert settled.average_price == Price.of("4.46")

    def test_an_advance_already_paid_comes_back_off(self):
        settled = (
            builder()
            .grain(Price.of("4.52"))
            .charge(ChargeKind.ADVANCE, "advance paid 10 Oct", dollars("2000.00"))
            .build()
        )
        assert settled.net == dollars("1874.18")

    def test_the_statement_renders_every_line(self):
        described = (
            builder()
            .grain(Price.of("4.52"))
            .discount("moisture", Price.from_cents("6"))
            .checkoff(CORN_CHECKOFF)
            .build()
            .describe()
        )
        assert described["net"] == "$3,814.18"
        assert [line["kind"] for line in described["lines"]] == [
            "grain",
            "discount",
            "checkoff",
        ]
        assert described["average_price"] == "$4.45"


def test_a_zero_quantity_settlement_has_no_average():
    empty = Settlement(
        number="S-2", account="H", commodity_code="CORN", on=DAY, quantity=bushels("0")
    )
    assert empty.average_price is None
    assert empty.net == Money.zero()
