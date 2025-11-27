"""Percentages, held in hundredths of a point.

Grade factors are read off instruments that print one decimal place: moisture
15.4%, damage 3.2%, foreign material 1.8%. Discount schedules, though, are
written in half points and quarter points, and shrink formulas divide by
``100 - moisture``. Hundredths of a point give room for both without ever
reaching for a float.

The type deliberately does **not** implement multiplication by another
percentage. "Three percent of the damage" is a different quantity from "three
percent damage" and the places in this package that need the first one say so
explicitly with :meth:`Percent.of`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from binyard.errors import IncompatibleUnits, ValidationError

__all__ = ["HUNDREDTHS_PER_POINT", "Percent", "percent", "weighted_average"]

HUNDREDTHS_PER_POINT: Final = 100
_HUNDREDTH: Final = Decimal("0.01")


@dataclass(frozen=True, order=True, slots=True)
class Percent:
    """A percentage, as an exact count of hundredths of a point."""

    hundredths: int

    def __post_init__(self) -> None:
        if not isinstance(self.hundredths, int) or isinstance(self.hundredths, bool):
            raise ValidationError(
                "a percentage is an integer count of hundredths of a point",
                got=type(self.hundredths).__name__,
            )

    @classmethod
    def of_points(cls, value: Decimal | int | str) -> Percent:
        try:
            amount = Decimal(str(value))
        except Exception as exc:  # pragma: no cover - Decimal raises several types
            raise ValidationError("not a number", got=repr(value)) from exc
        if not amount.is_finite():
            raise ValidationError("a percentage must be finite", got=str(value))
        scaled = amount * HUNDREDTHS_PER_POINT
        if scaled != scaled.to_integral_value():
            raise ValidationError("finer than a hundredth of a point", got=str(value))
        return cls(int(scaled))

    @classmethod
    def zero(cls) -> Percent:
        return cls(0)

    @property
    def points(self) -> Decimal:
        return (Decimal(self.hundredths) / HUNDREDTHS_PER_POINT).quantize(_HUNDREDTH)

    @property
    def fraction(self) -> Decimal:
        """The value as a plain ratio, so 15.5% reads back as ``0.155``."""
        return Decimal(self.hundredths) / (HUNDREDTHS_PER_POINT * 100)

    @property
    def tenths(self) -> Decimal:
        """Rounded to the tenth of a point, which is what the meter prints."""
        return self.points.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)

    def is_zero(self) -> bool:
        return self.hundredths == 0

    def __add__(self, other: Percent) -> Percent:
        _require(other, "add")
        return Percent(self.hundredths + other.hundredths)

    def __sub__(self, other: Percent) -> Percent:
        _require(other, "subtract")
        return Percent(self.hundredths - other.hundredths)

    def __neg__(self) -> Percent:
        return Percent(-self.hundredths)

    def __abs__(self) -> Percent:
        return Percent(abs(self.hundredths))

    def scaled_by(self, factor: Decimal | int | str) -> Percent:
        product = Decimal(self.hundredths) * Decimal(str(factor))
        return Percent(int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def of(self, amount: Decimal | int | str) -> Decimal:
        """Take this percentage *of* a plain number, exactly."""
        return Decimal(str(amount)) * self.fraction

    def clamped(self, low: Percent, high: Percent) -> Percent:
        if low > high:
            raise ValidationError("clamp bounds are the wrong way round")
        return min(max(self, low), high)

    def __str__(self) -> str:
        return f"{self.points.normalize():f}%"

    def __repr__(self) -> str:
        return f"Percent.of_points('{self.points}')"


def _require(other: object, verb: str) -> None:
    if not isinstance(other, Percent):
        raise IncompatibleUnits(
            f"cannot {verb} a percentage and a {type(other).__name__}",
            other=type(other).__name__,
        )


def percent(value: Decimal | int | str) -> Percent:
    """Shorthand for :meth:`Percent.of_points`."""
    return Percent.of_points(value)


def weighted_average(pairs: list[tuple[Percent, int]]) -> Percent:
    """Average percentages by an integer weight, rounding half up.

    Used every time two lots are commingled: the bin's moisture afterwards is
    the bushel weighted average of what went in, not the mean of the readings.
    """
    if not pairs:
        raise ValidationError("nothing to average")
    total_weight = sum(weight for _, weight in pairs)
    if total_weight <= 0:
        raise ValidationError("weights must sum to something positive", total=total_weight)
    numerator = Decimal(0)
    for value, weight in pairs:
        numerator += Decimal(value.hundredths) * weight
    quotient = numerator / total_weight
    return Percent(int(quotient.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
