"""Storage charges, and the three ways houses count them.

Storage is posted per bushel per month, and that sentence hides an argument
about what a month is. Three conventions are in use and they give different
money on the same grain:

**Per day** prorates the monthly rate over the days actually stored. It is the
fairest and the least common.

**Per month started** charges a whole month the moment the period rolls, so
grain in for thirty two days pays two months. This is what most country
elevators post.

**Per month prorated** charges whole months and then prorates the tail.

None is more correct than the others, so the convention travels on the terms
and the segments show which one produced the money. That matters because the
charge is not computed on one balance: a grower who takes out half in December
pays the full rate up to that day and half the rate afterwards, so the accrual
walks the balance through every release and charges each stretch separately.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.money import Money, Price
from binyard.obligations.obligation import StorageObligation

__all__ = ["Accrual", "StorageAssessment", "StorageSegment", "StorageTerms", "accrue"]

_DAYS_PER_MONTH = Decimal("30.4375")


class Accrual(StrEnum):
    """How a part month is counted."""

    PER_DAY = "per_day"
    PER_MONTH_STARTED = "per_month_started"
    PER_MONTH_PRORATED = "per_month_prorated"


@dataclass(frozen=True, slots=True)
class StorageTerms:
    """What the house posts for storage on one crop."""

    rate_per_bushel_month: Price
    accrual: Accrual = Accrual.PER_MONTH_STARTED
    free_days: int = 0
    minimum_charge: Money = Money.zero()

    def __post_init__(self) -> None:
        if self.rate_per_bushel_month.ticks < 0:
            raise ValidationError("a storage rate cannot be negative")
        if self.free_days < 0:
            raise ValidationError("free days cannot be negative")
        if self.minimum_charge.cents < 0:
            raise ValidationError("a minimum charge cannot be negative")

    def months_for(self, days: int) -> Decimal:
        """How many rate-months ``days`` of storage counts as."""
        if days <= 0:
            return Decimal(0)
        span = Decimal(days)
        if self.accrual is Accrual.PER_DAY:
            return span / _DAYS_PER_MONTH
        whole = (span / _DAYS_PER_MONTH).to_integral_value(rounding="ROUND_FLOOR")
        remainder = span - whole * _DAYS_PER_MONTH
        if remainder <= 0:
            return whole
        if self.accrual is Accrual.PER_MONTH_STARTED:
            return whole + 1
        return whole + remainder / _DAYS_PER_MONTH


@dataclass(frozen=True, slots=True)
class StorageSegment:
    """One stretch of days at one balance."""

    starts_on: date
    ends_on: date
    balance: Bushels
    charge: Money

    @property
    def days(self) -> int:
        return (self.ends_on - self.starts_on).days

    def describe(self) -> dict[str, str | int]:
        return {
            "starts_on": self.starts_on.isoformat(),
            "ends_on": self.ends_on.isoformat(),
            "days": self.days,
            "balance": str(self.balance),
            "charge": str(self.charge),
        }


@dataclass(frozen=True, slots=True)
class StorageAssessment:
    """Every stretch, and what the whole thing costs."""

    obligation_identifier: str
    through: date
    segments: tuple[StorageSegment, ...]
    minimum_applied: bool = False
    total: Money = Money.zero()

    @property
    def days(self) -> int:
        return sum(segment.days for segment in self.segments)

    def describe(self) -> dict[str, object]:
        return {
            "obligation": self.obligation_identifier,
            "through": self.through.isoformat(),
            "days": self.days,
            "minimum_applied": self.minimum_applied,
            "total": str(self.total),
            "segments": [segment.describe() for segment in self.segments],
        }


def accrue(
    obligation: StorageObligation, terms: StorageTerms, through: date
) -> StorageAssessment:
    """Charge storage on one obligation up to and including ``through``."""
    if through < obligation.opened_on:
        raise ValidationError(
            "cannot accrue storage before the obligation opened",
            obligation=obligation.identifier,
            through=through.isoformat(),
        )
    if not obligation.kind.accrues_storage:
        return StorageAssessment(
            obligation_identifier=obligation.identifier, through=through, segments=()
        )

    free_days = terms.free_days if obligation.free_days == 0 else obligation.free_days
    start = obligation.opened_on + timedelta(days=free_days)
    if through <= start:
        return StorageAssessment(
            obligation_identifier=obligation.identifier, through=through, segments=()
        )

    rate = (
        terms.rate_per_bushel_month
        if obligation.storage_rate == Price.zero()
        else obligation.storage_rate
    )

    boundaries = sorted(
        {start, through} | {r.on for r in obligation.releases if start < r.on < through}
    )
    segments: list[StorageSegment] = []
    total = Money.zero()
    balance = obligation.quantity

    for index, edge in enumerate(boundaries[:-1]):
        nxt = boundaries[index + 1]
        held = balance - _released_by(obligation, edge)
        if not held.is_positive():
            continue
        months = terms.months_for((nxt - edge).days)
        charge = _charge(rate, held, months)
        segments.append(
            StorageSegment(starts_on=edge, ends_on=nxt, balance=held, charge=charge)
        )
        total = total + charge

    minimum_applied = False
    if segments and total < terms.minimum_charge:
        total = terms.minimum_charge
        minimum_applied = True

    return StorageAssessment(
        obligation_identifier=obligation.identifier,
        through=through,
        segments=tuple(segments),
        minimum_applied=minimum_applied,
        total=total,
    )


def _released_by(obligation: StorageObligation, on: date) -> Bushels:
    total = Bushels.zero()
    for release in obligation.releases:
        if release.on <= on:
            total = total + release.quantity
    return total


def _charge(rate: Price, balance: Bushels, months: Decimal) -> Money:
    # ticks are ten-thousandths of a dollar and hundredths are hundredths of a
    # bushel, so the product is in hundred-millionths of a dollar; cents need a
    # further hundred, which leaves ten thousand.
    exact = Decimal(rate.ticks) * Decimal(balance.hundredths) * months / Decimal(10_000)
    return Money(int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
