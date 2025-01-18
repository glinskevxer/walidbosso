"""Weights, and the arithmetic the scale house does with them.

Everything the elevator weighs is recorded in **hundredths of a pound**, which
is the smallest division a certified truck scale prints, and is held in an
``int``. Floats are never used for a weight: a settlement statement that adds
up nine tickets has to close to the cent, and binary floating point does not
give that back reliably once shrink percentages get involved.

The public type is :class:`Weight`. It is immutable, it compares and hashes,
and it refuses to be added to a plain number so a stray ``+ 250`` cannot
silently mean "two hundred and fifty hundredths of a pound".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from binyard.errors import IncompatibleUnits, ValidationError

__all__ = [
    "HUNDREDTHS_PER_POUND",
    "POUNDS_PER_CWT",
    "POUNDS_PER_TON",
    "Weight",
    "pounds",
    "cwt",
    "tons",
    "parse_weight",
]

HUNDREDTHS_PER_POUND: Final = 100
POUNDS_PER_CWT: Final = 100
POUNDS_PER_TON: Final = 2000

_WEIGHT_TEXT = re.compile(
    r"^\s*(?P<sign>[-+]?)(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>lb|lbs|pound|pounds|cwt|ton|tons)?\s*$",
    re.IGNORECASE,
)

_UNIT_ALIASES: Final[dict[str, str]] = {
    "lb": "lb",
    "lbs": "lb",
    "pound": "lb",
    "pounds": "lb",
    "cwt": "cwt",
    "ton": "ton",
    "tons": "ton",
}


@dataclass(frozen=True, order=True, slots=True)
class Weight:
    """A mass, carried as an exact count of hundredths of a pound."""

    hundredths: int

    def __post_init__(self) -> None:
        if not isinstance(self.hundredths, int) or isinstance(self.hundredths, bool):
            raise ValidationError(
                "a weight is an integer count of hundredths of a pound",
                got=type(self.hundredths).__name__,
            )

    # -- construction ----------------------------------------------------

    @classmethod
    def from_pounds(cls, value: Decimal | int | str) -> Weight:
        return cls(_scaled(value, HUNDREDTHS_PER_POUND))

    @classmethod
    def from_cwt(cls, value: Decimal | int | str) -> Weight:
        return cls(_scaled(value, HUNDREDTHS_PER_POUND * POUNDS_PER_CWT))

    @classmethod
    def from_tons(cls, value: Decimal | int | str) -> Weight:
        return cls(_scaled(value, HUNDREDTHS_PER_POUND * POUNDS_PER_TON))

    @classmethod
    def zero(cls) -> Weight:
        return cls(0)

    # -- reading ---------------------------------------------------------

    @property
    def pounds(self) -> Decimal:
        return Decimal(self.hundredths) / HUNDREDTHS_PER_POUND

    @property
    def cwt(self) -> Decimal:
        return self.pounds / POUNDS_PER_CWT

    @property
    def tons(self) -> Decimal:
        return self.pounds / POUNDS_PER_TON

    @property
    def whole_pounds(self) -> int:
        """The weight rounded to the pound, which is what a ticket prints."""
        return int(
            (Decimal(self.hundredths) / HUNDREDTHS_PER_POUND).quantize(
                Decimal("1"), rounding=ROUND_HALF_UP
            )
        )

    def is_zero(self) -> bool:
        return self.hundredths == 0

    def is_positive(self) -> bool:
        return self.hundredths > 0

    # -- arithmetic ------------------------------------------------------

    def __add__(self, other: Weight) -> Weight:
        _require_weight(other, "add")
        return Weight(self.hundredths + other.hundredths)

    def __sub__(self, other: Weight) -> Weight:
        _require_weight(other, "subtract")
        return Weight(self.hundredths - other.hundredths)

    def __neg__(self) -> Weight:
        return Weight(-self.hundredths)

    def __abs__(self) -> Weight:
        return Weight(abs(self.hundredths))

    def scaled_by(self, factor: Decimal | int | str) -> Weight:
        """Multiply by a dimensionless factor, rounding half up to the hundredth."""
        product = Decimal(self.hundredths) * Decimal(str(factor))
        return Weight(int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def ratio_to(self, other: Weight) -> Decimal:
        _require_weight(other, "divide by")
        if other.hundredths == 0:
            raise ValidationError("cannot take a ratio against a zero weight")
        return Decimal(self.hundredths) / Decimal(other.hundredths)

    # -- rendering -------------------------------------------------------

    def __str__(self) -> str:
        return f"{self.pounds.normalize():f} lb"

    def __repr__(self) -> str:
        return f"Weight.from_pounds('{self.pounds}')"


def _require_weight(other: object, verb: str) -> None:
    if not isinstance(other, Weight):
        raise IncompatibleUnits(
            f"cannot {verb} a weight and a {type(other).__name__}",
            other=type(other).__name__,
        )


def _scaled(value: Decimal | int | str, per_unit: int) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal raises several types
        raise ValidationError("weight is not a number", got=repr(value)) from exc
    if not amount.is_finite():
        raise ValidationError("weight must be finite", got=str(value))
    scaled = amount * per_unit
    if scaled != scaled.to_integral_value():
        raise ValidationError(
            "weight is finer than a hundredth of a pound",
            got=str(value),
        )
    return int(scaled)


def pounds(value: Decimal | int | str) -> Weight:
    """Shorthand for :meth:`Weight.from_pounds`."""
    return Weight.from_pounds(value)


def cwt(value: Decimal | int | str) -> Weight:
    """Shorthand for :meth:`Weight.from_cwt`."""
    return Weight.from_cwt(value)


def tons(value: Decimal | int | str) -> Weight:
    """Shorthand for :meth:`Weight.from_tons`."""
    return Weight.from_tons(value)


def parse_weight(text: str) -> Weight:
    """Read a weight the way it is written on a ticket or typed at the CLI.

    ``"48200"`` and ``"48200 lb"`` mean the same thing; the unit is optional
    and defaults to pounds because that is what the scale prints.
    """
    match = _WEIGHT_TEXT.match(text)
    if match is None:
        raise ValidationError("could not read that as a weight", got=text)
    unit = _UNIT_ALIASES[(match.group("unit") or "lb").lower()]
    magnitude = Decimal(match.group("value"))
    if match.group("sign") == "-":
        magnitude = -magnitude
    if unit == "lb":
        return Weight.from_pounds(magnitude)
    if unit == "cwt":
        return Weight.from_cwt(magnitude)
    return Weight.from_tons(magnitude)
