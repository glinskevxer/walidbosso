"""Applying a posted board to a graded load.

The awkward part is not the arithmetic, it is the overlap. A load of corn at
7% damage grades No. 3, and the board charges both a No. 3 grade discount and a
damage discount. Charge both and the grower is billed twice for one defect.
Charge neither and light test weight, which usually has no band of its own,
goes free.

Houses settle this by saying the grade discount only bites when the grade was
set by something the board does not already price. That rule is carried on the
schedule rather than assumed here, because a minority of houses do charge both
and their statements have to reconcile too.

Every charge is a line. Nothing is netted before it is printed, so a stub shows
the reading, the rate the board gave it and the money, and a grower can check
all three against the sign on the wall.
"""

from __future__ import annotations

from dataclasses import dataclass

from binyard.bushels import Bushels
from binyard.discounts.schedule import Basis, PostedSchedule
from binyard.errors import ValidationError
from binyard.grading.factors import FactorName
from binyard.grading.grade import GradeResult
from binyard.money import Money, Price

__all__ = ["DiscountLine", "Assessment", "assess"]


@dataclass(frozen=True, slots=True)
class DiscountLine:
    """One charge, with everything needed to defend it."""

    source: str
    reading: str
    rate: Price
    amount: Money
    note: str = ""

    def describe(self) -> dict[str, str]:
        return {
            "source": self.source,
            "reading": self.reading,
            "rate": str(self.rate),
            "amount": str(self.amount),
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Assessment:
    """Everything the board takes off one load."""

    quantity: Bushels
    lines: tuple[DiscountLine, ...]
    rejected: bool = False
    rejecting_factor: FactorName | None = None

    @property
    def total_rate(self) -> Price:
        total = Price.zero()
        for line in self.lines:
            total = total + line.rate
        return total

    @property
    def total(self) -> Money:
        total = Money.zero()
        for line in self.lines:
            total = total + line.amount
        return total

    def line_for(self, source: str) -> DiscountLine | None:
        for line in self.lines:
            if line.source == source:
                return line
        return None

    def describe(self) -> dict[str, object]:
        return {
            "quantity": str(self.quantity),
            "rejected": self.rejected,
            "rejecting_factor": (
                self.rejecting_factor.value if self.rejecting_factor else None
            ),
            "total_rate": str(self.total_rate),
            "total": str(self.total),
            "lines": [line.describe() for line in self.lines],
        }


def assess(
    result: GradeResult,
    quantity: Bushels,
    schedule: PostedSchedule,
    *,
    grade_discount_excludes_scheduled_factors: bool = True,
) -> Assessment:
    """Price every defect on one graded load."""
    if result.commodity_code != schedule.commodity_code:
        raise ValidationError(
            "the posted board is for a different crop",
            graded=result.commodity_code,
            posted=schedule.commodity_code,
        )
    if quantity.hundredths < 0:
        raise ValidationError("cannot discount a negative quantity", quantity=str(quantity))

    sample = result.graded_sample
    lines: list[DiscountLine] = []

    for posted in schedule.factors:
        reading = sample.reading(posted.factor)
        if posted.rejects(reading):
            return Assessment(
                quantity=quantity,
                lines=(),
                rejected=True,
                rejecting_factor=posted.factor,
            )
        rate = posted.rate_for(reading)
        if rate == Price.zero():
            continue
        lines.append(
            DiscountLine(
                source=posted.factor.value,
                reading=str(reading),
                rate=rate,
                amount=rate.extend(quantity),
                note=_band_note(posted, reading),
            )
        )

    grade_rate = schedule.grades.rate_for(result.grade)
    if grade_rate != Price.zero() and _grade_charges(
        result, schedule, grade_discount_excludes_scheduled_factors
    ):
        lines.append(
            DiscountLine(
                source="grade",
                reading=result.label,
                rate=grade_rate,
                amount=grade_rate.extend(quantity),
                note=_grade_note(result),
            )
        )

    return Assessment(quantity=quantity, lines=tuple(lines))


def _grade_charges(
    result: GradeResult, schedule: PostedSchedule, exclude_scheduled: bool
) -> bool:
    if not exclude_scheduled:
        return True
    if result.is_sample_grade:
        return True
    if not result.limiting:
        return True
    return any(
        schedule.schedule_for(item.factor) is None for item in result.limiting
    )


def _band_note(posted, reading) -> str:
    band = posted.band_for(reading)
    if band is None:
        return ""
    upper = "and over" if band.is_open_ended else f"to {band.upper}"
    if band.basis is Basis.FLAT:
        return f"flat charge for {band.lower} {upper}"
    unit = "point" if band.basis is Basis.PER_POINT else "tenth"
    return f"{band.lower} {upper} at {band.rate} per {unit}"


def _grade_note(result: GradeResult) -> str:
    if not result.limiting:
        return "grade discount"
    factors = ", ".join(item.factor.value for item in result.limiting)
    return f"grade set by {factors}, which the board does not price separately"
