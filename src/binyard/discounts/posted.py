"""The board this house actually posts, as of the start of the crop year.

These are ordinary commercial numbers, not standards, and they change. They
live here as a starting book so a fresh install can take a load on day one, and
so the tests have something realistic to work against. A live house replaces
them through :class:`~binyard.discounts.schedule.ScheduleBook`, which keeps the
old ones effective dated rather than overwriting them, because a settlement
re-run for last October has to use last October's board.

Moisture is cumulative here: a wet load pays each band for the part of the
reading inside it. Damage, foreign material and test weight are stepped, which
is how they are written on the sign.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

from binyard.discounts.schedule import (
    Accumulation,
    Band,
    Basis,
    FactorSchedule,
    GradeSchedule,
    PostedSchedule,
    ScheduleBook,
)
from binyard.grading.factors import FactorName
from binyard.money import Price

__all__ = ["SEASON_START", "default_book", "CORN_SCHEDULE", "SOYBEAN_SCHEDULE"]

SEASON_START: Final = date(2025, 9, 1)


def _cents(value: str) -> Price:
    return Price.from_cents(value)


def _d(value: str) -> Decimal:
    return Decimal(value)


CORN_SCHEDULE: Final = PostedSchedule(
    commodity_code="CORN",
    effective_from=SEASON_START,
    factors=(
        FactorSchedule(
            factor=FactorName.MOISTURE,
            accumulation=Accumulation.CUMULATIVE,
            bands=(
                Band(_d("15.0"), _d("16.0"), _cents("3"), Basis.PER_POINT),
                Band(_d("16.0"), _d("18.0"), _cents("5"), Basis.PER_POINT),
                Band(_d("18.0"), _d("22.0"), _cents("8"), Basis.PER_POINT),
                Band(_d("22.0"), _d("25.0"), _cents("12"), Basis.PER_POINT),
                Band(_d("25.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.TOTAL_DAMAGE,
            bands=(
                Band(_d("5.0"), _d("10.0"), _cents("2"), Basis.PER_POINT),
                Band(_d("10.0"), _d("20.0"), _cents("4"), Basis.PER_POINT),
                Band(_d("20.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.BROKEN,
            bands=(
                Band(_d("3.0"), _d("7.0"), _cents("1"), Basis.PER_POINT),
                Band(_d("7.0"), None, _cents("3"), Basis.PER_POINT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.HEAT_DAMAGE,
            bands=(
                Band(_d("0.2"), _d("3.0"), _cents("10"), Basis.FLAT),
                Band(_d("3.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
    ),
    grades=GradeSchedule(
        rates={
            1: Price.zero(),
            2: Price.zero(),
            3: _cents("2"),
            4: _cents("6"),
            5: _cents("12"),
            0: _cents("30"),
        }
    ),
)

SOYBEAN_SCHEDULE: Final = PostedSchedule(
    commodity_code="SOYBEANS",
    effective_from=SEASON_START,
    factors=(
        FactorSchedule(
            factor=FactorName.MOISTURE,
            accumulation=Accumulation.CUMULATIVE,
            bands=(
                Band(_d("13.0"), _d("14.0"), _cents("4"), Basis.PER_POINT),
                Band(_d("14.0"), _d("16.0"), _cents("7"), Basis.PER_POINT),
                Band(_d("16.0"), _d("20.0"), _cents("11"), Basis.PER_POINT),
                Band(_d("20.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.FOREIGN_MATERIAL,
            bands=(
                Band(_d("1.0"), _d("5.0"), _cents("2"), Basis.PER_POINT),
                Band(_d("5.0"), None, _cents("5"), Basis.PER_POINT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.BROKEN,
            bands=(
                Band(_d("20.0"), _d("40.0"), _cents("1"), Basis.PER_POINT),
                Band(_d("40.0"), None, _cents("2"), Basis.PER_POINT),
            ),
        ),
    ),
    grades=GradeSchedule(
        rates={1: Price.zero(), 2: Price.zero(), 3: _cents("5"), 4: _cents("15"), 0: _cents("40")}
    ),
)

WHEAT_FACTORS: Final = (
    FactorSchedule(
        factor=FactorName.MOISTURE,
        accumulation=Accumulation.CUMULATIVE,
        bands=(
            Band(_d("13.5"), _d("14.5"), _cents("4"), Basis.PER_POINT),
            Band(_d("14.5"), _d("16.0"), _cents("8"), Basis.PER_POINT),
            Band(_d("16.0"), None, Price.zero(), Basis.REJECT),
        ),
    ),
    FactorSchedule(
        factor=FactorName.DOCKAGE,
        bands=(
            Band(_d("1.0"), _d("5.0"), _cents("3"), Basis.PER_POINT),
            Band(_d("5.0"), None, _cents("6"), Basis.PER_POINT),
        ),
    ),
    FactorSchedule(
        factor=FactorName.SHRUNKEN_BROKEN,
        bands=(Band(_d("5.0"), None, _cents("1"), Basis.PER_POINT),),
    ),
)

WHEAT_GRADES: Final = GradeSchedule(
    rates={
        1: Price.zero(),
        2: Price.zero(),
        3: _cents("5"),
        4: _cents("15"),
        5: _cents("30"),
        0: _cents("60"),
    }
)

HRW_SCHEDULE: Final = PostedSchedule(
    commodity_code="HRW_WHEAT",
    effective_from=SEASON_START,
    factors=WHEAT_FACTORS,
    grades=WHEAT_GRADES,
)

SRW_SCHEDULE: Final = PostedSchedule(
    commodity_code="SRW_WHEAT",
    effective_from=SEASON_START,
    factors=WHEAT_FACTORS,
    grades=WHEAT_GRADES,
)

SORGHUM_SCHEDULE: Final = PostedSchedule(
    commodity_code="GRAIN_SORGHUM",
    effective_from=SEASON_START,
    factors=(
        FactorSchedule(
            factor=FactorName.MOISTURE,
            accumulation=Accumulation.CUMULATIVE,
            bands=(
                Band(_d("14.0"), _d("16.0"), _cents("4"), Basis.PER_POINT),
                Band(_d("16.0"), _d("20.0"), _cents("7"), Basis.PER_POINT),
                Band(_d("20.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.TOTAL_DAMAGE,
            bands=(Band(_d("5.0"), None, _cents("3"), Basis.PER_POINT),),
        ),
    ),
    grades=GradeSchedule(
        rates={1: Price.zero(), 2: Price.zero(), 3: _cents("4"), 4: _cents("10"), 0: _cents("35")}
    ),
)

OAT_SCHEDULE: Final = PostedSchedule(
    commodity_code="OATS",
    effective_from=SEASON_START,
    factors=(
        FactorSchedule(
            factor=FactorName.MOISTURE,
            accumulation=Accumulation.CUMULATIVE,
            bands=(
                Band(_d("14.0"), _d("16.0"), _cents("3"), Basis.PER_POINT),
                Band(_d("16.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.FOREIGN_MATERIAL,
            bands=(Band(_d("3.0"), None, _cents("2"), Basis.PER_POINT),),
        ),
    ),
    grades=GradeSchedule(rates={1: Price.zero(), 2: Price.zero(), 3: _cents("3"), 4: _cents("8"), 0: _cents("25")}),
)

SUNFLOWER_SCHEDULE: Final = PostedSchedule(
    commodity_code="SUNFLOWER",
    effective_from=SEASON_START,
    factors=(
        FactorSchedule(
            factor=FactorName.MOISTURE,
            accumulation=Accumulation.CUMULATIVE,
            bands=(
                Band(_d("10.0"), _d("12.0"), _cents("5"), Basis.PER_POINT),
                Band(_d("12.0"), _d("18.0"), _cents("9"), Basis.PER_POINT),
                Band(_d("18.0"), None, Price.zero(), Basis.REJECT),
            ),
        ),
        FactorSchedule(
            factor=FactorName.FOREIGN_MATERIAL,
            bands=(Band(_d("2.0"), None, _cents("3"), Basis.PER_POINT),),
        ),
    ),
    grades=GradeSchedule(rates={1: Price.zero(), 2: _cents("10"), 0: _cents("50")}),
)


def default_book() -> ScheduleBook:
    """Every crop's opening board, in one book."""
    return ScheduleBook(
        schedules=(
            CORN_SCHEDULE,
            SOYBEAN_SCHEDULE,
            HRW_SCHEDULE,
            SRW_SCHEDULE,
            SORGHUM_SCHEDULE,
            OAT_SCHEDULE,
            SUNFLOWER_SCHEDULE,
        )
    )
