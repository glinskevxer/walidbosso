"""Factories the suite shares.

Every builder takes keyword overrides and fills the rest with something
plausible for a mid October day at a two pit country elevator. Tests say only
what they care about, which keeps the interesting value visible on the line
that asserts on it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from binyard import commodity
from binyard.commodity import CropStandard
from binyard.discounts.posted import default_book
from binyard.grading.factors import FactorSample
from binyard.identity import LocationCode, Sequence, TicketNumber
from binyard.percent import Percent, percent
from binyard.shrink import ShrinkPolicy
from binyard.tickets import ScaleTicket, ScaleWeights, TicketKind
from binyard.units import Weight, pounds

HARVEST_DAY = date(2025, 10, 14)
HARVEST_NOON = datetime(2025, 10, 14, 12, 0, 0)
LOCATION = LocationCode("WBS")


@pytest.fixture
def book():
    return default_book()


@pytest.fixture
def sequence():
    return Sequence(LOCATION, 2025)


def sample_for(standard: CropStandard = commodity.CORN, **overrides) -> FactorSample:
    """A clean composite for ``standard``, unless told otherwise."""
    defaults: dict[str, object] = {
        "moisture": standard.market_moisture,
        "test_weight": standard.pounds_per_bushel + Decimal("1"),
        "total_damage": percent("1.0"),
        "heat_damage": Percent.zero(),
    }
    if standard.code == "CORN":
        defaults["broken"] = percent("1.0")
    if standard.code in {"SOYBEANS", "HRW_WHEAT", "SRW_WHEAT"}:
        defaults["foreign_material"] = percent("0.2")
    defaults.update(overrides)
    return FactorSample(**defaults)  # type: ignore[arg-type]


def weights_for(net: str = "48240", tare: str = "30180", **overrides) -> ScaleWeights:
    gross = pounds(tare) + pounds(net)
    fields: dict[str, object] = {"gross": gross, "tare": pounds(tare)}
    fields.update(overrides)
    return ScaleWeights(**fields)  # type: ignore[arg-type]


def ticket_for(
    number: TicketNumber | None = None,
    standard: CropStandard = commodity.CORN,
    **overrides,
) -> ScaleTicket:
    fields: dict[str, object] = {
        "number": number or TicketNumber(LOCATION, 2025, 1),
        "kind": TicketKind.INBOUND,
        "commodity_code": standard.code,
        "account": "HALVORSEN FARMS",
        "weighed_on": HARVEST_DAY,
        "weights": weights_for(),
    }
    fields.update(overrides)
    return ScaleTicket(**fields)  # type: ignore[arg-type]


def graded_ticket(
    standard: CropStandard = commodity.CORN,
    sample: FactorSample | None = None,
    at: datetime = HARVEST_NOON,
    by: str = "scalehouse",
    **overrides,
) -> ScaleTicket:
    ticket = ticket_for(standard=standard, **overrides)
    return ticket.graded_as(
        sample or sample_for(standard),
        standard,
        ShrinkPolicy.market_default(standard),
        at=at,
        by=by,
    )


@pytest.fixture
def corn_policy():
    return ShrinkPolicy.market_default(commodity.CORN)


@pytest.fixture
def full_load() -> Weight:
    return pounds("48240")
