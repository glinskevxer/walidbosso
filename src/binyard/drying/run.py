"""Dryer runs, and the difference between the shrink you charged and the
shrink you got.

A load is charged shrink at the scale, computed from a moisture reading and a
formula. The dryer then removes actual water, and the two figures are never the
same. Over a season the gap is the single biggest unexplained number in a
country elevator's inventory, and the only way to explain it is to record both
and subtract.

So a run carries what went in, what came out, and what the formula said should
have come out. Three numbers, and the third is the one that turns a
disagreement with a grower into an arithmetic question.

Fuel is here too, for the same reason. Propane per point per bushel is what
decides whether it is cheaper to dry a load or discount it, and a house that
does not measure it is guessing at its own drying charge.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.money import Money, Price
from binyard.percent import Percent
from binyard.shrink import ShrinkPolicy, apply_shrink

__all__ = ["DryerRun", "DryingVariance", "FuelUse", "RunStatus"]


class RunStatus(StrEnum):
    """Where a run is."""

    #: Grain drawn, dryer not yet lit.
    LOADED = "loaded"
    #: Running. The wet bushels are out of their bin and not yet in another.
    DRYING = "drying"
    #: Finished and weighed out.
    FINISHED = "finished"
    #: Stopped without finishing. What came out is whatever came out.
    ABANDONED = "abandoned"

    @property
    def is_final(self) -> bool:
        return self in {RunStatus.FINISHED, RunStatus.ABANDONED}


@dataclass(frozen=True, slots=True)
class FuelUse:
    """Propane burned, and what it cost."""

    gallons: Decimal
    price_per_gallon: Price

    def __post_init__(self) -> None:
        if self.gallons < 0:
            raise ValidationError("fuel used cannot be negative", gallons=str(self.gallons))
        if self.price_per_gallon.ticks < 0:
            raise ValidationError("a fuel price cannot be negative")

    @property
    def cost(self) -> Money:
        exact = Decimal(self.price_per_gallon.ticks) * self.gallons / Decimal(100)
        return Money(int(exact.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


@dataclass(frozen=True, slots=True)
class DryingVariance:
    """What the formula said against what the dryer did."""

    expected_out: Bushels
    actual_out: Bushels

    @property
    def difference(self) -> Bushels:
        """Positive means the dryer gave back less than the formula allowed."""
        return self.expected_out - self.actual_out

    @property
    def favours_the_house(self) -> bool:
        return self.difference.hundredths < 0

    def rate(self) -> Decimal:
        if self.expected_out.is_zero():
            return Decimal(0)
        return Decimal(self.difference.hundredths) / Decimal(self.expected_out.hundredths) * 100

    def describe(self) -> dict[str, str]:
        return {
            "expected_out": str(self.expected_out),
            "actual_out": str(self.actual_out),
            "difference": str(self.difference),
            "rate": f"{self.rate():.4f}",
        }


@dataclass(frozen=True, slots=True)
class DryerRun:
    """One batch through the dryer."""

    identifier: str
    commodity_code: str
    source_bin: str
    destination_bin: str
    wet_bushels: Bushels
    wet_moisture: Percent
    started_at: datetime
    status: RunStatus = RunStatus.LOADED
    dry_moisture: Percent | None = None
    dry_bushels: Bushels | None = None
    finished_at: datetime | None = None
    fuel: FuelUse | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("a dryer run needs an identifier")
        if not self.wet_bushels.is_positive():
            raise ValidationError("a dryer run needs bushels", run=self.identifier)
        if self.source_bin == self.destination_bin:
            raise ValidationError("a run has to come out somewhere else", run=self.identifier)
        if self.dry_moisture is not None and self.dry_moisture > self.wet_moisture:
            raise ValidationError(
                "grain does not come out of a dryer wetter than it went in",
                run=self.identifier,
                wet=str(self.wet_moisture),
                dry=str(self.dry_moisture),
            )
        if self.dry_bushels is not None and self.dry_bushels > self.wet_bushels:
            raise ValidationError(
                "more came out of the dryer than went in",
                run=self.identifier,
                went_in=str(self.wet_bushels),
                came_out=str(self.dry_bushels),
            )
        if self.finished_at is not None and self.finished_at < self.started_at:
            raise ValidationError("a run cannot finish before it starts", run=self.identifier)

    @property
    def is_open(self) -> bool:
        return not self.status.is_final

    @property
    def points_removed(self) -> Percent:
        if self.dry_moisture is None:
            return Percent.zero()
        return self.wet_moisture - self.dry_moisture

    @property
    def actual_shrink(self) -> Bushels:
        if self.dry_bushels is None:
            return Bushels.zero()
        return self.wet_bushels - self.dry_bushels

    def expected_out(self, policy: ShrinkPolicy) -> Bushels:
        """What the posted formula says should come back."""
        return apply_shrink(self.wet_bushels, self.wet_moisture, policy).net

    def variance(self, policy: ShrinkPolicy) -> DryingVariance | None:
        if self.dry_bushels is None:
            return None
        return DryingVariance(
            expected_out=self.expected_out(policy), actual_out=self.dry_bushels
        )

    def fuel_per_point_bushel(self) -> Decimal | None:
        """Gallons per point per bushel, the number that decides drying or not."""
        if self.fuel is None or self.points_removed.is_zero():
            return None
        divisor = Decimal(self.wet_bushels.hundredths) / 100 * self.points_removed.points
        if divisor <= 0:
            return None
        return (self.fuel.gallons / divisor).quantize(Decimal("0.000001"))

    # -- transitions -----------------------------------------------------

    def lit(self, at: datetime) -> DryerRun:
        if self.status is not RunStatus.LOADED:
            raise ValidationError(
                "only a loaded run can be lit", run=self.identifier, status=self.status.value
            )
        return replace(self, status=RunStatus.DRYING, started_at=at)

    def finished(
        self,
        *,
        dry_bushels: Bushels,
        dry_moisture: Percent,
        at: datetime,
        fuel: FuelUse | None = None,
    ) -> DryerRun:
        if self.status is not RunStatus.DRYING:
            raise ValidationError(
                "only a running dryer can finish", run=self.identifier, status=self.status.value
            )
        return replace(
            self,
            status=RunStatus.FINISHED,
            dry_bushels=dry_bushels,
            dry_moisture=dry_moisture,
            finished_at=at,
            fuel=fuel,
        )

    def abandoned(
        self, reason: str, *, dry_bushels: Bushels, dry_moisture: Percent, at: datetime
    ) -> DryerRun:
        if not self.is_open:
            raise ValidationError("that run is already finished", run=self.identifier)
        if not reason.strip():
            raise ValidationError("abandoning a run needs a reason", run=self.identifier)
        return replace(
            self,
            status=RunStatus.ABANDONED,
            dry_bushels=dry_bushels,
            dry_moisture=dry_moisture,
            finished_at=at,
            note=reason.strip(),
        )

    def describe(self, policy: ShrinkPolicy | None = None) -> dict[str, object]:
        variance = self.variance(policy) if policy is not None else None
        fuel_rate = self.fuel_per_point_bushel()
        return {
            "identifier": self.identifier,
            "commodity": self.commodity_code,
            "from": self.source_bin,
            "to": self.destination_bin,
            "status": self.status.value,
            "wet_bushels": str(self.wet_bushels),
            "wet_moisture": str(self.wet_moisture),
            "dry_bushels": None if self.dry_bushels is None else str(self.dry_bushels),
            "dry_moisture": None if self.dry_moisture is None else str(self.dry_moisture),
            "points_removed": str(self.points_removed),
            "actual_shrink": str(self.actual_shrink),
            "fuel_gallons": None if self.fuel is None else str(self.fuel.gallons),
            "fuel_cost": None if self.fuel is None else str(self.fuel.cost),
            "gallons_per_point_bushel": None if fuel_rate is None else str(fuel_rate),
            "variance": None if variance is None else variance.describe(),
            "note": self.note,
        }
