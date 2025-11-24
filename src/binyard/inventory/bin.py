"""Bins, and what happens to quality when two loads land in the same one.

An empty bin has no quality at all, which is not the same as having zero
moisture, and the difference matters the first time somebody averages an empty
bin into a blend. :class:`BinContents` therefore has no factors until it has
bushels.

**Commingling is a weighted average, not a mean.** Nine thousand bushels of
14% corn and a thousand of 20% make a bin of 14.6% corn, and the bin's test
weight, damage and foreign material all move the same way. Every one of those
is the bushel weighted average of what went in.

Three things do not average, and getting them wrong is how a clean bin gets
declared out of a load nobody would have taken:

* **Infestation** spreads. One infested load infests the bin.
* **Odor** takes the worst of the two, on the same reasoning.
* **Heat damage** averages like any other percentage, but the invariant that
  it cannot exceed total damage has to survive the averaging, and it does,
  because a weighted average of two pairs that each satisfy it satisfies it.

Drawing grain out does not change quality. The bin is one homogeneous mass as
far as the accounting is concerned, which is a fiction, but it is the fiction
the whole trade settles on and a load-out is graded on the way out anyway.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import (
    BinCapacityExceeded,
    CommodityMismatch,
    InsufficientInventory,
    ValidationError,
)
from binyard.grading.factors import FactorSample, Odor
from binyard.percent import Percent

__all__ = ["Bin", "BinContents", "BinKind", "commingle", "commingle_many"]

_ODOR_SEVERITY: dict[Odor, int] = {
    Odor.NONE: 0,
    Odor.MUSTY: 1,
    Odor.SOUR: 2,
    Odor.COMMERCIALLY_OBJECTIONABLE: 3,
}


class BinKind(StrEnum):
    """What the structure is, which decides how it behaves."""

    #: Concrete or steel upright, aerated, drawn from the bottom.
    UPRIGHT = "upright"
    #: Flat storage building, drawn with a loader.
    FLAT = "flat"
    #: Ground pile under a tarp. Loses grain and cannot be trusted to the bushel.
    PILE = "pile"

    @property
    def is_temporary(self) -> bool:
        return self is BinKind.PILE


@dataclass(frozen=True, slots=True)
class Bin:
    """The structure itself. Nothing about it changes during a season."""

    identifier: str
    name: str
    kind: BinKind
    capacity: Bushels
    #: Crops this bin may hold. Empty means anything the house handles.
    allowed_commodities: frozenset[str] = frozenset()
    aerated: bool = True
    #: Extra loss a pile is expected to take, charged when it is emptied.
    pile_allowance: Percent = Percent.zero()

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("a bin needs an identifier")
        if not self.capacity.is_positive():
            raise ValidationError("a bin needs a positive capacity", bin=self.identifier)
        if self.pile_allowance.hundredths < 0:
            raise ValidationError("a pile allowance cannot be negative", bin=self.identifier)
        if self.pile_allowance.hundredths > 0 and not self.kind.is_temporary:
            raise ValidationError(
                "only a ground pile carries a pile allowance", bin=self.identifier
            )

    def accepts(self, commodity_code: str) -> bool:
        return not self.allowed_commodities or commodity_code in self.allowed_commodities

    def headroom(self, held: Bushels) -> Bushels:
        return self.capacity - held


@dataclass(frozen=True, slots=True)
class BinContents:
    """What is in a bin right now: how much, of what, at what quality."""

    commodity_code: str | None = None
    quantity: Bushels = Bushels.zero()
    quality: FactorSample | None = None

    def __post_init__(self) -> None:
        if self.quantity.hundredths < 0:
            raise ValidationError("a bin cannot hold a negative quantity")
        if self.quantity.is_zero():
            if self.quality is not None or self.commodity_code is not None:
                raise ValidationError("an empty bin holds nothing, not even a quality")
        else:
            if self.commodity_code is None:
                raise ValidationError("grain in a bin has to be some crop")
            if self.quality is None:
                raise ValidationError("grain in a bin has to have a quality")

    @classmethod
    def empty(cls) -> BinContents:
        return cls()

    @property
    def is_empty(self) -> bool:
        return self.quantity.is_zero()

    def add(
        self, structure: Bin, commodity_code: str, quantity: Bushels, quality: FactorSample
    ) -> BinContents:
        """Put a load in, averaging quality across what was already there."""
        if not quantity.is_positive():
            raise ValidationError("cannot add a non positive quantity", quantity=str(quantity))
        if not structure.accepts(commodity_code):
            raise CommodityMismatch(
                "this bin is not set up for that crop",
                bin=structure.identifier,
                crop=commodity_code,
                allowed=sorted(structure.allowed_commodities),
            )
        if not self.is_empty and self.commodity_code != commodity_code:
            raise CommodityMismatch(
                "the bin already holds a different crop",
                bin=structure.identifier,
                holding=self.commodity_code,
                adding=commodity_code,
            )
        after = self.quantity + quantity
        if after > structure.capacity:
            raise BinCapacityExceeded(
                "that load would overfill the bin",
                bin=structure.identifier,
                capacity=str(structure.capacity),
                held=str(self.quantity),
                adding=str(quantity),
            )
        if self.is_empty:
            blended = quality
        else:
            assert self.quality is not None
            blended = commingle(
                (self.quality, self.quantity.hundredths), (quality, quantity.hundredths)
            )
        return BinContents(commodity_code=commodity_code, quantity=after, quality=blended)

    def draw(self, quantity: Bushels) -> tuple[BinContents, FactorSample]:
        """Take grain out. Quality of what leaves is the bin's quality."""
        if not quantity.is_positive():
            raise ValidationError("cannot draw a non positive quantity", quantity=str(quantity))
        if quantity > self.quantity:
            raise InsufficientInventory(
                "the bin does not hold that much",
                held=str(self.quantity),
                asked=str(quantity),
            )
        assert self.quality is not None
        remaining = self.quantity - quantity
        if remaining.is_zero():
            return BinContents.empty(), self.quality
        return replace(self, quantity=remaining), self.quality

    def describe(self) -> dict[str, object]:
        return {
            "commodity": self.commodity_code,
            "quantity": str(self.quantity),
            "quality": None if self.quality is None else self.quality.describe(),
        }


