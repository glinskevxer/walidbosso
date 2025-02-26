"""Determining the grade, and saying what held it there.

The walk itself is short: rebase the sample if the crop is graded after dockage
is removed, then take the best row whose limits the readings all satisfy. What
takes the code is the second question the grain desk always asks next, which is
*what stopped it going one better*. That answer decides whether the load is
worth cleaning, worth blending, or worth nothing, so it is computed here rather
than reconstructed by eye off a stub.

A special condition is not a number and does not walk the table. Infestation
and a musty or sour smell ride alongside the numeric grade as designations. A
commercially objectionable odor is different in kind: it puts the load in
sample grade whatever the numbers said, because no buyer will take it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from binyard.commodity import CropStandard
from binyard.grading.factors import Direction, FactorName, FactorSample, Odor, direction_of
from binyard.grading.tables import SAMPLE_GRADE, GradeRow, GradeTable, table_for

__all__ = ["SpecialGrade", "LimitingFactor", "GradeResult", "determine"]


class SpecialGrade(StrEnum):
    """A designation that rides alongside the number."""

    INFESTED = "infested"
    MUSTY = "musty"
    SOUR = "sour"
    COMMERCIALLY_OBJECTIONABLE = "commercially_objectionable"


@dataclass(frozen=True, slots=True)
class LimitingFactor:
    """One factor, and how far it is from the limit it missed."""

    factor: FactorName
    reading: Decimal
    limit: Decimal

    @property
    def margin(self) -> Decimal:
        """How far the reading has to move to clear the limit, always positive."""
        if direction_of(self.factor) is Direction.HIGHER_IS_BETTER:
            return self.limit - self.reading
        return self.reading - self.limit

    def describe(self) -> str:
        if direction_of(self.factor) is Direction.HIGHER_IS_BETTER:
            return f"{self.factor.value} {self.reading} is under the {self.limit} minimum"
        return f"{self.factor.value} {self.reading} is over the {self.limit} maximum"


@dataclass(frozen=True, slots=True)
class GradeResult:
    """What the sample graded, and why."""

    commodity_code: str
    grade: int
    graded_sample: FactorSample
    limiting: tuple[LimitingFactor, ...] = ()
    special: tuple[SpecialGrade, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def is_sample_grade(self) -> bool:
        return self.grade == SAMPLE_GRADE

    @property
    def label(self) -> str:
        base = "US Sample grade" if self.is_sample_grade else f"US No. {self.grade}"
        if not self.special:
            return base
        return f"{base}, {', '.join(s.value.replace('_', ' ') for s in self.special)}"

    @property
    def binding_factor(self) -> FactorName | None:
        """The single factor holding the grade back, when there is exactly one."""
        if len(self.limiting) == 1:
            return self.limiting[0].factor
        return None

    def describe(self) -> dict[str, object]:
        return {
            "commodity": self.commodity_code,
            "grade": self.grade,
            "label": self.label,
            "limiting": [
                {
                    "factor": item.factor.value,
                    "reading": str(item.reading),
                    "limit": str(item.limit),
                    "margin": str(item.margin),
                }
                for item in self.limiting
            ],
            "special": [s.value for s in self.special],
            "notes": list(self.notes),
        }


def _readings(sample: FactorSample, table: GradeTable) -> dict[FactorName, Decimal]:
    return {factor: sample.reading(factor) for factor in table.factors}


def _limiting(row: GradeRow, readings: dict[FactorName, Decimal]) -> tuple[LimitingFactor, ...]:
    return tuple(
        LimitingFactor(factor=factor, reading=readings[factor], limit=row.limits[factor])
        for factor in row.failures(readings)
    )


def _special_grades(sample: FactorSample) -> tuple[SpecialGrade, ...]:
    found: list[SpecialGrade] = []
    if sample.infested:
        found.append(SpecialGrade.INFESTED)
    if sample.odor is Odor.MUSTY:
        found.append(SpecialGrade.MUSTY)
    elif sample.odor is Odor.SOUR:
        found.append(SpecialGrade.SOUR)
    elif sample.odor is Odor.COMMERCIALLY_OBJECTIONABLE:
        found.append(SpecialGrade.COMMERCIALLY_OBJECTIONABLE)
    return tuple(found)


def determine(sample: FactorSample, standard: CropStandard) -> GradeResult:
    """Grade one composite sample against the table for its crop."""
    table = table_for(standard.code)
    notes: list[str] = []

    graded = sample
    if standard.dockage_removed_before_grading and not sample.dockage.is_zero():
        graded = sample.without_dockage()
        notes.append(f"graded after removing {sample.dockage} dockage")

    readings = _readings(graded, table)
    special = _special_grades(graded)

    if graded.odor is Odor.COMMERCIALLY_OBJECTIONABLE:
        notes.append("commercially objectionable odor forces sample grade")
        return GradeResult(
            commodity_code=standard.code,
            grade=SAMPLE_GRADE,
            graded_sample=graded,
            limiting=(),
            special=special,
            notes=tuple(notes),
        )

    for index, row in enumerate(table.rows):
        if row.accepts(readings):
            limiting: tuple[LimitingFactor, ...] = ()
            if index > 0:
                limiting = _limiting(table.rows[index - 1], readings)
            return GradeResult(
                commodity_code=standard.code,
                grade=row.grade,
                graded_sample=graded,
                limiting=limiting,
                special=special,
                notes=tuple(notes),
            )

    return GradeResult(
        commodity_code=standard.code,
        grade=SAMPLE_GRADE,
        graded_sample=graded,
        limiting=_limiting(table.rows[-1], readings),
        special=special,
        notes=tuple(notes),
    )
