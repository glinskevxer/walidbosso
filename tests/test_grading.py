from decimal import Decimal

import pytest

from binyard import commodity
from binyard.errors import ConfigurationError, FactorOutOfRange, ValidationError
from binyard.grading import (
    SAMPLE_GRADE,
    Direction,
    FactorName,
    FactorSample,
    GradeRow,
    GradeTable,
    Odor,
    SpecialGrade,
    determine,
    direction_of,
    table_for,
)
from binyard.grading.tables import TABLES
from binyard.percent import percent


def corn(**overrides) -> FactorSample:
    base = {
        "moisture": percent("15.0"),
        "test_weight": Decimal("57.0"),
        "total_damage": percent("1.0"),
        "heat_damage": percent("0.0"),
        "broken": percent("1.0"),
    }
    base.update(overrides)
    return FactorSample(**base)


class TestSampleValidation:
    def test_heat_damage_cannot_exceed_total(self):
        with pytest.raises(FactorOutOfRange) as caught:
            corn(total_damage=percent("0.2"), heat_damage=percent("0.4"))
        assert caught.value.detail["heat"] == "0.4%"

    def test_a_negative_factor_is_refused(self):
        with pytest.raises(FactorOutOfRange):
            corn(total_damage=percent("-0.1"))

    def test_a_factor_over_a_hundred_is_refused(self):
        with pytest.raises(FactorOutOfRange):
            corn(total_damage=percent("101"))

    def test_an_implausible_test_weight_is_refused(self):
        with pytest.raises(FactorOutOfRange):
            corn(test_weight=Decimal("0"))
        with pytest.raises(FactorOutOfRange):
            corn(test_weight=Decimal("150"))

    def test_a_bare_number_is_not_a_percentage(self):
        with pytest.raises(ValidationError):
            corn(total_damage=Decimal("1.0"))

    def test_test_weight_has_to_be_a_decimal(self):
        with pytest.raises(ValidationError):
            corn(test_weight=57.0)

    def test_reading_returns_the_table_units(self):
        sample = corn()
        assert sample.reading(FactorName.TEST_WEIGHT) == Decimal("57.0")
        assert sample.reading(FactorName.TOTAL_DAMAGE) == Decimal("1.00")

    def test_test_weight_is_not_a_percentage(self):
        with pytest.raises(ValidationError):
            corn().percent_of(FactorName.TEST_WEIGHT)


class TestDirection:
    def test_only_test_weight_runs_upward(self):
        upward = [f for f in FactorName if direction_of(f) is Direction.HIGHER_IS_BETTER]
        assert upward == [FactorName.TEST_WEIGHT]


class TestTableIntegrity:
    @pytest.mark.parametrize("code", sorted(TABLES))
    def test_every_row_relaxes_or_holds(self, code):
        table = TABLES[code]
        for better, worse in zip(table.rows, table.rows[1:], strict=False):
            for factor, limit in better.limits.items():
                looser = worse.limits[factor]
                if direction_of(factor) is Direction.HIGHER_IS_BETTER:
                    assert looser <= limit
                else:
                    assert looser >= limit

    def test_a_table_that_tightens_is_rejected_at_construction(self):
        with pytest.raises(ConfigurationError) as caught:
            GradeTable(
                commodity_code="BROKEN",
                rows=(
                    GradeRow(1, {FactorName.TOTAL_DAMAGE: Decimal("5.0")}),
                    GradeRow(2, {FactorName.TOTAL_DAMAGE: Decimal("3.0")}),
                ),
            )
        assert caught.value.detail["factor"] == "total_damage"

    def test_rows_out_of_order_are_rejected(self):
        with pytest.raises(ConfigurationError):
            GradeTable(
                commodity_code="BROKEN",
                rows=(
                    GradeRow(2, {FactorName.TOTAL_DAMAGE: Decimal("3.0")}),
                    GradeRow(1, {FactorName.TOTAL_DAMAGE: Decimal("5.0")}),
                ),
            )

    def test_an_empty_table_is_rejected(self):
        with pytest.raises(ConfigurationError):
            GradeTable(commodity_code="BROKEN", rows=())

    def test_a_repeated_grade_is_rejected(self):
        with pytest.raises(ConfigurationError):
            GradeTable(
                commodity_code="BROKEN",
                rows=(
                    GradeRow(1, {FactorName.TOTAL_DAMAGE: Decimal("3.0")}),
                    GradeRow(1, {FactorName.TOTAL_DAMAGE: Decimal("5.0")}),
                ),
            )

    def test_asking_for_a_crop_with_no_table(self):
        with pytest.raises(ConfigurationError):
            table_for("RICE")

    def test_asking_a_table_for_a_grade_it_does_not_have(self):
        with pytest.raises(ConfigurationError):
            table_for("CORN").row(9)


