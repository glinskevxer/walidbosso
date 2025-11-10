from datetime import date
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.bushels import bushels
from binyard.discounts import (
    Accumulation,
    Band,
    Basis,
    FactorSchedule,
    GradeSchedule,
    PostedSchedule,
    ScheduleBook,
    assess,
)
from binyard.discounts.posted import CORN_SCHEDULE, SEASON_START, default_book
from binyard.errors import ConfigurationError, NoScheduleInEffect, ScheduleError, ValidationError
from binyard.grading import FactorName, FactorSample, determine
from binyard.money import Money, Price, dollars
from binyard.percent import percent

DAY = date(2025, 10, 14)


def cents(value: str) -> Price:
    return Price.from_cents(value)


def corn_sample(**overrides) -> FactorSample:
    base = {
        "moisture": percent("15.0"),
        "test_weight": Decimal("57.0"),
        "total_damage": percent("1.0"),
        "heat_damage": percent("0.0"),
        "broken": percent("1.0"),
    }
    base.update(overrides)
    return FactorSample(**base)


class TestBandValidation:
    def test_an_upper_bound_under_the_lower_is_refused(self):
        with pytest.raises(ConfigurationError):
            Band(Decimal("16"), Decimal("15"), cents("3"))

    def test_a_rejecting_band_cannot_carry_a_rate(self):
        with pytest.raises(ConfigurationError):
            Band(Decimal("25"), None, cents("3"), Basis.REJECT)

    def test_a_gap_between_bands_is_refused(self):
        with pytest.raises(ConfigurationError) as caught:
            FactorSchedule(
                FactorName.MOISTURE,
                (
                    Band(Decimal("15"), Decimal("16"), cents("3")),
                    Band(Decimal("17"), Decimal("18"), cents("5")),
                ),
            )
        assert caught.value.detail["gap_at"] == "17"

    def test_an_overlap_between_bands_is_refused(self):
        with pytest.raises(ConfigurationError):
            FactorSchedule(
                FactorName.MOISTURE,
                (
                    Band(Decimal("15"), Decimal("17"), cents("3")),
                    Band(Decimal("16"), Decimal("18"), cents("5")),
                ),
            )

    def test_an_open_ended_band_has_to_be_last(self):
        with pytest.raises(ConfigurationError):
            FactorSchedule(
                FactorName.MOISTURE,
                (
                    Band(Decimal("15"), None, cents("3")),
                    Band(Decimal("18"), Decimal("20"), cents("5")),
                ),
            )

    def test_a_schedule_needs_bands(self):
        with pytest.raises(ConfigurationError):
            FactorSchedule(FactorName.MOISTURE, ())

    def test_test_weight_cannot_use_this_shape(self):
        with pytest.raises(ConfigurationError):
            FactorSchedule(
                FactorName.TEST_WEIGHT, (Band(Decimal("54"), None, cents("3")),)
            )


