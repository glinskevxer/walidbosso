"""The market position: how much grain the house is long or short, and where.

Inventory is not a market position. A bin full of corn the house has bought and
not sold is a long position and it moves with the board. The same bin full of
grain a farmer has stored and not priced is not the house's risk at all, it is
the farmer's. Getting those two the wrong way round is how an elevator hedges
the wrong number and finds out in February.

So this splits everything the house touches into three books:

**Owned and unsold.** Bought outright, or delivered under a contract where
title passed. Long the board.

**Sold and undelivered.** Forward sales the house has to fill. Short the board.

**Not the house's.** Open storage and warehouse receipts. No market position at
all, and the reason the position report and this report never agree, which is
correct and constantly surprising.

Basis is separate again. A bushel bought on a basis contract with the futures
leg unfixed is priced against the board but not against the basis, so it is
long the flat price and flat the basis. That distinction is the whole reason
this module reports two numbers rather than one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from binyard.bushels import Bushels
from binyard.contracts.contract import ContractStatus, PriceKind, PurchaseContract
from binyard.errors import ValidationError
from binyard.money import Money, Price
from binyard.obligations.obligation import ObligationKind, ObligationStatus, StorageObligation

__all__ = ["Book", "CommodityExposure", "ExposureReport", "build_exposure"]


class Book:
    """The three books a bushel can sit in."""

    OWNED = "owned"
    SOLD = "sold"
    CUSTOMER = "customer"


@dataclass(frozen=True, slots=True)
class CommodityExposure:
    """One crop's market position on one day."""

    commodity_code: str
    on_hand: Bushels
    owned_priced: Bushels
    owned_unpriced: Bushels
    customer_owned: Bushels
    sold_forward: Bushels
    basis_unfixed: Bushels
    average_cost: Price | None = None

    @property
    def owned(self) -> Bushels:
        return self.owned_priced + self.owned_unpriced

    @property
    def flat_price_position(self) -> Bushels:
        """Long, less short. Negative means the house owes more than it owns."""
        return self.owned - self.sold_forward

    @property
    def is_long(self) -> bool:
        return self.flat_price_position.hundredths > 0

    @property
    def is_short(self) -> bool:
        return self.flat_price_position.hundredths < 0

    @property
    def unhedged(self) -> Bushels:
        """The part of the position nothing on the board offsets."""
        position = self.flat_price_position
        return position if position.is_positive() else -position

    @property
    def basis_position(self) -> Bushels:
        """Bushels bought on the board but not yet basis priced."""
        return self.basis_unfixed

    def value_at(self, board: Price) -> Money:
        """What the flat price position is worth marked to a board price."""
        return board.extend(self.flat_price_position)

    def describe(self) -> dict[str, object]:
        return {
            "commodity": self.commodity_code,
            "on_hand": str(self.on_hand),
            "owned": str(self.owned),
            "owned_priced": str(self.owned_priced),
            "owned_unpriced": str(self.owned_unpriced),
            "customer_owned": str(self.customer_owned),
            "sold_forward": str(self.sold_forward),
            "flat_price_position": str(self.flat_price_position),
            "long": self.is_long,
            "short": self.is_short,
            "basis_unfixed": str(self.basis_unfixed),
            "average_cost": None if self.average_cost is None else str(self.average_cost),
        }


@dataclass(frozen=True, slots=True)
class ExposureReport:
    """Every crop, on one day."""

    on: date
    exposures: tuple[CommodityExposure, ...]

    def for_commodity(self, commodity_code: str) -> CommodityExposure | None:
        for exposure in self.exposures:
            if exposure.commodity_code == commodity_code:
                return exposure
        return None

    @property
    def shorts(self) -> tuple[CommodityExposure, ...]:
        return tuple(item for item in self.exposures if item.is_short)

    def describe(self) -> dict[str, object]:
        return {
            "on": self.on.isoformat(),
            "exposures": [item.describe() for item in self.exposures],
            "short_in": [item.commodity_code for item in self.shorts],
        }


def build_exposure(
    *,
    on: date,
    on_hand: dict[str, Bushels],
    obligations: list[StorageObligation],
    purchases: list[PurchaseContract],
    sales: dict[str, Bushels] | None = None,
) -> ExposureReport:
    """Split what the house holds into the three books and net the position.

    ``sales`` is forward sales by crop. It comes in rather than being derived,
    because a sale to a terminal is a document this package does not hold and
    inventing one from load-out orders would count a shipment twice.
    """
    forward = dict(sales or {})
    codes: list[str] = []
    for source in (on_hand.keys(), forward.keys()):
        for code in source:
            if code not in codes:
                codes.append(code)
    for obligation in obligations:
        if obligation.commodity_code not in codes:
            codes.append(obligation.commodity_code)
    for contract in purchases:
        if contract.commodity_code not in codes:
            codes.append(contract.commodity_code)

    exposures: list[CommodityExposure] = []
    for code in sorted(codes):
        priced = Bushels.zero()
        unpriced = Bushels.zero()
        customer = Bushels.zero()
        cost_numerator = 0
        cost_bushels = 0

        for obligation in obligations:
            if obligation.commodity_code != code:
                continue
            if obligation.status is ObligationStatus.CANCELLED:
                continue
            if obligation.opened_on > on:
                continue
            balance = _balance_on(obligation, on)
            if not balance.is_positive():
                continue
            if not obligation.kind.house_owns:
                customer = customer + balance
                continue
            if obligation.kind is ObligationKind.COMPANY_OWNED or obligation.is_priced:
                priced = priced + balance
                if obligation.priced_at is not None:
                    cost_numerator += obligation.priced_at.ticks * balance.hundredths
                    cost_bushels += balance.hundredths
            else:
                unpriced = unpriced + balance

        basis_open = Bushels.zero()
        for contract in purchases:
            if contract.commodity_code != code:
                continue
            if contract.status is ContractStatus.CANCELLED:
                continue
            if contract.price_kind is PriceKind.BASIS and contract.futures_price is None:
                basis_open = basis_open + contract.delivered

        average = Price(cost_numerator // cost_bushels) if cost_bushels else None
        exposures.append(
            CommodityExposure(
                commodity_code=code,
                on_hand=on_hand.get(code, Bushels.zero()),
                owned_priced=priced,
                owned_unpriced=unpriced,
                customer_owned=customer,
                sold_forward=forward.get(code, Bushels.zero()),
                basis_unfixed=basis_open,
                average_cost=average,
            )
        )
    return ExposureReport(on=on, exposures=tuple(exposures))


def _balance_on(obligation: StorageObligation, on: date) -> Bushels:
    released = Bushels.zero()
    for release in obligation.releases:
        if release.on <= on:
            released = released + release.quantity
    balance = obligation.quantity - released
    return balance if balance.is_positive() else Bushels.zero()


def check_books_balance(exposure: CommodityExposure) -> None:
    """Every bushel on hand is in exactly one book, or something is wrong.

    Called by the report builder in the tests rather than on every read. It is
    the invariant that catches a bushel counted in two books at once, which is
    the failure this module exists to avoid and the one nothing else notices.
    """
    booked = exposure.owned + exposure.customer_owned
    if booked != exposure.on_hand:
        raise ValidationError(
            "the books do not add up to what is on hand",
            commodity=exposure.commodity_code,
            on_hand=str(exposure.on_hand),
            booked=str(booked),
        )
