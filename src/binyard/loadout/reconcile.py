"""Proving the load that arrived is the load that left.

Every shipment is weighed twice, once here and once at the destination, and the
two numbers never agree exactly. Some of the gap is real: grain settles, dust
blows out of a hopper gate, and a car sitting three days in July gives up
moisture. Some of it is two scales that were last certified in different years.

The house allows a tolerance, usually a fraction of a percent, and inside it
the difference is absorbed and posted to shrink. Outside it, somebody has to
look, because a consistent one way variance on one destination is a scale
problem and a consistent variance on one loader is a different problem.

Grade differences are separate and are worse. A car that left as No. 2 and
arrived as No. 3 costs the discount on the whole car, and unlike weight there
is no tolerance band: either the destination's grade certificate stands or it
is protested and reinspected. This module computes the money and flags the
protest window; it does not decide whether to protest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import LoadoutError, ValidationError
from binyard.money import Money, Price
from binyard.percent import Percent

__all__ = [
    "Variance",
    "ArrivalReport",
    "Reconciliation",
    "PROTEST_WINDOW",
    "reconcile",
]

#: How long after arrival a grade certificate can still be protested.
PROTEST_WINDOW = timedelta(days=5)


class Variance(StrEnum):
    """What the difference between the two weights means."""

    #: Inside the allowance. Posted to shrink and forgotten.
    WITHIN_TOLERANCE = "within_tolerance"
    #: Arrived light by more than the allowance.
    SHORT = "short"
    #: Arrived heavy, which is rarer and usually a scale.
    OVER = "over"


@dataclass(frozen=True, slots=True)
class ArrivalReport:
    """What the destination said when the load got there."""

    order_identifier: str
    load_sequence: int
    arrived_on: date
    quantity: Bushels
    grade: int | None = None
    certificate: str | None = None

    def __post_init__(self) -> None:
        if self.quantity.hundredths < 0:
            raise ValidationError("an arrival cannot be negative")
        if self.grade is not None and self.grade < 0:
            raise ValidationError("a grade is not negative", grade=self.grade)

    def protest_closes_on(self) -> date:
        return self.arrived_on + PROTEST_WINDOW


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The two weights, the difference, and what it costs."""

    order_identifier: str
    load_sequence: int
    shipped: Bushels
    arrived: Bushels
    allowance: Percent
    variance: Variance
    difference: Bushels
    allowed: Bushels
    excess: Bushels
    grade_shipped: int | None = None
    grade_arrived: int | None = None
    grade_charge: Money = Money.zero()
    protest_closes_on: date | None = None

    @property
    def is_clean(self) -> bool:
        return self.variance is Variance.WITHIN_TOLERANCE and not self.grade_slipped

    @property
    def grade_slipped(self) -> bool:
        if self.grade_shipped is None or self.grade_arrived is None:
            return False
        if self.grade_arrived == 0:
            return self.grade_shipped != 0
        return self.grade_arrived > self.grade_shipped

    @property
    def loss_rate(self) -> Decimal:
        if self.shipped.is_zero():
            return Decimal(0)
        return Decimal(self.difference.hundredths) / Decimal(self.shipped.hundredths) * 100

    def describe(self) -> dict[str, object]:
        return {
            "order": self.order_identifier,
            "load": self.load_sequence,
            "shipped": str(self.shipped),
            "arrived": str(self.arrived),
            "difference": str(self.difference),
            "allowed": str(self.allowed),
            "excess": str(self.excess),
            "loss_rate": f"{self.loss_rate:.4f}",
            "variance": self.variance.value,
            "grade_shipped": self.grade_shipped,
            "grade_arrived": self.grade_arrived,
            "grade_slipped": self.grade_slipped,
            "grade_charge": str(self.grade_charge),
            "clean": self.is_clean,
            "protest_closes_on": (
                None if self.protest_closes_on is None else self.protest_closes_on.isoformat()
            ),
        }


def reconcile(
    *,
    order_identifier: str,
    load_sequence: int,
    shipped: Bushels,
    report: ArrivalReport,
    allowance: Percent,
    grade_shipped: int | None = None,
    grade_discount: Price = Price.zero(),
) -> Reconciliation:
    """Compare one shipped load against what the destination reported."""
    if not shipped.is_positive():
        raise ValidationError("nothing was shipped on that load", order=order_identifier)
    if report.order_identifier != order_identifier or report.load_sequence != load_sequence:
        raise LoadoutError(
            "that arrival report belongs to a different load",
            order=order_identifier,
            load=load_sequence,
            reported=report.order_identifier,
        )
    if allowance.hundredths < 0:
        raise ValidationError("an allowance cannot be negative")

    difference = shipped - report.quantity
    allowed = shipped.scaled_by(allowance.fraction)
    magnitude = abs(difference)
    excess = magnitude - allowed
    if not excess.is_positive():
        variance = Variance.WITHIN_TOLERANCE
        excess = Bushels.zero()
    elif difference.is_positive():
        variance = Variance.SHORT
    else:
        variance = Variance.OVER

    charge = Money.zero()
    slipped = (
        grade_shipped is not None
        and report.grade is not None
        and (report.grade > grade_shipped or (report.grade == 0 and grade_shipped != 0))
    )
    if slipped:
        charge = grade_discount.extend(report.quantity)

    return Reconciliation(
        order_identifier=order_identifier,
        load_sequence=load_sequence,
        shipped=shipped,
        arrived=report.quantity,
        allowance=allowance,
        variance=variance,
        difference=difference,
        allowed=allowed,
        excess=excess,
        grade_shipped=grade_shipped,
        grade_arrived=report.grade,
        grade_charge=charge,
        protest_closes_on=report.protest_closes_on() if slipped else None,
    )
