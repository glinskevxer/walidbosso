"""What the board outside the scale house says a defect costs.

A schedule is bands. Corn at 15.1 to 16.0 moisture costs three cents a bushel
a point, 16.1 to 18.0 costs five, and over 20.0 the house stops buying. The
bands are what a farmer reads; what the accounting needs is a rate for one
reading, and there are two conventions in the trade for getting there.

**Stepped**: the band the reading lands in decides the whole charge. Twenty
percent corn is charged at the 18.1-to-20.0 rate for all five points over
target.

**Cumulative**: each band charges for its own span and the reading only pays
the top band for the part of the reading inside it. Twenty percent corn pays
the 15.1-to-16.0 rate for one point, the 16.1-to-18.0 rate for two, and the
18.1-to-20.0 rate for two.

The two differ by real money on a wet load, so the convention is on the
schedule and never assumed. Bands are validated to be ascending, contiguous
and non overlapping at construction, because a gap between bands is a reading
that costs nothing and nobody notices until a grower finds it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from binyard.errors import ConfigurationError, NoScheduleInEffect, ScheduleError
from binyard.grading.factors import Direction, FactorName, direction_of
from binyard.money import Price

__all__ = ["Basis", "Band", "FactorSchedule", "GradeSchedule", "PostedSchedule", "ScheduleBook"]


class Basis(StrEnum):
    """How the band's rate turns into cents per bushel."""

    #: The rate is the whole charge for any reading inside the band.
    FLAT = "flat"
    #: The rate is charged for each whole point of reading past the reference.
    PER_POINT = "per_point"
    #: The rate is charged for each tenth of a point past the reference.
    PER_TENTH = "per_tenth"
    #: Nothing is charged, and the load is refused outright.
    REJECT = "reject"


class Accumulation(StrEnum):
    """Whether bands below the reading also charge."""

    STEPPED = "stepped"
    CUMULATIVE = "cumulative"


@dataclass(frozen=True, slots=True)
class Band:
    """One row of the posted board."""

    lower: Decimal
    upper: Decimal | None
    rate: Price
    basis: Basis = Basis.PER_POINT

    def __post_init__(self) -> None:
        if self.upper is not None and self.upper <= self.lower:
            raise ConfigurationError(
                "a band's upper bound must sit above its lower bound",
                lower=str(self.lower),
                upper=str(self.upper),
            )
        if self.basis is Basis.REJECT and self.rate != Price.zero():
            raise ConfigurationError("a rejecting band cannot also carry a rate")

    @property
    def is_open_ended(self) -> bool:
        return self.upper is None

    def contains(self, reading: Decimal) -> bool:
        if reading < self.lower:
            return False
        return self.upper is None or reading < self.upper

    def charge_for(self, reading: Decimal, span_from: Decimal) -> Price:
        """Cents per bushel this band charges for ``reading``, counted from ``span_from``."""
        if self.basis is Basis.REJECT:
            return Price.zero()
        if self.basis is Basis.FLAT:
            return self.rate
        top = reading if self.upper is None else min(reading, self.upper)
        span = top - span_from
        if span <= 0:
            return Price.zero()
        units = span if self.basis is Basis.PER_POINT else span * 10
        return Price(int(Decimal(self.rate.ticks) * units))


