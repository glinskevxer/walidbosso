"""The numeric grade tables, one per crop.

These are the published limits. A row is a grade and the worst reading each
factor may take and still sit in it. Because test weight runs the other way
(see :func:`binyard.grading.factors.direction_of`) a row holds a *limit*, and
whether that limit is a floor or a ceiling is read off the factor, never
written into the table. Getting that wrong is silent: a table that treated test
weight as a maximum would grade every good load as sample grade and every bad
one as No. 1.

Tables are checked at import. Grades have to be listed best first, and each
successive grade has to be no stricter on every factor it names. A table that
tightened as it got worse would make the walk in :mod:`binyard.grading.grade`
return the first row it tried, forever, and no test of a single load would
notice.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from binyard.errors import ConfigurationError
from binyard.grading.factors import Direction, FactorName, direction_of

__all__ = ["SAMPLE_GRADE", "TABLES", "GradeRow", "GradeTable", "table_for"]

#: The grade number used when no row accepts the sample.
SAMPLE_GRADE: Final = 0


@dataclass(frozen=True, slots=True)
class GradeRow:
    """One grade and its limits."""

    grade: int
    limits: dict[FactorName, Decimal]

    def accepts(self, readings: dict[FactorName, Decimal]) -> bool:
        return not self.failures(readings)

    def failures(self, readings: dict[FactorName, Decimal]) -> tuple[FactorName, ...]:
        """The factors that keep this sample out of this grade."""
        missed: list[FactorName] = []
        for factor, limit in self.limits.items():
            if factor not in readings:
                continue
            value = readings[factor]
            if direction_of(factor) is Direction.HIGHER_IS_BETTER:
                if value < limit:
                    missed.append(factor)
            elif value > limit:
                missed.append(factor)
        return tuple(missed)

    @property
    def label(self) -> str:
        return f"US No. {self.grade}"


@dataclass(frozen=True, slots=True)
class GradeTable:
    """The ordered rows for one crop, best grade first."""

    commodity_code: str
    rows: tuple[GradeRow, ...]

    def __post_init__(self) -> None:
        if not self.rows:
            raise ConfigurationError(f"{self.commodity_code} has no grade rows")
        grades = [row.grade for row in self.rows]
        if grades != sorted(grades):
            raise ConfigurationError(
                f"{self.commodity_code} rows are not best grade first", grades=grades
            )
        if len(set(grades)) != len(grades):
            raise ConfigurationError(f"{self.commodity_code} repeats a grade", grades=grades)
        for better, worse in zip(self.rows, self.rows[1:], strict=False):
            _check_relaxes(self.commodity_code, better, worse)

    @property
    def factors(self) -> tuple[FactorName, ...]:
        seen: list[FactorName] = []
        for row in self.rows:
            for factor in row.limits:
                if factor not in seen:
                    seen.append(factor)
        return tuple(seen)

    def row(self, grade: int) -> GradeRow:
        for row in self.rows:
            if row.grade == grade:
                return row
        raise ConfigurationError(
            f"{self.commodity_code} has no grade {grade}",
            available=[r.grade for r in self.rows],
        )


def _check_relaxes(code: str, better: GradeRow, worse: GradeRow) -> None:
    for factor, limit in better.limits.items():
        if factor not in worse.limits:
            continue
        looser = worse.limits[factor]
        if direction_of(factor) is Direction.HIGHER_IS_BETTER:
            tightened = looser > limit
        else:
            tightened = looser < limit
        if tightened:
            raise ConfigurationError(
                f"{code} grade {worse.grade} is stricter than grade {better.grade}",
                factor=factor.value,
            )


def _d(value: str) -> Decimal:
    return Decimal(value)


CORN_TABLE: Final = GradeTable(
    commodity_code="CORN",
    rows=(
        GradeRow(
            1,
            {
                FactorName.TEST_WEIGHT: _d("56.0"),
                FactorName.HEAT_DAMAGE: _d("0.1"),
                FactorName.TOTAL_DAMAGE: _d("3.0"),
                FactorName.BROKEN: _d("2.0"),
            },
        ),
        GradeRow(
            2,
            {
                FactorName.TEST_WEIGHT: _d("54.0"),
                FactorName.HEAT_DAMAGE: _d("0.2"),
                FactorName.TOTAL_DAMAGE: _d("5.0"),
                FactorName.BROKEN: _d("3.0"),
            },
        ),
        GradeRow(
            3,
            {
                FactorName.TEST_WEIGHT: _d("52.0"),
                FactorName.HEAT_DAMAGE: _d("0.5"),
                FactorName.TOTAL_DAMAGE: _d("7.0"),
                FactorName.BROKEN: _d("4.0"),
            },
        ),
        GradeRow(
            4,
            {
                FactorName.TEST_WEIGHT: _d("49.0"),
                FactorName.HEAT_DAMAGE: _d("1.0"),
                FactorName.TOTAL_DAMAGE: _d("10.0"),
                FactorName.BROKEN: _d("5.0"),
            },
        ),
        GradeRow(
            5,
            {
                FactorName.TEST_WEIGHT: _d("46.0"),
                FactorName.HEAT_DAMAGE: _d("3.0"),
                FactorName.TOTAL_DAMAGE: _d("15.0"),
                FactorName.BROKEN: _d("7.0"),
            },
        ),
    ),
)

SOYBEAN_TABLE: Final = GradeTable(
    commodity_code="SOYBEANS",
    rows=(
        GradeRow(
            1,
            {
                FactorName.TEST_WEIGHT: _d("56.0"),
                FactorName.HEAT_DAMAGE: _d("0.2"),
                FactorName.TOTAL_DAMAGE: _d("2.0"),
                FactorName.FOREIGN_MATERIAL: _d("1.0"),
                FactorName.BROKEN: _d("10.0"),
                FactorName.OTHER_CLASSES: _d("1.0"),
            },
        ),
        GradeRow(
            2,
            {
                FactorName.TEST_WEIGHT: _d("54.0"),
                FactorName.HEAT_DAMAGE: _d("0.5"),
                FactorName.TOTAL_DAMAGE: _d("3.0"),
                FactorName.FOREIGN_MATERIAL: _d("2.0"),
                FactorName.BROKEN: _d("20.0"),
                FactorName.OTHER_CLASSES: _d("2.0"),
            },
        ),
        GradeRow(
            3,
            {
                FactorName.TEST_WEIGHT: _d("52.0"),
                FactorName.HEAT_DAMAGE: _d("1.0"),
                FactorName.TOTAL_DAMAGE: _d("5.0"),
                FactorName.FOREIGN_MATERIAL: _d("3.0"),
                FactorName.BROKEN: _d("30.0"),
                FactorName.OTHER_CLASSES: _d("5.0"),
            },
        ),
        GradeRow(
            4,
            {
                FactorName.TEST_WEIGHT: _d("49.0"),
                FactorName.HEAT_DAMAGE: _d("3.0"),
                FactorName.TOTAL_DAMAGE: _d("8.0"),
                FactorName.FOREIGN_MATERIAL: _d("5.0"),
                FactorName.BROKEN: _d("40.0"),
                FactorName.OTHER_CLASSES: _d("10.0"),
            },
        ),
    ),
)

_WHEAT_ROWS: Final = (
    GradeRow(
        1,
        {
            FactorName.TEST_WEIGHT: _d("60.0"),
            FactorName.HEAT_DAMAGE: _d("0.2"),
            FactorName.TOTAL_DAMAGE: _d("2.0"),
            FactorName.FOREIGN_MATERIAL: _d("0.4"),
            FactorName.SHRUNKEN_BROKEN: _d("3.0"),
            FactorName.OTHER_CLASSES: _d("1.0"),
        },
    ),
    GradeRow(
        2,
        {
            FactorName.TEST_WEIGHT: _d("58.0"),
            FactorName.HEAT_DAMAGE: _d("0.2"),
            FactorName.TOTAL_DAMAGE: _d("4.0"),
            FactorName.FOREIGN_MATERIAL: _d("0.7"),
            FactorName.SHRUNKEN_BROKEN: _d("5.0"),
            FactorName.OTHER_CLASSES: _d("2.0"),
        },
    ),
    GradeRow(
        3,
        {
            FactorName.TEST_WEIGHT: _d("56.0"),
            FactorName.HEAT_DAMAGE: _d("0.5"),
            FactorName.TOTAL_DAMAGE: _d("7.0"),
            FactorName.FOREIGN_MATERIAL: _d("1.3"),
            FactorName.SHRUNKEN_BROKEN: _d("8.0"),
            FactorName.OTHER_CLASSES: _d("3.0"),
        },
    ),
    GradeRow(
        4,
        {
            FactorName.TEST_WEIGHT: _d("54.0"),
            FactorName.HEAT_DAMAGE: _d("1.0"),
            FactorName.TOTAL_DAMAGE: _d("10.0"),
            FactorName.FOREIGN_MATERIAL: _d("3.0"),
            FactorName.SHRUNKEN_BROKEN: _d("12.0"),
            FactorName.OTHER_CLASSES: _d("10.0"),
        },
    ),
    GradeRow(
        5,
        {
            FactorName.TEST_WEIGHT: _d("51.0"),
            FactorName.HEAT_DAMAGE: _d("3.0"),
            FactorName.TOTAL_DAMAGE: _d("15.0"),
            FactorName.FOREIGN_MATERIAL: _d("5.0"),
            FactorName.SHRUNKEN_BROKEN: _d("20.0"),
            FactorName.OTHER_CLASSES: _d("10.0"),
        },
    ),
)

HRW_TABLE: Final = GradeTable(commodity_code="HRW_WHEAT", rows=_WHEAT_ROWS)
SRW_TABLE: Final = GradeTable(commodity_code="SRW_WHEAT", rows=_WHEAT_ROWS)

SORGHUM_TABLE: Final = GradeTable(
    commodity_code="GRAIN_SORGHUM",
    rows=(
        GradeRow(
            1,
            {
                FactorName.TEST_WEIGHT: _d("57.0"),
                FactorName.HEAT_DAMAGE: _d("0.2"),
                FactorName.TOTAL_DAMAGE: _d("2.0"),
                FactorName.BROKEN: _d("4.0"),
            },
        ),
        GradeRow(
            2,
            {
                FactorName.TEST_WEIGHT: _d("55.0"),
                FactorName.HEAT_DAMAGE: _d("0.5"),
                FactorName.TOTAL_DAMAGE: _d("5.0"),
                FactorName.BROKEN: _d("8.0"),
            },
        ),
        GradeRow(
            3,
            {
                FactorName.TEST_WEIGHT: _d("53.0"),
                FactorName.HEAT_DAMAGE: _d("1.0"),
                FactorName.TOTAL_DAMAGE: _d("10.0"),
                FactorName.BROKEN: _d("12.0"),
            },
        ),
        GradeRow(
            4,
            {
                FactorName.TEST_WEIGHT: _d("51.0"),
                FactorName.HEAT_DAMAGE: _d("3.0"),
                FactorName.TOTAL_DAMAGE: _d("15.0"),
                FactorName.BROKEN: _d("15.0"),
            },
        ),
    ),
)

OAT_TABLE: Final = GradeTable(
    commodity_code="OATS",
    rows=(
        GradeRow(
            1,
            {
                FactorName.TEST_WEIGHT: _d("36.0"),
                FactorName.HEAT_DAMAGE: _d("0.1"),
                FactorName.TOTAL_DAMAGE: _d("2.0"),
                FactorName.FOREIGN_MATERIAL: _d("2.0"),
                FactorName.OTHER_CLASSES: _d("2.0"),
            },
        ),
        GradeRow(
            2,
            {
                FactorName.TEST_WEIGHT: _d("33.0"),
                FactorName.HEAT_DAMAGE: _d("0.3"),
                FactorName.TOTAL_DAMAGE: _d("3.0"),
                FactorName.FOREIGN_MATERIAL: _d("3.0"),
                FactorName.OTHER_CLASSES: _d("3.0"),
            },
        ),
        GradeRow(
            3,
            {
                FactorName.TEST_WEIGHT: _d("30.0"),
                FactorName.HEAT_DAMAGE: _d("1.0"),
                FactorName.TOTAL_DAMAGE: _d("5.0"),
                FactorName.FOREIGN_MATERIAL: _d("4.0"),
                FactorName.OTHER_CLASSES: _d("5.0"),
            },
        ),
        GradeRow(
            4,
            {
                FactorName.TEST_WEIGHT: _d("27.0"),
                FactorName.HEAT_DAMAGE: _d("3.0"),
                FactorName.TOTAL_DAMAGE: _d("10.0"),
                FactorName.FOREIGN_MATERIAL: _d("5.0"),
                FactorName.OTHER_CLASSES: _d("10.0"),
            },
        ),
    ),
)

SUNFLOWER_TABLE: Final = GradeTable(
    commodity_code="SUNFLOWER",
    rows=(
        GradeRow(
            1,
            {
                FactorName.TEST_WEIGHT: _d("25.0"),
                FactorName.HEAT_DAMAGE: _d("0.5"),
                FactorName.TOTAL_DAMAGE: _d("5.0"),
                FactorName.FOREIGN_MATERIAL: _d("2.0"),
            },
        ),
        GradeRow(
            2,
            {
                FactorName.TEST_WEIGHT: _d("22.0"),
                FactorName.HEAT_DAMAGE: _d("1.0"),
                FactorName.TOTAL_DAMAGE: _d("10.0"),
                FactorName.FOREIGN_MATERIAL: _d("5.0"),
            },
        ),
    ),
)

TABLES: Final[dict[str, GradeTable]] = {
    table.commodity_code: table
    for table in (
        CORN_TABLE,
        SOYBEAN_TABLE,
        HRW_TABLE,
        SRW_TABLE,
        SORGHUM_TABLE,
        OAT_TABLE,
        SUNFLOWER_TABLE,
    )
}


def table_for(commodity_code: str) -> GradeTable:
    try:
        return TABLES[commodity_code]
    except KeyError as exc:
        raise ConfigurationError(
            "no grade table is loaded for that crop",
            commodity=commodity_code,
            loaded=sorted(TABLES),
        ) from exc