class TestCornGrades:
    def test_a_clean_load_grades_number_one(self):
        result = determine(corn(), commodity.CORN)
        assert result.grade == 1
        assert result.label == "US No. 1"
        assert result.limiting == ()

    def test_light_test_weight_drops_it_to_two_and_says_so(self):
        result = determine(corn(test_weight=Decimal("55.0")), commodity.CORN)
        assert result.grade == 2
        assert result.binding_factor is FactorName.TEST_WEIGHT
        assert result.limiting[0].margin == Decimal("1.0")

    def test_damage_alone_can_be_the_binding_factor(self):
        result = determine(corn(total_damage=percent("4.0")), commodity.CORN)
        assert result.grade == 2
        assert result.binding_factor is FactorName.TOTAL_DAMAGE

    def test_two_factors_can_bind_at_once(self):
        result = determine(
            corn(test_weight=Decimal("55.0"), total_damage=percent("4.0")),
            commodity.CORN,
        )
        assert result.grade == 2
        assert result.binding_factor is None
        assert {item.factor for item in result.limiting} == {
            FactorName.TEST_WEIGHT,
            FactorName.TOTAL_DAMAGE,
        }

    def test_a_reading_exactly_on_the_limit_stays_in_the_grade(self):
        assert determine(corn(test_weight=Decimal("56.0")), commodity.CORN).grade == 1
        assert determine(corn(total_damage=percent("3.0")), commodity.CORN).grade == 1
        assert determine(corn(broken=percent("2.0")), commodity.CORN).grade == 1

    def test_falling_off_the_bottom_is_sample_grade(self):
        result = determine(
            corn(test_weight=Decimal("40.0"), total_damage=percent("20.0")),
            commodity.CORN,
        )
        assert result.grade == SAMPLE_GRADE
        assert result.is_sample_grade
        assert result.label == "US Sample grade"
        assert len(result.limiting) == 2


class TestSpecialGrades:
    def test_infestation_rides_alongside_the_number(self):
        result = determine(corn(infested=True), commodity.CORN)
        assert result.grade == 1
        assert result.special == (SpecialGrade.INFESTED,)
        assert result.label == "US No. 1, infested"

    def test_musty_does_not_change_the_number(self):
        result = determine(corn(odor=Odor.MUSTY), commodity.CORN)
        assert result.grade == 1
        assert SpecialGrade.MUSTY in result.special

    def test_an_objectionable_smell_forces_sample_grade(self):
        result = determine(corn(odor=Odor.COMMERCIALLY_OBJECTIONABLE), commodity.CORN)
        assert result.is_sample_grade
        assert result.limiting == ()
        assert "objectionable" in result.notes[-1]


class TestDockageRebasing:
    def test_wheat_is_graded_after_dockage_comes_out(self):
        sample = FactorSample(
            moisture=percent("12.0"),
            test_weight=Decimal("61.0"),
            total_damage=percent("1.0"),
            foreign_material=percent("0.35"),
            dockage=percent("5.0"),
        )
        result = determine(sample, commodity.HRW_WHEAT)
        assert result.graded_sample.dockage.is_zero()
        assert result.graded_sample.foreign_material.points == Decimal("0.37")
        assert result.grade == 1
        assert "dockage" in result.notes[0]

    def test_rebasing_can_cost_a_grade(self):
        readings = {
            "moisture": percent("12.0"),
            "test_weight": Decimal("61.0"),
            "foreign_material": percent("0.40"),
        }
        clean = determine(FactorSample(**readings), commodity.HRW_WHEAT)
        assert clean.grade == 1
        dirty = determine(
            FactorSample(**readings, dockage=percent("3.0")), commodity.HRW_WHEAT
        )
        assert dirty.grade == 2
        assert dirty.binding_factor is FactorName.FOREIGN_MATERIAL

    def test_corn_is_graded_with_the_dockage_left_in(self):
        result = determine(corn(dockage=percent("2.0")), commodity.CORN)
        assert result.graded_sample.dockage == percent("2.0")
        assert result.notes == ()

    def test_a_sample_that_is_entirely_dockage_is_refused(self):
        sample = FactorSample(
            moisture=percent("12.0"), test_weight=Decimal("60.0"), dockage=percent("100")
        )
        with pytest.raises(FactorOutOfRange):
            determine(sample, commodity.HRW_WHEAT)


class TestDescription:
    def test_the_result_renders_for_a_stub(self):
        result = determine(corn(test_weight=Decimal("55.0")), commodity.CORN)
        described = result.describe()
        assert described["grade"] == 2
        assert described["limiting"][0]["factor"] == "test_weight"
        assert described["limiting"][0]["margin"] == "1.0"

    def test_the_sample_renders_flat(self):
        described = corn().describe()
        assert described["test_weight"] == "57.0 lb/bu"
        assert described["infested"] == "no"
