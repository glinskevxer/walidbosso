"""Rail: placement, free time and the money the clock costs.

A railroad spots cars at the elevator and starts a clock. The house has a fixed
number of free hours to load them and release them back, and after that it pays
demurrage per car per day. On a shuttle train that is a hundred and ten cars and
the clock runs on all of them at once, so an hour of hesitation on a Sunday is
a real number.

The rules that make this awkward and are handled here:

**Free time is in calendar days, counted from the day after placement.** A car
spotted at four in the afternoon gets the same free time as one spotted at six
in the morning, because the railroad counts days and not hours.

**Weekends and holidays may or may not count.** Which one is a tariff term, so
it is on the terms rather than assumed, and the day counter takes a set of
non-computing days.

**Credits offset debits within a month.** A car released early earns a credit
day, and credits net against debits across the whole month before anything is
billed. That netting is why demurrage cannot be computed one car at a time and
why this module works on a placement of many cars at once.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from binyard.errors import LoadoutError, ValidationError
from binyard.money import Money, Price

__all__ = [
    "CarStatus",
    "DemurrageBill",
    "DemurrageTerms",
    "Placement",
    "RailCar",
    "bill_demurrage",
]


class CarStatus(StrEnum):
    """Where one car is in its visit."""

    PLACED = "placed"
    LOADING = "loading"
    RELEASED = "released"
    #: Refused and sent back without loading. It still ran a clock.
    REJECTED = "rejected"

    @property
    def is_final(self) -> bool:
        return self in {CarStatus.RELEASED, CarStatus.REJECTED}


@dataclass(frozen=True, slots=True)
class DemurrageTerms:
    """One tariff."""

    #: Days after placement before the meter starts.
    free_days: int
    #: Charged per car per day past the free period.
    per_car_day: Price
    #: Days a car released early earns back, capped at this many per car.
    credit_days_per_car: int = 1
    #: Days that do not count against free time under this tariff.
    non_computing_weekdays: frozenset[int] = frozenset()
    #: Named holidays that do not count.
    holidays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        if self.free_days < 0:
            raise ValidationError("free days cannot be negative")
        if self.per_car_day.ticks < 0:
            raise ValidationError("a demurrage rate cannot be negative")
        if self.credit_days_per_car < 0:
            raise ValidationError("credit days cannot be negative")
        if any(day < 0 or day > 6 for day in self.non_computing_weekdays):
            raise ValidationError("a weekday is 0 through 6")

    def counts(self, day: date) -> bool:
        if day in self.holidays:
            return False
        return day.weekday() not in self.non_computing_weekdays

    def computing_days(self, first: date, last: date) -> int:
        """Days between two dates that the tariff actually counts, inclusive."""
        if last < first:
            return 0
        total = 0
        day = first
        while day <= last:
            if self.counts(day):
                total += 1
            day += timedelta(days=1)
        return total


@dataclass(frozen=True, slots=True)
class RailCar:
    """One car on one placement."""

    reporting_mark: str
    placed_at: datetime
    status: CarStatus = CarStatus.PLACED
    released_at: datetime | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.reporting_mark.strip():
            raise ValidationError("a car needs a reporting mark")
        if self.released_at is not None and self.released_at < self.placed_at:
            raise ValidationError(
                "a car cannot be released before it was placed", car=self.reporting_mark
            )
        if self.status.is_final and self.released_at is None:
            raise ValidationError(
                "a finished car needs a release time", car=self.reporting_mark
            )

    @property
    def placed_on(self) -> date:
        return self.placed_at.date()

    @property
    def released_on(self) -> date | None:
        return None if self.released_at is None else self.released_at.date()

    def loading(self) -> RailCar:
        if self.status is not CarStatus.PLACED:
            raise LoadoutError("that car is not waiting", car=self.reporting_mark)
        return replace(self, status=CarStatus.LOADING)

    def released(self, at: datetime) -> RailCar:
        if self.status.is_final:
            raise LoadoutError("that car has already gone", car=self.reporting_mark)
        return replace(self, status=CarStatus.RELEASED, released_at=at)

    def rejected(self, reason: str, at: datetime) -> RailCar:
        if self.status.is_final:
            raise LoadoutError("that car has already gone", car=self.reporting_mark)
        if not reason.strip():
            raise ValidationError("rejecting a car needs a reason", car=self.reporting_mark)
        return replace(self, status=CarStatus.REJECTED, released_at=at, note=reason.strip())

    def chargeable_days(self, terms: DemurrageTerms, through: date) -> int:
        """Computing days past the free period, positive only."""
        start = self.placed_on + timedelta(days=1)
        finish = self.released_on or through
        used = terms.computing_days(start, finish)
        return max(used - terms.free_days, 0)

    def credit_days(self, terms: DemurrageTerms) -> int:
        """Days earned back by releasing inside the free period."""
        if self.released_on is None:
            return 0
        start = self.placed_on + timedelta(days=1)
        used = terms.computing_days(start, self.released_on)
        return min(max(terms.free_days - used, 0), terms.credit_days_per_car)


@dataclass(frozen=True, slots=True)
class Placement:
    """A set of cars spotted together, which is how the clock is actually run."""

    identifier: str
    order_identifier: str
    cars: tuple[RailCar, ...]

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("a placement needs an identifier")
        if not self.cars:
            raise ValidationError("a placement with no cars is not a placement")
        marks = [car.reporting_mark for car in self.cars]
        if len(set(marks)) != len(marks):
            raise ValidationError("a car appears twice on this placement", cars=marks)

    @property
    def size(self) -> int:
        return len(self.cars)

    @property
    def outstanding(self) -> tuple[RailCar, ...]:
        return tuple(car for car in self.cars if not car.status.is_final)

    def car(self, reporting_mark: str) -> RailCar:
        for car in self.cars:
            if car.reporting_mark == reporting_mark:
                return car
        raise LoadoutError(
            "no such car on this placement",
            placement=self.identifier,
            car=reporting_mark,
        )

    def with_car(self, car: RailCar) -> Placement:
        return replace(
            self,
            cars=tuple(
                car if item.reporting_mark == car.reporting_mark else item for item in self.cars
            ),
        )


@dataclass(frozen=True, slots=True)
class DemurrageBill:
    """What a placement owes once credits are netted against debits."""

    placement_identifier: str
    through: date
    debit_days: int
    credit_days: int
    per_car_day: Price

    @property
    def net_days(self) -> int:
        return max(self.debit_days - self.credit_days, 0)

    @property
    def amount(self) -> Money:
        exact = Decimal(self.per_car_day.ticks) * self.net_days / Decimal(100)
        return Money(int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    @property
    def is_clean(self) -> bool:
        return self.net_days == 0

    def describe(self) -> dict[str, object]:
        return {
            "placement": self.placement_identifier,
            "through": self.through.isoformat(),
            "debit_days": self.debit_days,
            "credit_days": self.credit_days,
            "net_days": self.net_days,
            "per_car_day": str(self.per_car_day),
            "amount": str(self.amount),
        }


def bill_demurrage(placement: Placement, terms: DemurrageTerms, through: date) -> DemurrageBill:
    """Net every car's debits and credits and price what is left."""
    debits = sum(car.chargeable_days(terms, through) for car in placement.cars)
    credits = sum(car.credit_days(terms) for car in placement.cars)
    return DemurrageBill(
        placement_identifier=placement.identifier,
        through=through,
        debit_days=debits,
        credit_days=credits,
        per_car_day=terms.per_car_day,
    )
