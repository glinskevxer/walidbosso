"""Money, prices and the one place a product gets rounded.

Two exact types, both integers underneath:

:class:`Money` counts cents. Anything that lands on a settlement statement, a
storage invoice or a position report is Money.

:class:`Price` counts ten-thousandths of a dollar, which is a quarter of a
cent divided by twenty five. Grain is quoted in quarter cents, so ``$4.5225``
has to be representable without apology, and a discount schedule that takes
half a cent per point of moisture has to be able to say so.

Multiplying a Price by a quantity is the only rounding step in the whole
package that a customer sees, so it lives in exactly one method,
:meth:`Price.extend`, and every caller goes through it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from binyard.bushels import Bushels
from binyard.errors import IncompatibleUnits, ValidationError

__all__ = ["CENTS_PER_DOLLAR", "TICKS_PER_DOLLAR", "Money", "Price", "dollars", "price"]

CENTS_PER_DOLLAR: Final = 100
TICKS_PER_DOLLAR: Final = 10_000
_CENT: Final = Decimal("0.01")
_TICK: Final = Decimal("0.0001")

_MONEY_TEXT = re.compile(
    r"^\s*(?P<sign>[-+]?)\$?\s*(?P<value>\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*$"
)


@dataclass(frozen=True, order=True, slots=True)
class Money:
    """An amount of money, as an exact number of cents."""

    cents: int

    def __post_init__(self) -> None:
        if not isinstance(self.cents, int) or isinstance(self.cents, bool):
            raise ValidationError(
                "money is an integer number of cents", got=type(self.cents).__name__
            )

    @classmethod
    def of(cls, value: Decimal | int | str) -> Money:
        return cls(_exact(value, CENTS_PER_DOLLAR, "cent"))

    @classmethod
    def zero(cls) -> Money:
        return cls(0)

    @classmethod
    def parse(cls, text: str) -> Money:
        match = _MONEY_TEXT.match(text)
        if match is None:
            raise ValidationError("could not read that as an amount", got=text)
        amount = Decimal(match.group("value").replace(",", ""))
        if match.group("sign") == "-":
            amount = -amount
        return cls.of(amount)

    @property
    def amount(self) -> Decimal:
        return (Decimal(self.cents) / CENTS_PER_DOLLAR).quantize(_CENT)

    def is_zero(self) -> bool:
        return self.cents == 0

    def __add__(self, other: Money) -> Money:
        _require(other, Money, "add")
        return Money(self.cents + other.cents)

    def __sub__(self, other: Money) -> Money:
        _require(other, Money, "subtract")
        return Money(self.cents - other.cents)

    def __neg__(self) -> Money:
        return Money(-self.cents)

    def __abs__(self) -> Money:
        return Money(abs(self.cents))

    def scaled_by(self, factor: Decimal | int | str) -> Money:
        product = Decimal(self.cents) * Decimal(str(factor))
        return Money(int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def allocate(self, weights: list[int]) -> list[Money]:
        """Split across ``weights`` so the parts sum back to the whole exactly.

        Used when one settlement line has to be shared across several owners of
        a commingled bin. Remainder cents go to the largest weights first, which
        is arbitrary but stable, and stability is what an auditor asks for.
        """
        if not weights or any(w < 0 for w in weights):
            raise ValidationError("allocation weights must be a non empty non negative list")
        total = sum(weights)
        if total == 0:
            raise ValidationError("allocation weights sum to zero")
        shares = [self.cents * w // total for w in weights]
        remainder = self.cents - sum(shares)
        order = sorted(range(len(weights)), key=lambda i: (-weights[i], i))
        step = 1 if remainder >= 0 else -1
        for index in order[: abs(remainder)]:
            shares[index] += step
        return [Money(c) for c in shares]

    def __str__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        return f"{sign}${abs(self.amount):,.2f}"

    def __repr__(self) -> str:
        return f"Money.of('{self.amount}')"


@dataclass(frozen=True, order=True, slots=True)
class Price:
    """A price per unit, as an exact number of ten-thousandths of a dollar."""

    ticks: int

    def __post_init__(self) -> None:
        if not isinstance(self.ticks, int) or isinstance(self.ticks, bool):
            raise ValidationError(
                "a price is an integer number of ticks", got=type(self.ticks).__name__
            )

    @classmethod
    def of(cls, value: Decimal | int | str) -> Price:
        return cls(_exact(value, TICKS_PER_DOLLAR, "hundredth of a cent"))

    @classmethod
    def from_cents(cls, value: Decimal | int | str) -> Price:
        return cls(_exact(value, TICKS_PER_DOLLAR // CENTS_PER_DOLLAR, "hundredth of a cent"))

    @classmethod
    def zero(cls) -> Price:
        return cls(0)

    @property
    def amount(self) -> Decimal:
        return (Decimal(self.ticks) / TICKS_PER_DOLLAR).quantize(_TICK)

    @property
    def cents(self) -> Decimal:
        return self.amount * CENTS_PER_DOLLAR

    def __add__(self, other: Price) -> Price:
        _require(other, Price, "add")
        return Price(self.ticks + other.ticks)

    def __sub__(self, other: Price) -> Price:
        _require(other, Price, "subtract")
        return Price(self.ticks - other.ticks)

    def __neg__(self) -> Price:
        return Price(-self.ticks)

    def extend(self, quantity: Bushels) -> Money:
        """Price times quantity, rounded half up to the cent. The only rounding."""
        if not isinstance(quantity, Bushels):
            raise IncompatibleUnits("a price extends over bushels", got=type(quantity).__name__)
        product = (
            Decimal(self.ticks)
            * Decimal(quantity.hundredths)
            * CENTS_PER_DOLLAR
            / (TICKS_PER_DOLLAR * 100)
        )
        return Money(int(product.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def __str__(self) -> str:
        text = f"{self.amount:.4f}".rstrip("0").rstrip(".")
        return f"${text}"

    def __repr__(self) -> str:
        return f"Price.of('{self.amount}')"


def _require(other: object, kind: type, verb: str) -> None:
    if not isinstance(other, kind):
        raise IncompatibleUnits(
            f"cannot {verb} {kind.__name__.lower()} and a {type(other).__name__}",
            other=type(other).__name__,
        )


def _exact(value: Decimal | int | str, per_unit: int, unit_name: str) -> int:
    try:
        amount = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - Decimal raises several types
        raise ValidationError("not a number", got=repr(value)) from exc
    if not amount.is_finite():
        raise ValidationError("must be finite", got=str(value))
    scaled = amount * per_unit
    if scaled != scaled.to_integral_value():
        raise ValidationError(f"finer than a {unit_name}", got=str(value))
    return int(scaled)


def dollars(value: Decimal | int | str) -> Money:
    """Shorthand for :meth:`Money.of`."""
    return Money.of(value)


def price(value: Decimal | int | str) -> Price:
    """Shorthand for :meth:`Price.of`."""
    return Price.of(value)
