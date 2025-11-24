"""Bushels, and the crossing from scale weight into them.

A bushel here is an exact count of hundredths, the same trick the weight type
uses and for the same reason. Elevators print bushels to two decimals and every
downstream number, storage charges, settlement, position, is built by summing
those printed figures. If the conversion carried a float the printed column and
the stored column would drift apart within a week.

The conversion itself is one division, but *where the rounding happens* is the
whole argument. A load is converted once, at receiving, to the hundredth of a
bushel, and that figure is the load forever. Nothing later reconstitutes
bushels from pounds, because doing it twice with different roundings is how a
bin ends up eleven bushels short with no ticket to blame.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from binyard.commodity import CropStandard
from binyard.errors import IncompatibleUnits, ValidationError
from binyard.units import Weight

__all__ = ["HUNDREDTHS_PER_BUSHEL", "Bushels", "bushels", "from_weight", "to_weight"]

HUNDREDTHS_PER_BUSHEL: Final = 100
_HUNDREDTH: Final = Decimal("0.01")


@dataclass(frozen=True, order=True, slots=True)
class Bushels:
    """A quantity of grain, as an exact count of hundredths of a bushel."""

    hundredths: int

    def __post_init__(self) -> None:
        if not isinstance(self.hundredths, int) or isinstance(self.hundredths, bool):
            raise ValidationError(
                "bushels are an integer count of hundredths",
                got=type(self.hundredths).__name__,
            )

    @classmethod
    def of(cls, value: Decimal | int | str) -> Bushels:
        try:
            amount = Decimal(str(value))
        except Exception as exc:  # pragma: no cover - Decimal raises several types
            raise ValidationError("bushels are not a number", got=repr(value)) from exc
        if not amount.is_finite():
            raise ValidationError("bushels must be finite", got=str(value))
        scaled = amount * HUNDREDTHS_PER_BUSHEL
        if scaled != scaled.to_integral_value():
            raise ValidationError("finer than a hundredth of a bushel", got=str(value))
        return cls(int(scaled))

    @classmethod
    def zero(cls) -> Bushels:
        return cls(0)

    @property
    def amount(self) -> Decimal:
        return (Decimal(self.hundredths) / HUNDREDTHS_PER_BUSHEL).quantize(_HUNDREDTH)

    @property
    def whole(self) -> int:
        """Rounded to the bushel, which is the unit a load-out order uses."""
        return int(self.amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def is_zero(self) -> bool:
        return self.hundredths == 0

    def is_positive(self) -> bool:
        return self.hundredths > 0

    def __add__(self, other: Bushels) -> Bushels:
        _require_bushels(other, "add")
        return Bushels(self.hundredths + other.hundredths)

    def __sub__(self, other: Bushels) -> Bushels:
        _require_bushels(other, "subtract")
        return Bushels(self.hundredths - other.hundredths)

    def __neg__(self) -> Bushels:
        return Bushels(-self.hundredths)

    def __abs__(self) -> Bushels:
        return Bushels(abs(self.hundredths))

    def scaled_by(self, factor: Decimal | int | str) -> Bushels:
        product = Decimal(self.hundredths) * Decimal(str(factor))
        return Bushels(int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def share_of(self, other: Bushels) -> Decimal:
        _require_bushels(other, "divide by")
        if other.hundredths == 0:
            raise ValidationError("cannot take a share of zero bushels")
        return Decimal(self.hundredths) / Decimal(other.hundredths)

    def split(self, parts: int) -> tuple[Bushels, ...]:
        """Divide into ``parts`` that sum back exactly, largest remainder first."""
        if parts < 1:
            raise ValidationError("cannot split into fewer than one part", parts=parts)
        base, remainder = divmod(self.hundredths, parts)
        return tuple(Bushels(base + (1 if index < remainder else 0)) for index in range(parts))

    def __str__(self) -> str:
        return f"{self.amount} bu"

    def __repr__(self) -> str:
        return f"Bushels.of('{self.amount}')"


def _require_bushels(other: object, verb: str) -> None:
    if not isinstance(other, Bushels):
        raise IncompatibleUnits(
            f"cannot {verb} bushels and a {type(other).__name__}",
            other=type(other).__name__,
        )


def bushels(value: Decimal | int | str) -> Bushels:
    """Shorthand for :meth:`Bushels.of`."""
    return Bushels.of(value)


def from_weight(weight: Weight, standard: CropStandard) -> Bushels:
    """Convert scale pounds into bushels using the crop's fixed bushel weight.

    Rounds half up at the hundredth of a bushel. This is the only place the
    crossing is made; see the module docstring for why that matters.
    """
    if not isinstance(weight, Weight):
        raise IncompatibleUnits("expected a weight to convert", got=type(weight).__name__)
    per_bushel_hundredths = standard.pounds_per_bushel * HUNDREDTHS_PER_BUSHEL
    quotient = Decimal(weight.hundredths) * HUNDREDTHS_PER_BUSHEL / per_bushel_hundredths
    return Bushels(int(quotient.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def to_weight(quantity: Bushels, standard: CropStandard) -> Weight:
    """Convert bushels back to pounds, for a load-out order written in bushels."""
    if not isinstance(quantity, Bushels):
        raise IncompatibleUnits("expected bushels to convert", got=type(quantity).__name__)
    product = Decimal(quantity.hundredths) * standard.pounds_per_bushel
    return Weight(int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