class TestRates:
    @pytest.fixture
    def stepped(self):
        return FactorSchedule(
            FactorName.MOISTURE,
            (
                Band(Decimal("15.0"), Decimal("16.0"), cents("3")),
                Band(Decimal("16.0"), Decimal("18.0"), cents("5")),
                Band(Decimal("18.0"), None, cents("8")),
            ),
        )

    @pytest.fixture
    def cumulative(self, stepped):
        return FactorSchedule(
            stepped.factor, stepped.bands, accumulation=Accumulation.CUMULATIVE
        )

    def test_a_reading_at_the_threshold_is_free(self, stepped, cumulative):
        assert stepped.rate_for(Decimal("15.0")) == Price.zero()
        assert cumulative.rate_for(Decimal("15.0")) == Price.zero()

    def test_a_dry_reading_is_free(self, stepped):
        assert stepped.rate_for(Decimal("13.2")) == Price.zero()

    def test_stepped_charges_the_whole_span_at_the_landing_band(self, stepped):
        assert stepped.rate_for(Decimal("17.0")) == cents("10")

    def test_cumulative_charges_each_band_for_its_own_span(self, cumulative):
        assert cumulative.rate_for(Decimal("17.0")) == cents("8")

    def test_the_two_conventions_agree_inside_the_first_band(self, stepped, cumulative):
        assert stepped.rate_for(Decimal("15.5")) == cumulative.rate_for(Decimal("15.5"))

    def test_cumulative_runs_into_the_open_band(self, cumulative):
        assert cumulative.rate_for(Decimal("20.0")) == cents("29")

    def test_a_flat_band_charges_the_same_anywhere_inside_it(self):
        flat = FactorSchedule(
            FactorName.HEAT_DAMAGE,
            (Band(Decimal("0.2"), None, cents("10"), Basis.FLAT),),
        )
        assert flat.rate_for(Decimal("0.3")) == cents("10")
        assert flat.rate_for(Decimal("2.9")) == cents("10")

    def test_a_per_tenth_band_charges_ten_times_as_often(self):
        per_tenth = FactorSchedule(
            FactorName.MOISTURE,
            (Band(Decimal("15.0"), None, cents("0.5"), Basis.PER_TENTH),),
        )
        assert per_tenth.rate_for(Decimal("16.0")) == cents("5")

    def test_a_stepped_reading_past_the_last_band_is_an_error(self):
        short = FactorSchedule(
            FactorName.MOISTURE, (Band(Decimal("15.0"), Decimal("16.0"), cents("3")),)
        )
        with pytest.raises(ScheduleError):
            short.rate_for(Decimal("18.0"))

    def test_a_rejecting_band_is_reported_as_such(self):
        schedule = FactorSchedule(
            FactorName.MOISTURE,
            (
                Band(Decimal("15.0"), Decimal("25.0"), cents("3")),
                Band(Decimal("25.0"), None, Price.zero(), Basis.REJECT),
            ),
        )
        assert schedule.rejects(Decimal("26.0"))
        assert not schedule.rejects(Decimal("24.0"))


class TestGradeSchedule:
    def test_a_worse_grade_cannot_be_cheaper(self):
        with pytest.raises(ConfigurationError):
            GradeSchedule(rates={1: Price.zero(), 2: cents("5"), 3: cents("2")})

    def test_an_empty_schedule_is_refused(self):
        with pytest.raises(ConfigurationError):
            GradeSchedule(rates={})

    def test_a_grade_past_the_listed_ones_pays_the_worst_listed(self):
        schedule = GradeSchedule(rates={1: Price.zero(), 2: cents("5")})
        assert schedule.rate_for(4) == cents("5")

    def test_sample_grade_is_listed_as_zero(self):
        assert CORN_SCHEDULE.grades.rate_for(0) == cents("30")


class TestScheduleBook:
    def test_the_latest_effective_schedule_wins(self):
        later = PostedSchedule(
            commodity_code="CORN",
            effective_from=date(2025, 10, 1),
            factors=CORN_SCHEDULE.factors,
            grades=GradeSchedule(rates={1: Price.zero(), 2: cents("1")}),
        )
        book = default_book().with_schedule(later)
        assert book.in_effect("CORN", DAY) is later
        assert book.in_effect("CORN", date(2025, 9, 15)) is CORN_SCHEDULE

    def test_nothing_posted_before_the_season_opens(self):
        with pytest.raises(NoScheduleInEffect):
            default_book().in_effect("CORN", date(2025, 8, 31))

    def test_a_crop_with_no_board_at_all(self):
        with pytest.raises(NoScheduleInEffect):
            default_book().in_effect("RICE", DAY)

    def test_an_expired_schedule_stops_covering(self):
        expiring = PostedSchedule(
            commodity_code="OATS",
            effective_from=SEASON_START,
            effective_to=date(2025, 9, 30),
            factors=(),
            grades=GradeSchedule(rates={1: Price.zero()}),
        )
        assert expiring.covers(date(2025, 9, 30))
        assert not expiring.covers(date(2025, 10, 1))

    def test_a_schedule_cannot_expire_before_it_starts(self):
        with pytest.raises(ConfigurationError):
            PostedSchedule(
                commodity_code="OATS",
                effective_from=date(2025, 9, 1),
                effective_to=date(2025, 8, 1),
                factors=(),
                grades=GradeSchedule(rates={1: Price.zero()}),
            )

    def test_a_factor_posted_twice_is_refused(self):
        one = FactorSchedule(FactorName.MOISTURE, (Band(Decimal("15"), None, cents("3")),))
        with pytest.raises(ConfigurationError):
            PostedSchedule(
                commodity_code="CORN",
                effective_from=SEASON_START,
                factors=(one, one),
                grades=GradeSchedule(rates={1: Price.zero()}),
            )