@dataclass(frozen=True, slots=True)
class FactorSchedule:
    """Every band posted for one factor."""

    factor: FactorName
    bands: tuple[Band, ...]
    accumulation: Accumulation = Accumulation.STEPPED
    #: Readings at or better than this are free. Defaults to the first band's edge.
    free_below: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.bands:
            raise ConfigurationError(f"{self.factor.value} schedule has no bands")
        if direction_of(self.factor) is Direction.HIGHER_IS_BETTER:
            raise ConfigurationError(
                "a factor where higher is better needs its own schedule shape",
                factor=self.factor.value,
            )
        previous: Band | None = None
        for band in self.bands:
            if previous is not None:
                if previous.upper is None:
                    raise ConfigurationError(
                        "an open ended band has to be last", factor=self.factor.value
                    )
                if band.lower != previous.upper:
                    raise ConfigurationError(
                        "bands must be contiguous with no gap and no overlap",
                        factor=self.factor.value,
                        gap_at=str(band.lower),
                        previous_upper=str(previous.upper),
                    )
            previous = band

    @property
    def threshold(self) -> Decimal:
        return self.bands[0].lower if self.free_below is None else self.free_below

    def band_for(self, reading: Decimal) -> Band | None:
        for band in self.bands:
            if band.contains(reading):
                return band
        return None

    def rejects(self, reading: Decimal) -> bool:
        band = self.band_for(reading)
        return band is not None and band.basis is Basis.REJECT

    def rate_for(self, reading: Decimal) -> Price:
        """Cents per bushel charged for this reading."""
        if reading <= self.threshold:
            return Price.zero()
        if self.accumulation is Accumulation.STEPPED:
            band = self.band_for(reading)
            if band is None:
                raise ScheduleError(
                    "the reading is past the last posted band",
                    factor=self.factor.value,
                    reading=str(reading),
                )
            return band.charge_for(reading, self.threshold)
        total = Price.zero()
        span_from = self.threshold
        for band in self.bands:
            if reading <= band.lower:
                break
            start = max(span_from, band.lower)
            total = total + band.charge_for(reading, start)
            if band.upper is None or reading < band.upper:
                break
            span_from = band.upper
        return total


@dataclass(frozen=True, slots=True)
class GradeSchedule:
    """What each numeric grade costs, sample grade included."""

    rates: dict[int, Price]

    def __post_init__(self) -> None:
        if not self.rates:
            raise ConfigurationError("a grade schedule needs at least one rate")
        graded = sorted(g for g in self.rates if g > 0)
        for better, worse in zip(graded, graded[1:], strict=False):
            if self.rates[worse] < self.rates[better]:
                raise ConfigurationError(
                    "a worse grade cannot be cheaper than a better one",
                    better=better,
                    worse=worse,
                )

    def rate_for(self, grade: int) -> Price:
        if grade in self.rates:
            return self.rates[grade]
        worse_than_listed = max(g for g in self.rates if g > 0)
        if grade > worse_than_listed:
            return self.rates[worse_than_listed]
        return Price.zero()


@dataclass(frozen=True, slots=True)
class PostedSchedule:
    """One commodity's whole board, effective from a date."""

    commodity_code: str
    effective_from: date
    factors: tuple[FactorSchedule, ...]
    grades: GradeSchedule
    effective_to: date | None = None

    def __post_init__(self) -> None:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise ConfigurationError(
                "a schedule cannot expire before it starts",
                commodity=self.commodity_code,
            )
        seen = [f.factor for f in self.factors]
        if len(set(seen)) != len(seen):
            raise ConfigurationError(
                "a factor is posted twice", commodity=self.commodity_code
            )

    def covers(self, day: date) -> bool:
        if day < self.effective_from:
            return False
        return self.effective_to is None or day <= self.effective_to

    def schedule_for(self, factor: FactorName) -> FactorSchedule | None:
        for posted in self.factors:
            if posted.factor is factor:
                return posted
        return None


@dataclass(frozen=True, slots=True)
class ScheduleBook:
    """Every posted schedule the house has, across crops and dates."""

    schedules: tuple[PostedSchedule, ...] = ()

    def in_effect(self, commodity_code: str, day: date) -> PostedSchedule:
        candidates = [
            posted
            for posted in self.schedules
            if posted.commodity_code == commodity_code and posted.covers(day)
        ]
        if not candidates:
            raise NoScheduleInEffect(
                "nothing is posted for that crop on that day",
                commodity=commodity_code,
                day=day.isoformat(),
            )
        return max(candidates, key=lambda posted: posted.effective_from)

    def with_schedule(self, posted: PostedSchedule) -> ScheduleBook:
        return ScheduleBook(schedules=(*self.schedules, posted))
