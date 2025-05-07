"""Purchase contracts: what a grower agreed to deliver and at what price.

A contract is a promise about three things, and each one can be left open:

* **How much.** Fixed bushels, with a tolerance either side, because a farmer
  cannot hit five thousand bushels to the bushel with a truck.
* **When.** A delivery window. Grain outside it is not contract grain, it is
  spot grain, and it prices differently.
* **At what.** A flat price, or a basis against a futures month with the
  futures leg still to be fixed, or nothing at all yet.

The tolerance rule is the one that trips people. A five thousand bushel
contract with a 5% tolerance is filled by anything from 4,750 to 5,250. Below
that the grower is short and owes the difference at market. Above it the excess
is not contract grain and the elevator is not obliged to take it at the
contract price. Both of those are decisions somebody makes, so this module says
which state a contract is in and leaves the decision to the desk.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.money import Money, Price
from binyard.percent import Percent

__all__ = ["PriceKind", "ContractStatus", "FillState", "Delivery", "PurchaseContract"]


class PriceKind(StrEnum):
    """How the price on this contract is arrived at."""

    #: A single number agreed the day the contract was written.
    FLAT = "flat"
    #: A differential to a named futures month. The futures leg is open.
    BASIS = "basis"
    #: Nothing agreed. The grower prices later, off whatever is posted then.
    OPEN = "open"

    @property
    def needs_futures(self) -> bool:
        return self is PriceKind.BASIS


class ContractStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class FillState(StrEnum):
    """Where delivery stands against the contracted quantity."""

    #: Nothing delivered yet.
    EMPTY = "empty"
    #: Some delivered, still under the tolerance floor.
    SHORT = "short"
    #: Inside the tolerance either side. The contract is satisfied.
    FILLED = "filled"
    #: Over the tolerance ceiling. The excess is not contract grain.
    OVER = "over"


@dataclass(frozen=True, slots=True)
class Delivery:
    """One ticket landing against this contract."""

    on: date
    ticket_number: str
    quantity: Bushels

    def __post_init__(self) -> None:
        if not self.quantity.is_positive():
            raise ValidationError("a delivery has to be positive", ticket=self.ticket_number)
        if not self.ticket_number.strip():
            raise ValidationError("a delivery needs a ticket number")


@dataclass(frozen=True, slots=True)
class PurchaseContract:
    """One agreement to buy grain from one grower."""

    identifier: str
    account: str
    commodity_code: str
    quantity: Bushels
    delivery_from: date
    delivery_to: date
    price_kind: PriceKind = PriceKind.FLAT
    flat_price: Price | None = None
    basis: Price | None = None
    futures_month: str | None = None
    futures_price: Price | None = None
    tolerance: Percent = Percent.of_points("5.0")
    status: ContractStatus = ContractStatus.OPEN
    written_on: date | None = None
    deliveries: tuple[Delivery, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("a contract needs an identifier")
        if not self.account.strip():
            raise ValidationError("a contract needs an account")
        if not self.quantity.is_positive():
            raise ValidationError(
                "a contract has to be for a positive quantity", contract=self.identifier
            )
        if self.delivery_to < self.delivery_from:
            raise ValidationError(
                "the delivery window ends before it starts", contract=self.identifier
            )
        if self.tolerance.hundredths < 0 or self.tolerance.points > 100:
            raise ValidationError("a tolerance is a percentage", contract=self.identifier)
        if self.price_kind is PriceKind.FLAT and self.flat_price is None:
            raise ValidationError(
                "a flat priced contract needs a price", contract=self.identifier
            )
        if self.price_kind is PriceKind.BASIS:
            if self.basis is None:
                raise ValidationError("a basis contract needs a basis", contract=self.identifier)
            if not self.futures_month:
                raise ValidationError(
                    "a basis contract needs a futures month", contract=self.identifier
                )
        if self.price_kind is not PriceKind.FLAT and self.flat_price is not None:
            raise ValidationError(
                "only a flat contract carries a flat price", contract=self.identifier
            )

    # -- quantities ------------------------------------------------------

    @property
    def delivered(self) -> Bushels:
        total = Bushels.zero()
        for delivery in self.deliveries:
            total = total + delivery.quantity
        return total

    @property
    def remaining(self) -> Bushels:
        left = self.quantity - self.delivered
        return left if left.is_positive() else Bushels.zero()

    @property
    def floor(self) -> Bushels:
        return self.quantity - self.quantity.scaled_by(self.tolerance.fraction)

    @property
    def ceiling(self) -> Bushels:
        return self.quantity + self.quantity.scaled_by(self.tolerance.fraction)

    @property
    def fill_state(self) -> FillState:
        delivered = self.delivered
        if delivered.is_zero():
            return FillState.EMPTY
        if delivered < self.floor:
            return FillState.SHORT
        if delivered > self.ceiling:
            return FillState.OVER
        return FillState.FILLED

    @property
    def excess(self) -> Bushels:
        over = self.delivered - self.ceiling
        return over if over.is_positive() else Bushels.zero()

    @property
    def shortfall(self) -> Bushels:
        under = self.floor - self.delivered
        return under if under.is_positive() else Bushels.zero()

    # -- price -----------------------------------------------------------

    @property
    def is_priced(self) -> bool:
        return self.price() is not None

    def price(self) -> Price | None:
        """The number a settlement would use, or ``None`` while it is still open."""
        if self.price_kind is PriceKind.FLAT:
            return self.flat_price
        if self.price_kind is PriceKind.BASIS:
            if self.futures_price is None or self.basis is None:
                return None
            return self.futures_price + self.basis
        return None

    def value_of(self, quantity: Bushels) -> Money:
        settled = self.price()
        if settled is None:
            raise ValidationError(
                "this contract has no price yet", contract=self.identifier
            )
        return settled.extend(quantity)

    def covers(self, on: date) -> bool:
        return self.delivery_from <= on <= self.delivery_to

    # -- transitions -----------------------------------------------------

    def deliver(self, delivery: Delivery) -> PurchaseContract:
        if self.status not in {ContractStatus.OPEN, ContractStatus.FILLED}:
            raise ValidationError(
                "that contract is not taking deliveries",
                contract=self.identifier,
                status=self.status.value,
            )
        moved = replace(self, deliveries=(*self.deliveries, delivery))
        if moved.fill_state in {FillState.FILLED, FillState.OVER}:
            return replace(moved, status=ContractStatus.FILLED)
        return moved

    def fix_futures(self, at: Price) -> PurchaseContract:
        if self.price_kind is not PriceKind.BASIS:
            raise ValidationError(
                "only a basis contract has a futures leg to fix", contract=self.identifier
            )
        return replace(self, futures_price=at)

    def closed(self) -> PurchaseContract:
        if self.status is ContractStatus.CANCELLED:
            raise ValidationError("a cancelled contract cannot be closed")
        return replace(self, status=ContractStatus.CLOSED)

    def cancelled(self) -> PurchaseContract:
        if self.deliveries:
            raise ValidationError(
                "a contract with deliveries against it cannot be cancelled",
                contract=self.identifier,
            )
        return replace(self, status=ContractStatus.CANCELLED)

    def describe(self) -> dict[str, object]:
        settled = self.price()
        return {
            "identifier": self.identifier,
            "account": self.account,
            "commodity": self.commodity_code,
            "quantity": str(self.quantity),
            "delivered": str(self.delivered),
            "remaining": str(self.remaining),
            "fill_state": self.fill_state.value,
            "floor": str(self.floor),
            "ceiling": str(self.ceiling),
            "shortfall": str(self.shortfall),
            "excess": str(self.excess),
            "price_kind": self.price_kind.value,
            "price": None if settled is None else str(settled),
            "status": self.status.value,
            "window": f"{self.delivery_from.isoformat()}..{self.delivery_to.isoformat()}",
        }


def tolerance_bushels(contract: PurchaseContract) -> Bushels:
    """How far either side of the contracted quantity still counts as filled."""
    return contract.quantity.scaled_by(Decimal(contract.tolerance.fraction))
