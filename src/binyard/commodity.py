"""What the elevator handles, and the constants that follow from the crop.

A bushel is a volume, but grain is bought by weight, so every crop carries a
"test weight per bushel" that the trade fixed long ago: 56 lb for corn, 60 for
soybeans and wheat, 32 for oats. That number converts pounds on the scale into
bushels on the ticket, and it is *not* the same as the measured test weight of
a particular load, which is a grade factor. Confusing the two is the classic
way to be several thousand bushels out at the end of a season, so they are
different names here and never the same type.

Each crop also carries the moisture the market pays at. Corn trades at 15.0%,
soybeans at 13.0%, wheat at 13.5%. Anything wetter is shrunk down to that
number before it counts as inventory, and anything drier is simply a gift to
whoever delivered it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final

from binyard.errors import UnknownCommodity

__all__ = [
    "PricingBasis",
    "CropStandard",
    "CORN",
    "SOYBEANS",
    "HRW_WHEAT",
    "SRW_WHEAT",
    "GRAIN_SORGHUM",
    "OATS",
    "SUNFLOWER",
    "STANDARDS",
    "lookup",
    "codes",
]


class PricingBasis(StrEnum):
    """The unit a contract is written in."""

    BUSHEL = "bushel"
    HUNDREDWEIGHT = "hundredweight"


@dataclass(frozen=True, slots=True)
class CropStandard:
    """Everything about a crop that does not change from load to load."""

    code: str
    name: str
    #: Pounds of grain the trade calls one bushel. Fixed by crop, never measured.
    pounds_per_bushel: Decimal
    #: The moisture a contract is priced at.
    market_moisture: Decimal
    #: Above this the elevator will not take the load at all without drying.
    receiving_moisture_ceiling: Decimal
    #: Grain sorghum and every wheat class are graded after dockage is removed.
    dockage_removed_before_grading: bool
    pricing_basis: PricingBasis = PricingBasis.BUSHEL
    #: Other spellings the scale house and the grain desk actually use.
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.pounds_per_bushel <= 0:
            raise ValueError(f"{self.code} needs a positive bushel weight")
        if not (0 < self.market_moisture < 100):
            raise ValueError(f"{self.code} market moisture is not a percentage")
        if self.receiving_moisture_ceiling <= self.market_moisture:
            raise ValueError(
                f"{self.code} would refuse loads it is willing to pay for"
            )

    @property
    def priced_by_the_bushel(self) -> bool:
        return self.pricing_basis is PricingBasis.BUSHEL

    def matches(self, text: str) -> bool:
        folded = text.strip().upper().replace(" ", "_")
        return folded == self.code or folded in {a.upper() for a in self.aliases}


CORN: Final = CropStandard(
    code="CORN",
    name="Yellow corn",
    pounds_per_bushel=Decimal("56"),
    market_moisture=Decimal("15.0"),
    receiving_moisture_ceiling=Decimal("25.0"),
    dockage_removed_before_grading=False,
    aliases=("YELLOW_CORN", "NO2_YELLOW", "MAIZE"),
)

SOYBEANS: Final = CropStandard(
    code="SOYBEANS",
    name="Yellow soybeans",
    pounds_per_bushel=Decimal("60"),
    market_moisture=Decimal("13.0"),
    receiving_moisture_ceiling=Decimal("20.0"),
    dockage_removed_before_grading=False,
    aliases=("BEANS", "SOYBEAN", "SOY"),
)

HRW_WHEAT: Final = CropStandard(
    code="HRW_WHEAT",
    name="Hard red winter wheat",
    pounds_per_bushel=Decimal("60"),
    market_moisture=Decimal("13.5"),
    receiving_moisture_ceiling=Decimal("18.0"),
    dockage_removed_before_grading=True,
    aliases=("HRW", "HARD_RED_WINTER"),
)

SRW_WHEAT: Final = CropStandard(
    code="SRW_WHEAT",
    name="Soft red winter wheat",
    pounds_per_bushel=Decimal("60"),
    market_moisture=Decimal("13.5"),
    receiving_moisture_ceiling=Decimal("18.0"),
    dockage_removed_before_grading=True,
    aliases=("SRW", "SOFT_RED_WINTER"),
)

GRAIN_SORGHUM: Final = CropStandard(
    code="GRAIN_SORGHUM",
    name="Grain sorghum",
    pounds_per_bushel=Decimal("56"),
    market_moisture=Decimal("14.0"),
    receiving_moisture_ceiling=Decimal("20.0"),
    dockage_removed_before_grading=True,
    aliases=("MILO", "SORGHUM"),
)

OATS: Final = CropStandard(
    code="OATS",
    name="Oats",
    pounds_per_bushel=Decimal("32"),
    market_moisture=Decimal("14.0"),
    receiving_moisture_ceiling=Decimal("18.0"),
    dockage_removed_before_grading=True,
    aliases=("OAT",),
)

SUNFLOWER: Final = CropStandard(
    code="SUNFLOWER",
    name="Oil type sunflower",
    pounds_per_bushel=Decimal("25"),
    market_moisture=Decimal("10.0"),
    receiving_moisture_ceiling=Decimal("18.0"),
    dockage_removed_before_grading=True,
    pricing_basis=PricingBasis.HUNDREDWEIGHT,
    aliases=("SUNFLOWERS", "SUNS", "OIL_SUNFLOWER"),
)

STANDARDS: Final[tuple[CropStandard, ...]] = (
    CORN,
    SOYBEANS,
    HRW_WHEAT,
    SRW_WHEAT,
    GRAIN_SORGHUM,
    OATS,
    SUNFLOWER,
)


def lookup(text: str) -> CropStandard:
    """Resolve whatever the scale house typed into a crop standard."""
    if not text or not text.strip():
        raise UnknownCommodity("no commodity was named", got=text)
    for standard in STANDARDS:
        if standard.matches(text):
            return standard
    raise UnknownCommodity(
        "this elevator does not handle that crop",
        got=text.strip(),
        handled=[s.code for s in STANDARDS],
    )


def codes() -> tuple[str, ...]:
    """The canonical codes, in the order the grain desk lists them."""
    return tuple(s.code for s in STANDARDS)
