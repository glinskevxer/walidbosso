"""The daily position: what is in the bins against what is owed out of them.

This is the report the manager reads before anything else, and it answers one
question. The bins hold a number. The obligations add to a number. The
difference is the house's own position, and if it is negative the house is
short: it owes more grain than it has, which is a licence problem before it is
a trading problem.

Three subtleties, all of which have bitten somebody:

**A voided ticket is not a deleted ticket.** Its receipt movement was reversed,
so the ledger nets it out. Nothing here has to know about voids, and that is
the whole reason the ledger keeps reversals rather than removing entries.

**Deferred price grain is the house's.** Title passed when it was delivered.
It counts as company owned even though nobody has been paid, which surprises
people who expect "unpaid" and "not ours" to be the same thing.

**Grain in a pile is not worth its book number.** A ground pile carries an
allowance for what the tarp and the loader will lose, and the honest position
carries it too, as a separate line so it does not hide inside the total.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from binyard.bushels import Bushels
from binyard.dates import end_of
from binyard.errors import ValidationError
from binyard.inventory.yard import Yard
from binyard.obligations.obligation import ObligationKind, ObligationStatus, StorageObligation
from binyard.percent import Percent

__all__ = ["CommodityPosition", "PositionReport", "build_position"]


@dataclass(frozen=True, slots=True)
class CommodityPosition:
    """One crop's whole picture on one day."""

    commodity_code: str
    on_hand: Bushels
    pile_allowance: Bushels
    open_storage: Bushels
    warehouse_receipts: Bushels
    deferred_price: Bushels
    price_later: Bushels
    company_owned: Bushels

    @property
    def measurable(self) -> Bushels:
        """On hand less what a pile is expected to have lost."""
        return self.on_hand - self.pile_allowance

    @property
    def owed_out(self) -> Bushels:
        """Everything the house owes somebody: stored grain and receipts."""
        return self.open_storage + self.warehouse_receipts

    @property
    def house_position(self) -> Bushels:
        """What is left after the obligations are covered. Negative means short."""
        return self.measurable - self.owed_out

    @property
    def is_short(self) -> bool:
        return self.house_position.hundredths < 0

    @property
    def unpaid(self) -> Bushels:
        """House owned grain nobody has been paid for yet."""
        return self.deferred_price + self.price_later

    def coverage(self) -> Decimal | None:
        """Measurable inventory over what is owed out. Under one is short."""
        if self.owed_out.is_zero():
            return None
        return self.measurable.share_of(self.owed_out)

    def describe(self) -> dict[str, object]:
        cover = self.coverage()
        return {
            "commodity": self.commodity_code,
            "on_hand": str(self.on_hand),
            "pile_allowance": str(self.pile_allowance),
            "measurable": str(self.measurable),
            "open_storage": str(self.open_storage),
            "warehouse_receipts": str(self.warehouse_receipts),
            "deferred_price": str(self.deferred_price),
            "price_later": str(self.price_later),
            "company_owned": str(self.company_owned),
            "owed_out": str(self.owed_out),
            "house_position": str(self.house_position),
            "unpaid": str(self.unpaid),
            "short": self.is_short,
            "coverage": None if cover is None else f"{cover:.4f}",
        }


@dataclass(frozen=True, slots=True)
class PositionReport:
    """Every crop, on one day."""

    on: date
    location: str
    positions: tuple[CommodityPosition, ...]

    def position_for(self, commodity_code: str) -> CommodityPosition | None:
        for position in self.positions:
            if position.commodity_code == commodity_code:
                return position
        return None

    @property
    def shorts(self) -> tuple[CommodityPosition, ...]:
        return tuple(position for position in self.positions if position.is_short)

    @property
    def is_clean(self) -> bool:
        return not self.shorts

    def describe(self) -> dict[str, object]:
        return {
            "on": self.on.isoformat(),
            "location": self.location,
            "clean": self.is_clean,
            "positions": [position.describe() for position in self.positions],
        }


def build_position(
    *,
    on: date,
    location: str,
    yard: Yard,
    obligations: list[StorageObligation],
    as_of: datetime | None = None,
) -> PositionReport:
    """Fold the yard and the obligation book into one day's position."""
    if not location.strip():
        raise ValidationError("a position report needs a location")

    when = as_of if as_of is not None else end_of(on)
    states = yard.states(when=when)

    codes: list[str] = []
    for state in states:
        code = state.contents.commodity_code
        if code is not None and code not in codes:
            codes.append(code)
    for obligation in obligations:
        if obligation.commodity_code not in codes:
            codes.append(obligation.commodity_code)

    positions: list[CommodityPosition] = []
    for code in sorted(codes):
        on_hand = Bushels.zero()
        allowance = Bushels.zero()
        for state in states:
            if state.contents.commodity_code != code:
                continue
            on_hand = on_hand + state.contents.quantity
            if state.structure.pile_allowance != Percent.zero():
                allowance = allowance + state.contents.quantity.scaled_by(
                    state.structure.pile_allowance.fraction
                )
        buckets = _bucket(obligations, code, on)
        positions.append(
            CommodityPosition(
                commodity_code=code,
                on_hand=on_hand,
                pile_allowance=allowance,
                open_storage=buckets[ObligationKind.OPEN_STORAGE],
                warehouse_receipts=buckets[ObligationKind.WAREHOUSE_RECEIPT],
                deferred_price=buckets[ObligationKind.DEFERRED_PRICE],
                price_later=buckets[ObligationKind.PRICE_LATER],
                company_owned=buckets[ObligationKind.COMPANY_OWNED],
            )
        )
    return PositionReport(on=on, location=location, positions=tuple(positions))


def _bucket(
    obligations: list[StorageObligation], commodity_code: str, on: date
) -> dict[ObligationKind, Bushels]:
    totals = {kind: Bushels.zero() for kind in ObligationKind}
    for obligation in obligations:
        if obligation.commodity_code != commodity_code:
            continue
        if obligation.status is ObligationStatus.CANCELLED:
            continue
        if obligation.opened_on > on:
            continue
        totals[obligation.kind] = totals[obligation.kind] + _balance_on(obligation, on)
    return totals


def _balance_on(obligation: StorageObligation, on: date) -> Bushels:
    """The balance as it stood at the end of ``on``, ignoring later releases."""
    released = Bushels.zero()
    for release in obligation.releases:
        if release.on <= on:
            released = released + release.quantity
    balance = obligation.quantity - released
    return balance if balance.is_positive() else Bushels.zero()
