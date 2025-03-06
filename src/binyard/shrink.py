"""Shrink: how many bushels are left once the water comes out.

Wet grain is mostly grain and partly water. Drying takes the water away, and
the load gets lighter. A farmer who delivers 1,000 bushels of 20% corn does not
have 1,000 bushels of 15% corn afterwards, they have about 941, and the elevator
has to say so before it puts anything in a bin.

Two shrinks stack, and they are different things:

**Water shrink** is arithmetic. Removing moisture from ``wet`` down to
``dry`` takes away ``(wet - dry) / (100 - dry)`` of the mass. It is not a
percentage per point, it is a ratio, and the per-point figure people quote
(1.18 for corn to 15%) is that ratio rounded and then re-multiplied.

**Handling shrink** is a business term, not physics. Grain sticks to augers,
blows out of the dryer and gets eaten. Elevators post a flat percentage for it,
usually half a point, and it is charged on the dried quantity, not the wet one.

Which of the two conventions a house uses is posted, so a policy can either
carry an exact target and compute water shrink, or carry the posted per point
factor and use that instead. Both are supported because both are in use, and a
settlement that mixes them is off by tenths of a percent on every load.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from binyard.bushels import Bushels
from binyard.commodity import CropStandard
from binyard.errors import ValidationError
from binyard.percent import Percent

__all__ = ["ShrinkPolicy", "ShrinkResult", "water_shrink_ratio", "apply_shrink"]


def water_shrink_ratio(wet: Percent, dry: Percent) -> Decimal:
    """The fraction of mass lost taking moisture from ``wet`` down to ``dry``."""
    if dry >= Percent.of_points(100):
        raise ValidationError("target moisture must be under 100%", target=str(dry))
    if wet <= dry:
        return Decimal(0)
    removed = Decimal(wet.hundredths - dry.hundredths)
    remaining_dry_matter = Decimal(100 * 100 - dry.hundredths)
    return removed / remaining_dry_matter


@dataclass(frozen=True, slots=True)
class ShrinkPolicy:
    """What one house posts for one crop."""

    commodity_code: str
    target_moisture: Percent
    handling_shrink: Percent = Percent.zero()
    #: Posted percent of the load lost per point removed. ``None`` uses the ratio.
    posted_factor_per_point: Decimal | None = None
    #: Loads at or under this moisture are not shrunk at all.
    shrink_threshold: Percent | None = None

    def __post_init__(self) -> None:
        if self.target_moisture.hundredths <= 0:
            raise ValidationError("target moisture must be positive", crop=self.commodity_code)
        if self.target_moisture.points >= 100:
            raise ValidationError("target moisture must be under 100%", crop=self.commodity_code)
        if self.handling_shrink.hundredths < 0:
            raise ValidationError("handling shrink cannot be negative", crop=self.commodity_code)
        if self.handling_shrink.points >= 100:
            raise ValidationError("handling shrink cannot take the whole load")
        if self.posted_factor_per_point is not None and self.posted_factor_per_point < 0:
            raise ValidationError("a posted shrink factor cannot be negative")
        if self.shrink_threshold is not None and self.shrink_threshold < self.target_moisture:
            raise ValidationError(
                "a shrink threshold below the target would leave wet grain unshrunk",
                threshold=str(self.shrink_threshold),
                target=str(self.target_moisture),
            )

    @classmethod
    def market_default(cls, standard: CropStandard) -> ShrinkPolicy:
        """The plain policy: shrink to the market moisture, half a point handling."""
        return cls(
            commodity_code=standard.code,
            target_moisture=Percent.of_points(standard.market_moisture),
            handling_shrink=Percent.of_points("0.5"),
        )

    def points_removed(self, moisture: Percent) -> Percent:
        if self.shrink_threshold is not None and moisture <= self.shrink_threshold:
            return Percent.zero()
        if moisture <= self.target_moisture:
            return Percent.zero()
        return moisture - self.target_moisture


@dataclass(frozen=True, slots=True)
class ShrinkResult:
    """The full accounting for one load, so a stub can print every line."""

    gross: Bushels
    points_removed: Percent
    water_shrink: Bushels
    handling_shrink: Bushels
    net: Bushels

    @property
    def total_shrink(self) -> Bushels:
        return self.water_shrink + self.handling_shrink

    @property
    def shrunk(self) -> bool:
        return not self.total_shrink.is_zero()

    def describe(self) -> dict[str, str]:
        return {
            "gross": str(self.gross),
            "points_removed": str(self.points_removed),
            "water_shrink": str(self.water_shrink),
            "handling_shrink": str(self.handling_shrink),
            "total_shrink": str(self.total_shrink),
            "net": str(self.net),
        }


def apply_shrink(gross: Bushels, moisture: Percent, policy: ShrinkPolicy) -> ShrinkResult:
    """Shrink ``gross`` bushels delivered at ``moisture`` under ``policy``.

    Water comes out first, then handling is taken from what is left. Both
    figures are rounded to the hundredth of a bushel and the net is the gross
    less the two rounded figures, so the printed lines add up on the stub
    rather than nearly adding up.
    """
    if not isinstance(gross, Bushels):
        raise ValidationError("shrink applies to bushels", got=type(gross).__name__)
    if gross.hundredths < 0:
        raise ValidationError("cannot shrink a negative quantity", gross=str(gross))

    points = policy.points_removed(moisture)

    if points.is_zero():
        water = Bushels.zero()
    elif policy.posted_factor_per_point is not None:
        rate = policy.posted_factor_per_point * points.points / 100
        water = _round_bushels(Decimal(gross.hundredths) * rate)
    else:
        ratio = water_shrink_ratio(moisture, policy.target_moisture)
        water = _round_bushels(Decimal(gross.hundredths) * ratio)

    dried = gross - water
    if policy.handling_shrink.is_zero():
        handling = Bushels.zero()
    else:
        handling = _round_bushels(
            Decimal(dried.hundredths) * policy.handling_shrink.fraction
        )

    return ShrinkResult(
        gross=gross,
        points_removed=points,
        water_shrink=water,
        handling_shrink=handling,
        net=gross - water - handling,
    )


def _round_bushels(hundredths: Decimal) -> Bushels:
    return Bushels(int(hundredths.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