_BLENDED = (
    "moisture",
    "total_damage",
    "heat_damage",
    "foreign_material",
    "broken",
    "dockage",
    "other_classes",
    "shrunken_broken",
)


def commingle_many(lots: list[tuple[FactorSample, int]]) -> FactorSample:
    """Bushel weight any number of samples into one, in a single rounding.

    Folding pairwise instead would round at every step, and a blend drawn from
    eight bins can accumulate enough of that to report a hundredth of a point
    over a ceiling the true average is under. One pass, one rounding.
    """
    if not lots:
        raise ValidationError("cannot commingle nothing with nothing")
    total = sum(weight for _, weight in lots)
    if total <= 0:
        raise ValidationError("cannot commingle nothing with nothing")

    def blend(attribute: str) -> Percent:
        mixed = (
            sum(
                Decimal(getattr(sample, attribute).hundredths) * weight
                for sample, weight in lots
            )
            / total
        )
        return Percent(int(mixed.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

    test_weight = sum(sample.test_weight * weight for sample, weight in lots) / total
    worst_odor = max((sample.odor for sample, _ in lots), key=lambda odor: _ODOR_SEVERITY[odor])
    return FactorSample(
        **{name: blend(name) for name in _BLENDED},
        test_weight=test_weight.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
        odor=worst_odor,
        infested=any(sample.infested for sample, _ in lots),
    )


def commingle(left: tuple[FactorSample, int], right: tuple[FactorSample, int]) -> FactorSample:
    """Bushel weight two samples into one, taking the worst of what cannot average."""
    return commingle_many([left, right])
