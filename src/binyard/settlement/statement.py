"""The settlement statement: what the grower actually gets paid.

Everything upstream produces one number each. This is where they line up and
get subtracted from each other, and the order matters because two of the
deductions are computed off the results of the others.

The order is fixed:

1. **Grain value.** Net bushels times the contract price.
2. **Discounts.** Off the board, per bushel, computed on net bushels.
3. **Drying.** Charged per point removed per bushel, when the house dried it.
4. **Storage.** Accrued separately, and already in dollars.
5. **Checkoff.** A statutory assessment. Corn is a flat rate per bushel;
   soybeans are half a percent **of the value after discounts**, which is why
   it cannot be computed before the discounts are known.
6. **Advances and hauling.** Anything already paid out, netted back.

A statement is immutable and carries every line. Nothing is combined before it
prints, and no line is dropped for being zero if it was computed, because a
grower who sees no storage line cannot tell whether storage was free or
forgotten.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.money import Money, Price
from binyard.percent import Percent

__all__ = ["ChargeKind", "Checkoff", "StatementLine", "Settlement", "SettlementBuilder"]


class ChargeKind(StrEnum):
    """What a line is, which decides its sign and where it sorts."""

    GRAIN = "grain"
    DISCOUNT = "discount"
    DRYING = "drying"
    STORAGE = "storage"
    CHECKOFF = "checkoff"
    HAULING = "hauling"
    ADVANCE = "advance"
    ADJUSTMENT = "adjustment"

    @property
    def is_credit(self) -> bool:
        return self is ChargeKind.GRAIN


_ORDER: dict[ChargeKind, int] = {
    ChargeKind.GRAIN: 0,
    ChargeKind.DISCOUNT: 1,
    ChargeKind.DRYING: 2,
    ChargeKind.STORAGE: 3,
    ChargeKind.CHECKOFF: 4,
    ChargeKind.HAULING: 5,
    ChargeKind.ADVANCE: 6,
    ChargeKind.ADJUSTMENT: 7,
}


@dataclass(frozen=True, slots=True)
class Checkoff:
    """A statutory assessment, either per bushel or off the value."""

    name: str
    per_bushel: Price | None = None
    percent_of_value: Percent | None = None

    def __post_init__(self) -> None:
        if (self.per_bushel is None) == (self.percent_of_value is None):
            raise ValidationError(
                "a checkoff is either per bushel or a percentage, not both or neither",
                checkoff=self.name,
            )

    def assess(self, quantity: Bushels, value: Money) -> Money:
        if self.per_bushel is not None:
            return self.per_bushel.extend(quantity)
        assert self.percent_of_value is not None
        exact = Decimal(value.cents) * self.percent_of_value.fraction
        return Money(int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


@dataclass(frozen=True, slots=True)
class StatementLine:
    """One line. ``amount`` is what it adds to the settlement, so charges are negative."""

    kind: ChargeKind
    description: str
    amount: Money
    quantity: Bushels | None = None
    rate: Price | None = None

    def __post_init__(self) -> None:
        if not self.description.strip():
            raise ValidationError("a statement line needs a description")
        if self.kind.is_credit and self.amount.cents < 0:
            raise ValidationError(
                "a grain line cannot be negative", description=self.description
            )

    def describe(self) -> dict[str, str | None]:
        return {
            "kind": self.kind.value,
            "description": self.description,
            "quantity": None if self.quantity is None else str(self.quantity),
            "rate": None if self.rate is None else str(self.rate),
            "amount": str(self.amount),
        }


@dataclass(frozen=True, slots=True)
class Settlement:
    """A finished statement, ready to print and pay."""

    number: str
    account: str
    commodity_code: str
    on: date
    quantity: Bushels
    lines: tuple[StatementLine, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.number.strip():
            raise ValidationError("a settlement needs a number")
        if not self.account.strip():
            raise ValidationError("a settlement needs an account")

    @property
    def gross(self) -> Money:
        total = Money.zero()
        for line in self.lines:
            if line.kind.is_credit:
                total = total + line.amount
        return total

    @property
    def deductions(self) -> Money:
        total = Money.zero()
        for line in self.lines:
            if not line.kind.is_credit:
                total = total + line.amount
        return total

    @property
    def net(self) -> Money:
        return self.gross + self.deductions

    def lines_of(self, kind: ChargeKind) -> tuple[StatementLine, ...]:
        return tuple(line for line in self.lines if line.kind is kind)

    def total_of(self, kind: ChargeKind) -> Money:
        total = Money.zero()
        for line in self.lines_of(kind):
            total = total + line.amount
        return total

    @property
    def average_price(self) -> Price | None:
        """Net over bushels, which is the number a grower actually compares."""
        if self.quantity.is_zero():
            return None
        # cents over hundredths-of-a-bushel is already dollars per bushel, and a
        # tick is a ten-thousandth of a dollar.
        exact = Decimal(self.net.cents) * 10_000 / Decimal(self.quantity.hundredths)
        return Price(int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    def describe(self) -> dict[str, object]:
        average = self.average_price
        return {
            "number": self.number,
            "account": self.account,
            "commodity": self.commodity_code,
            "on": self.on.isoformat(),
            "quantity": str(self.quantity),
            "gross": str(self.gross),
            "deductions": str(self.deductions),
            "net": str(self.net),
            "average_price": None if average is None else str(average),
            "lines": [line.describe() for line in self.lines],
        }


class SettlementBuilder:
    """Assembles a statement in the fixed order, so nothing lands out of place."""

    def __init__(
        self, number: str, account: str, commodity_code: str, on: date, quantity: Bushels
    ) -> None:
        if not quantity.is_positive():
            raise ValidationError("a settlement needs bushels", number=number)
        self._number = number
        self._account = account
        self._commodity_code = commodity_code
        self._on = on
        self._quantity = quantity
        self._lines: list[StatementLine] = []

    def grain(self, price: Price, description: str = "grain") -> SettlementBuilder:
        self._lines.append(
            StatementLine(
                kind=ChargeKind.GRAIN,
                description=description,
                amount=price.extend(self._quantity),
                quantity=self._quantity,
                rate=price,
            )
        )
        return self

    def charge(
        self,
        kind: ChargeKind,
        description: str,
        amount: Money,
        *,
        quantity: Bushels | None = None,
        rate: Price | None = None,
    ) -> SettlementBuilder:
        if kind.is_credit:
            raise ValidationError("use grain() for a credit line", description=description)
        signed = amount if amount.cents <= 0 else -amount
        self._lines.append(
            StatementLine(
                kind=kind, description=description, amount=signed, quantity=quantity, rate=rate
            )
        )
        return self

    def discount(self, description: str, rate: Price) -> SettlementBuilder:
        return self.charge(
            ChargeKind.DISCOUNT,
            description,
            rate.extend(self._quantity),
            quantity=self._quantity,
            rate=rate,
        )

    def drying(self, points: Percent, rate_per_point: Price) -> SettlementBuilder:
        if points.is_zero():
            return self
        per_bushel = Price(int(Decimal(rate_per_point.ticks) * points.points))
        return self.charge(
            ChargeKind.DRYING,
            f"drying {points} at {rate_per_point} a point",
            per_bushel.extend(self._quantity),
            quantity=self._quantity,
            rate=per_bushel,
        )

    def checkoff(self, checkoff: Checkoff) -> SettlementBuilder:
        value = Money.zero()
        for line in self._lines:
            value = value + line.amount
        return self.charge(
            ChargeKind.CHECKOFF,
            checkoff.name,
            checkoff.assess(self._quantity, value),
            quantity=self._quantity,
            rate=checkoff.per_bushel,
        )

    def build(self) -> Settlement:
        ordered = sorted(
            self._lines, key=lambda line: (_ORDER[line.kind], line.description)
        )
        return Settlement(
            number=self._number,
            account=self._account,
            commodity_code=self._commodity_code,
            on=self._on,
            quantity=self._quantity,
            lines=tuple(ordered),
        )
