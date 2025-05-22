"""What a buyer asked for, and what the yard has to offer against it.

A shipping spec is not a grade. A buyer wanting No. 2 yellow corn at 14.5%
moisture maximum has asked for two different things: a grade, which is a walk
down a table, and a factor ceiling tighter than that grade requires. Blending
has to satisfy both, and the tighter one is usually the moisture.

The other half is what the yard can offer. A bin is not simply "available".
It has a minimum economic draw, because turning on a leg for two hundred
bushels is not worth the electricity. It may be locked out because the grain in
it is pledged, or because the bin is being fumigated. And drawing it dry has a
consequence, since the last of a bin is where the fines collect.

Both halves are here, and nothing in this module knows how to solve anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.grading.factors import Direction, FactorName, FactorSample, direction_of
from binyard.grading.tables import table_for
from binyard.inventory.yard import BinState

__all__ = ["SpecLimit", "BlendSpec", "BlendSource", "sources_from"]


@dataclass(frozen=True, slots=True)
class SpecLimit:
    """One factor the buyer put a number on."""

    factor: FactorName
    limit: Decimal

    @property
    def is_minimum(self) -> bool:
        return direction_of(self.factor) is Direction.HIGHER_IS_BETTER

    def satisfied_by(self, reading: Decimal) -> bool:
        return reading >= self.limit if self.is_minimum else reading <= self.limit

    def describe(self) -> dict[str, str]:
        return {
            "factor": self.factor.value,
            "limit": str(self.limit),
            "sense": "minimum" if self.is_minimum else "maximum",
        }


@dataclass(frozen=True, slots=True)
class BlendSpec:
    """A shipping order: this much, of this crop, no worse than this."""

    commodity_code: str
    quantity: Bushels
    minimum_grade: int | None = None
    limits: tuple[SpecLimit, ...] = ()
    #: Nothing worse than this may go in, even if the average would still pass.
    reject_worse_than: tuple[SpecLimit, ...] = ()

    def __post_init__(self) -> None:
        if not self.quantity.is_positive():
            raise ValidationError("a blend spec needs a positive quantity")
        named = [limit.factor for limit in self.limits]
        if len(set(named)) != len(named):
            raise ValidationError("a factor is limited twice", factors=[f.value for f in named])
        if self.minimum_grade is not None and self.minimum_grade < 1:
            raise ValidationError(
                "a minimum grade is a numbered grade", grade=self.minimum_grade
            )

    @classmethod
    def for_grade(
        cls, commodity_code: str, quantity: Bushels, grade: int, **overrides
    ) -> BlendSpec:
        """Build a spec from a grade, so the table's limits become the spec's."""
        row = table_for(commodity_code).row(grade)
        limits = tuple(
            SpecLimit(factor=factor, limit=limit) for factor, limit in row.limits.items()
        )
        return cls(
            commodity_code=commodity_code,
            quantity=quantity,
            minimum_grade=grade,
            limits=limits,
            **overrides,
        )

    def limit_for(self, factor: FactorName) -> SpecLimit | None:
        for limit in self.limits:
            if limit.factor is factor:
                return limit
        return None

    def accepts_average(self, quality: FactorSample) -> tuple[FactorName, ...]:
        """The factors an averaged quality would fail. Empty means it passes."""
        return tuple(
            limit.factor
            for limit in self.limits
            if not limit.satisfied_by(quality.reading(limit.factor))
        )

    def admits(self, quality: FactorSample) -> bool:
        """Whether a single bin is allowed into the blend at all."""
        return all(
            limit.satisfied_by(quality.reading(limit.factor))
            for limit in self.reject_worse_than
        )

    def describe(self) -> dict[str, object]:
        return {
            "commodity": self.commodity_code,
            "quantity": str(self.quantity),
            "minimum_grade": self.minimum_grade,
            "limits": [limit.describe() for limit in self.limits],
            "reject_worse_than": [limit.describe() for limit in self.reject_worse_than],
        }


@dataclass(frozen=True, slots=True)
class BlendSource:
    """One bin, and the terms on which it will give grain up."""

    identifier: str
    quality: FactorSample
    available: Bushels
    minimum_draw: Bushels = Bushels.zero()
    locked: bool = False
    lock_reason: str = ""

    def __post_init__(self) -> None:
        if self.available.hundredths < 0:
            raise ValidationError("a source cannot offer a negative quantity")
        if self.minimum_draw.hundredths < 0:
            raise ValidationError("a minimum draw cannot be negative")
        if self.minimum_draw > self.available:
            raise ValidationError(
                "the minimum draw is more than the bin holds",
                bin=self.identifier,
                available=str(self.available),
                minimum=str(self.minimum_draw),
            )
        if self.locked and not self.lock_reason.strip():
            raise ValidationError("a locked bin needs a reason", bin=self.identifier)

    @property
    def usable(self) -> bool:
        return not self.locked and self.available.is_positive()

    def reading(self, factor: FactorName) -> Decimal:
        return self.quality.reading(factor)

    def describe(self) -> dict[str, object]:
        return {
            "bin": self.identifier,
            "available": str(self.available),
            "minimum_draw": str(self.minimum_draw),
            "locked": self.locked,
            "lock_reason": self.lock_reason,
        }


def sources_from(
    states: tuple[BinState, ...],
    commodity_code: str,
    *,
    minimum_draw: Bushels = Bushels.zero(),
    locked: frozenset[str] = frozenset(),
) -> tuple[BlendSource, ...]:
    """Turn the yard's bins into offers, skipping the ones with nothing to give."""
    offers: list[BlendSource] = []
    for state in states:
        contents = state.contents
        if contents.commodity_code != commodity_code or contents.quality is None:
            continue
        if not contents.quantity.is_positive():
            continue
        floor = min(minimum_draw, contents.quantity)
        offers.append(
            BlendSource(
                identifier=state.identifier,
                quality=contents.quality,
                available=contents.quantity,
                minimum_draw=floor,
                locked=state.identifier in locked,
                lock_reason="held out by the desk" if state.identifier in locked else "",
            )
        )
    return tuple(offers)
