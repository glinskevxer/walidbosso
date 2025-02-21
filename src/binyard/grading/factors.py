"""The factors a sample is measured on, and the rules between them.

A probe pulls grain from several places in the load, the composite goes through
a moisture meter, a test weight kettle and a dockage machine, and a technician
hand picks damage out of a cut sample. What comes back is this dataclass.

Two things about factors are easy to get wrong and are settled here once.

First, **test weight runs the other way**. Every other factor is a defect and
lower is better; test weight is a density and higher is better. Grade tables
are written as limits, so a table row for test weight is a *minimum* and every
other row is a *maximum*. :func:`direction_of` is what stops that from being
re-decided in four places.

Second, **heat damage is a subset of total damage**. A sample with 0.4% heat
and 0.2% total is not a strict sample, it is a typo, and it is rejected here
rather than producing a grade nobody can defend.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from binyard.errors import FactorOutOfRange, ValidationError
from binyard.percent import Percent

__all__ = ["Direction", "FactorName", "Odor", "FactorSample", "direction_of"]


class FactorName(StrEnum):
    """Every factor a grade table or a discount schedule can name."""

    MOISTURE = "moisture"
    TEST_WEIGHT = "test_weight"
    TOTAL_DAMAGE = "total_damage"
    HEAT_DAMAGE = "heat_damage"
    FOREIGN_MATERIAL = "foreign_material"
    BROKEN = "broken"
    DOCKAGE = "dockage"
    OTHER_CLASSES = "other_classes"
    SHRUNKEN_BROKEN = "shrunken_broken"


class Direction(StrEnum):
    """Which way a factor has to move to be better."""

    LOWER_IS_BETTER = "lower_is_better"
    HIGHER_IS_BETTER = "higher_is_better"


_HIGHER_IS_BETTER = frozenset({FactorName.TEST_WEIGHT})


def direction_of(factor: FactorName) -> Direction:
    """Test weight is a minimum, everything else is a maximum."""
    if factor in _HIGHER_IS_BETTER:
        return Direction.HIGHER_IS_BETTER
    return Direction.LOWER_IS_BETTER


class Odor(StrEnum):
    """The smell of the sample, which can cap the grade on its own."""

    NONE = "none"
    MUSTY = "musty"
    SOUR = "sour"
    COMMERCIALLY_OBJECTIONABLE = "commercially_objectionable"

    @property
    def is_objectionable(self) -> bool:
        return self is not Odor.NONE


_PERCENT_FACTORS: tuple[FactorName, ...] = (
    FactorName.MOISTURE,
    FactorName.TOTAL_DAMAGE,
    FactorName.HEAT_DAMAGE,
    FactorName.FOREIGN_MATERIAL,
    FactorName.BROKEN,
    FactorName.DOCKAGE,
    FactorName.OTHER_CLASSES,
    FactorName.SHRUNKEN_BROKEN,
)


@dataclass(frozen=True, slots=True)
class FactorSample:
    """One composite sample, fully measured."""

    moisture: Percent
    test_weight: Decimal
    total_damage: Percent = Percent.zero()
    heat_damage: Percent = Percent.zero()
    foreign_material: Percent = Percent.zero()
    broken: Percent = Percent.zero()
    dockage: Percent = Percent.zero()
    other_classes: Percent = Percent.zero()
    shrunken_broken: Percent = Percent.zero()
    odor: Odor = Odor.NONE
    infested: bool = False

    def __post_init__(self) -> None:
        for name in _PERCENT_FACTORS:
            value = getattr(self, name.value)
            if not isinstance(value, Percent):
                raise ValidationError(
                    f"{name.value} must be a Percent", got=type(value).__name__
                )
            if value.hundredths < 0:
                raise FactorOutOfRange(f"{name.value} cannot be negative", value=str(value))
            if value.points > 100:
                raise FactorOutOfRange(f"{name.value} cannot exceed 100%", value=str(value))
        if not isinstance(self.test_weight, Decimal):
            raise ValidationError(
                "test weight must be a Decimal in pounds per bushel",
                got=type(self.test_weight).__name__,
            )
        if not (Decimal("0") < self.test_weight < Decimal("100")):
            raise FactorOutOfRange(
                "test weight is not a plausible pounds per bushel reading",
                value=str(self.test_weight),
            )
        if self.heat_damage > self.total_damage:
            raise FactorOutOfRange(
                "heat damage is counted inside total damage and cannot exceed it",
                heat=str(self.heat_damage),
                total=str(self.total_damage),
            )
        if self.broken > self.foreign_material + self.broken:  # pragma: no cover - guard
            raise FactorOutOfRange("broken material accounting is inconsistent")

    def reading(self, factor: FactorName) -> Decimal:
        """The numeric value of one factor, in the units its table is written in."""
        if factor is FactorName.TEST_WEIGHT:
            return self.test_weight
        value = getattr(self, factor.value)
        assert isinstance(value, Percent)
        return value.points

    def percent_of(self, factor: FactorName) -> Percent:
        if factor is FactorName.TEST_WEIGHT:
            raise ValidationError("test weight is not a percentage")
        value = getattr(self, factor.value)
        assert isinstance(value, Percent)
        return value

    @property
    def has_special_condition(self) -> bool:
        """True when something outside the numbers caps the grade."""
        return self.infested or self.odor.is_objectionable

    def without_dockage(self) -> FactorSample:
        """The sample as it grades after dockage is machine separated out.

        Wheat and sorghum are graded on what is left once dockage is removed,
        so every remaining percentage is re-based onto the smaller sample. A
        load carrying 2% dockage and 1% foreign material grades at 1.02% FM,
        not 1%, and the difference decides grades at the margin.
        """
        if self.dockage.is_zero():
            return self
        remaining = Decimal(100) - self.dockage.points
        if remaining <= 0:
            raise FactorOutOfRange("the sample is entirely dockage", dockage=str(self.dockage))
        rescale = Decimal(100) / remaining
        rebased = {
            name.value: getattr(self, name.value).scaled_by(rescale)
            for name in _PERCENT_FACTORS
            if name not in (FactorName.MOISTURE, FactorName.DOCKAGE)
        }
        return replace(self, dockage=Percent.zero(), **rebased)

    def describe(self) -> dict[str, str]:
        """A flat rendering for a ticket stub or a JSON body."""
        described = {name.value: str(getattr(self, name.value)) for name in _PERCENT_FACTORS}
        described["test_weight"] = f"{self.test_weight} lb/bu"
        described["odor"] = self.odor.value
        described["infested"] = "yes" if self.infested else "no"
        return described
