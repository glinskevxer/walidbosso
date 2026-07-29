"""What is being loaded, and how much it is legally allowed to carry.

A truck's capacity is not one number. It is the smallest of three:

* the **gross vehicle weight** the axle configuration is rated and permitted
  for,
* the **volume** of the trailer, which binds on light grain like oats long
  before weight does,
* and whatever the receiving elevator will take, since a terminal with a short
  pit refuses long trailers whatever the plates say.

Rail is different in kind, not degree. A covered hopper is loaded to a marked
capacity, and a car loaded over it is not a fine, it is a car the railroad will
set out and the shipper will pay to have transloaded. So the rail limit is hard
and the truck limit is the minimum of a set.

Overloading is expensive and undetectable from the office, so the check runs
before the draw list is built rather than after the truck is under the spout.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binyard.bushels import Bushels, from_weight, to_weight
from binyard.commodity import CropStandard
from binyard.errors import LoadoutError, ValidationError
from binyard.units import Weight, pounds

__all__ = [
    "AxleConfiguration",
    "CapacityCheck",
    "Carrier",
    "CarrierKind",
    "capacity_bushels",
    "capacity_for",
    "fits",
]


class CarrierKind(StrEnum):
    """What is being loaded, which decides how its limit is worked out."""

    TRUCK = "truck"
    RAIL_CAR = "rail_car"
    BARGE = "barge"

    @property
    def is_rail(self) -> bool:
        return self is CarrierKind.RAIL_CAR


class AxleConfiguration(StrEnum):
    """The common farm and commercial configurations, with their federal limits."""

    #: Straight truck, two axles.
    SINGLE_UNIT_2 = "single_unit_2"
    #: Straight truck with a tag axle.
    SINGLE_UNIT_3 = "single_unit_3"
    #: Tractor and semitrailer, five axles. The workhorse.
    TRACTOR_SEMI_5 = "tractor_semi_5"
    #: Tractor, semitrailer and pup.
    TRACTOR_SEMI_PUP_7 = "tractor_semi_pup_7"

    @property
    def federal_gross_limit(self) -> Weight:
        return {
            AxleConfiguration.SINGLE_UNIT_2: pounds("40000"),
            AxleConfiguration.SINGLE_UNIT_3: pounds("54000"),
            AxleConfiguration.TRACTOR_SEMI_5: pounds("80000"),
            AxleConfiguration.TRACTOR_SEMI_PUP_7: pounds("105500"),
        }[self]


@dataclass(frozen=True, slots=True)
class Carrier:
    """One vehicle or car, with everything that bounds what it can take."""

    identifier: str
    kind: CarrierKind
    empty_weight: Weight
    axles: AxleConfiguration | None = None
    #: Overrides the federal limit when a state permit raises or lowers it.
    permitted_gross: Weight | None = None
    #: Cubic feet of usable space. ``None`` means volume never binds.
    volume_cubic_feet: Decimal | None = None
    #: Rail only: the stencilled load limit on the car.
    marked_capacity: Weight | None = None

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("a carrier needs an identifier")
        if not self.empty_weight.is_positive():
            raise ValidationError("a carrier has an empty weight", carrier=self.identifier)
        if self.kind is CarrierKind.TRUCK and self.axles is None:
            raise ValidationError(
                "a truck needs an axle configuration", carrier=self.identifier
            )
        if self.kind.is_rail and self.marked_capacity is None:
            raise ValidationError(
                "a rail car needs its marked capacity", carrier=self.identifier
            )
        if self.volume_cubic_feet is not None and self.volume_cubic_feet <= 0:
            raise ValidationError("a volume has to be positive", carrier=self.identifier)

    @property
    def gross_limit(self) -> Weight:
        if self.kind.is_rail:
            assert self.marked_capacity is not None
            return self.empty_weight + self.marked_capacity
        if self.permitted_gross is not None:
            return self.permitted_gross
        assert self.axles is not None
        return self.axles.federal_gross_limit

    def payload_limit(self) -> Weight:
        """The most grain this carrier may hold, by weight alone."""
        payload = self.gross_limit - self.empty_weight
        if not payload.is_positive():
            raise LoadoutError(
                "this carrier is over its own limit empty", carrier=self.identifier
            )
        return payload

    def volume_limit(self, standard: CropStandard) -> Weight | None:
        """The most grain that fits, or ``None`` when volume does not bind.

        Test weight is pounds per Winchester bushel and a bushel is 1.2445
        cubic feet, so the density in pounds per cubic foot is the crop's
        bushel weight over that.
        """
        if self.volume_cubic_feet is None:
            return None
        density = standard.pounds_per_bushel / Decimal("1.2445")
        return Weight.from_pounds((self.volume_cubic_feet * density).quantize(Decimal("0.01")))


@dataclass(frozen=True, slots=True)
class CapacityCheck:
    """The binding limit for one carrier and one crop, and why it binds."""

    carrier_identifier: str
    commodity_code: str
    by_weight: Weight
    by_volume: Weight | None
    limit: Weight
    binding: str

    def describe(self) -> dict[str, str | None]:
        return {
            "carrier": self.carrier_identifier,
            "commodity": self.commodity_code,
            "by_weight": str(self.by_weight),
            "by_volume": None if self.by_volume is None else str(self.by_volume),
            "limit": str(self.limit),
            "binding": self.binding,
        }


def capacity_for(carrier: Carrier, standard: CropStandard) -> CapacityCheck:
    """Work out what actually limits this carrier on this crop."""
    by_weight = carrier.payload_limit()
    by_volume = carrier.volume_limit(standard)
    if by_volume is not None and by_volume < by_weight:
        return CapacityCheck(
            carrier_identifier=carrier.identifier,
            commodity_code=standard.code,
            by_weight=by_weight,
            by_volume=by_volume,
            limit=by_volume,
            binding="volume",
        )
    return CapacityCheck(
        carrier_identifier=carrier.identifier,
        commodity_code=standard.code,
        by_weight=by_weight,
        by_volume=by_volume,
        limit=by_weight,
        binding="weight",
    )


def capacity_bushels(carrier: Carrier, standard: CropStandard) -> Bushels:
    """The binding limit, expressed the way a load-out order is written."""
    return from_weight(capacity_for(carrier, standard).limit, standard)


def fits(carrier: Carrier, standard: CropStandard, quantity: Bushels) -> bool:
    """Whether this quantity is inside what the carrier may legally hold."""
    return to_weight(quantity, standard) <= capacity_for(carrier, standard).limit