class TestAssessing:
    def test_a_clean_load_owes_nothing(self):
        result = determine(corn_sample(), commodity.CORN)
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        assert found.lines == ()
        assert found.total == Money.zero()

    def test_a_wet_load_pays_the_cumulative_moisture_charge(self):
        result = determine(corn_sample(moisture=percent("18.0")), commodity.CORN)
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        line = found.line_for("moisture")
        assert line is not None
        assert line.rate == cents("13")
        assert line.amount == dollars("130.00")

    def test_the_lines_sum_to_the_total(self):
        result = determine(
            corn_sample(moisture=percent("18.0"), broken=percent("5.0")), commodity.CORN
        )
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        assert len(found.lines) >= 2
        running = Money.zero()
        for line in found.lines:
            running = running + line.amount
        assert found.total == running

    def test_the_summed_rate_can_differ_from_the_charge_by_the_rounding(self):
        # Each line is rounded to the cent as it prints, so the sum of the
        # rounded lines and the rounded sum need not agree on a small load.
        result = determine(
            corn_sample(moisture=percent("18.0"), broken=percent("5.0")), commodity.CORN
        )
        found = assess(result, bushels("0.01"), CORN_SCHEDULE)
        gap = abs(found.total.cents - found.total_rate.extend(bushels("0.01")).cents)
        assert gap <= len(found.lines)

    def test_a_rejecting_reading_stops_everything(self):
        result = determine(corn_sample(moisture=percent("26.0")), commodity.CORN)
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        assert found.rejected
        assert found.rejecting_factor is FactorName.MOISTURE
        assert found.lines == ()
        assert found.total == Money.zero()

    def test_the_grade_charge_is_skipped_when_the_board_prices_the_factor(self):
        result = determine(corn_sample(total_damage=percent("6.0")), commodity.CORN)
        assert result.grade == 3
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        assert found.line_for("total_damage") is not None
        assert found.line_for("grade") is None

    def test_the_grade_charge_bites_when_the_board_does_not_price_the_factor(self):
        result = determine(corn_sample(test_weight=Decimal("52.5")), commodity.CORN)
        assert result.grade == 3
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        assert found.line_for("grade") is not None
        assert found.line_for("grade").rate == cents("2")

    def test_the_house_can_choose_to_charge_both(self):
        result = determine(corn_sample(total_damage=percent("6.0")), commodity.CORN)
        found = assess(
            result,
            bushels("1000"),
            CORN_SCHEDULE,
            grade_discount_excludes_scheduled_factors=False,
        )
        assert found.line_for("grade") is not None

    def test_sample_grade_always_pays_its_charge(self):
        result = determine(
            corn_sample(test_weight=Decimal("40.0"), total_damage=percent("16.0")),
            commodity.CORN,
        )
        assert result.is_sample_grade
        found = assess(result, bushels("1000"), CORN_SCHEDULE)
        assert found.line_for("grade").rate == cents("30")

    def test_a_board_for_the_wrong_crop_is_refused(self):
        result = determine(corn_sample(), commodity.CORN)
        with pytest.raises(ValidationError):
            assess(result, bushels("1000"), default_book().in_effect("SOYBEANS", DAY))

    def test_a_negative_quantity_is_refused(self):
        result = determine(corn_sample(), commodity.CORN)
        with pytest.raises(ValidationError):
            assess(result, bushels("-1"), CORN_SCHEDULE)

    def test_the_assessment_renders_with_every_reading(self):
        result = determine(corn_sample(moisture=percent("17.5")), commodity.CORN)
        described = assess(result, bushels("1000"), CORN_SCHEDULE).describe()
        assert described["lines"][0]["source"] == "moisture"
        assert "per point" in described["lines"][0]["note"]


def test_every_posted_crop_has_a_board():
    book = default_book()
    for standard in commodity.STANDARDS:
        assert book.in_effect(standard.code, DAY).commodity_code == standard.code
