"""Who owns the bushels sitting in the bins.

The grain in bin 4 is one homogeneous mass, and the elevator's claim on it is
not. Some of it was bought outright and belongs to the house. Some is a
grower's, stored under an open account they can price any day they like. Some
is pledged against a warehouse receipt that a bank is holding as collateral,
and some is under a deferred price contract where title passed but the price
did not.

Those are different obligations with different rules, and the difference is
not cosmetic. The house owns its own bushels and carries the market risk on
them. It does not own the stored ones, it owes them, and a state warehouse
examiner counts obligations against inventory and expects the number to come
out right.

Four rules that are easy to get wrong and are enforced here:

* An **open storage** balance is bushels, not money, and it accrues storage
  after a free period.
* A **warehouse receipt** is a negotiable instrument for a fixed quantity. It
  cannot be partly released. Delivering against half of one means cancelling it
  and issuing a new receipt for the rest.
* A **deferred price** contract has passed title, so the house owns the grain,
  but the grower still has to name a price before a service charge period runs
  out.
* Nothing can be released twice, and nothing can be released for more than it
  holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import ObligationError, ValidationError
from binyard.money import Money, Price

__all__ = ["ObligationKind", "ObligationStatus", "Release", "StorageObligation"]


class ObligationKind(StrEnum):
    """What the house owes, and to whom."""

    #: The house bought it. No obligation to anyone.
    COMPANY_OWNED = "company_owned"
    #: A grower's grain, stored on open account, unpriced.
    OPEN_STORAGE = "open_storage"
    #: A negotiable receipt for a fixed quantity, usually pledged to a lender.
    WAREHOUSE_RECEIPT = "warehouse_receipt"
    #: Title passed, price not yet named. The house owns the bushels.
    DEFERRED_PRICE = "deferred_price"
    #: Delivered against a contract, priced later off a named futures month.
    PRICE_LATER = "price_later"

    @property
    def house_owns(self) -> bool:
        return self in {
            ObligationKind.COMPANY_OWNED,
            ObligationKind.DEFERRED_PRICE,
            ObligationKind.PRICE_LATER,
        }

    @property
    def accrues_storage(self) -> bool:
        return self in {ObligationKind.OPEN_STORAGE, ObligationKind.WAREHOUSE_RECEIPT}

    @property
    def divisible(self) -> bool:
        """Whether part of the balance can be released on its own."""
        return self is not ObligationKind.WAREHOUSE_RECEIPT


class ObligationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Release:
    """One draw against an obligation."""

    on: date
    quantity: Bushels
    reference: str
    note: str = ""

    def __post_init__(self) -> None:
        if not self.quantity.is_positive():
            raise ValidationError("a release has to be a positive quantity")
        if not self.reference.strip():
            raise ValidationError("a release needs a reference")


@dataclass(frozen=True, slots=True)
class StorageObligation:
    """One claim on inventory, from the day it opened to the day it closes."""

    identifier: str
    account: str
    commodity_code: str
    kind: ObligationKind
    quantity: Bushels
    opened_on: date
    #: Days after opening before storage starts. Zero means it starts at once.
    free_days: int = 0
    #: Charged per bushel per month for open storage and receipts.
    storage_rate: Price = Price.zero()
    #: Named when the grower prices a deferred or price later contract.
    priced_at: Price | None = None
    priced_on: date | None = None
    status: ObligationStatus = ObligationStatus.OPEN
    releases: tuple[Release, ...] = field(default=())
    #: Set when a receipt is cancelled and reissued, pointing at the new one.
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("an obligation needs an identifier")
        if not self.account.strip():
            raise ValidationError("an obligation needs an account")
        if not self.quantity.is_positive():
            raise ValidationError(
                "an obligation has to be for a positive quantity", obligation=self.identifier
            )
        if self.free_days < 0:
            raise ValidationError("free days cannot be negative", obligation=self.identifier)
        if self.storage_rate.ticks < 0:
            raise ValidationError("a storage rate cannot be negative")
        if self.storage_rate != Price.zero() and not self.kind.accrues_storage:
            raise ValidationError(
                "that obligation kind does not accrue storage",
                obligation=self.identifier,
                kind=self.kind.value,
            )
        if self.priced_on is not None and self.priced_at is None:
            raise ValidationError("priced on a date but at no price")
        released = sum(release.quantity.hundredths for release in self.releases)
        if released > self.quantity.hundredths:
            raise ObligationError(
                "more has been released than the obligation holds",
                obligation=self.identifier,
            )

    # -- reading ---------------------------------------------------------

    @property
    def released(self) -> Bushels:
        total = Bushels.zero()
        for release in self.releases:
            total = total + release.quantity
        return total

    @property
    def balance(self) -> Bushels:
        return self.quantity - self.released

    @property
    def is_open(self) -> bool:
        return self.status is ObligationStatus.OPEN

    @property
    def is_priced(self) -> bool:
        return self.priced_at is not None

    @property
    def needs_pricing(self) -> bool:
        return self.kind in {
            ObligationKind.DEFERRED_PRICE,
            ObligationKind.PRICE_LATER,
        } and not self.is_priced

    def storage_starts_on(self) -> date:
        from datetime import timedelta

        return self.opened_on + timedelta(days=self.free_days)

    def value(self) -> Money:
        """What the balance is worth, once a price has been named."""
        if self.priced_at is None:
            raise ObligationError(
                "this obligation has no price yet", obligation=self.identifier
            )
        return self.priced_at.extend(self.balance)

    # -- transitions -----------------------------------------------------

    def release(self, release: Release) -> StorageObligation:
        """Draw against the obligation, closing it when it empties."""
        if not self.is_open:
            raise ObligationError(
                "that obligation is not open", obligation=self.identifier, status=self.status.value
            )
        if release.on < self.opened_on:
            raise ObligationError(
                "cannot release before the obligation opened",
                obligation=self.identifier,
                on=release.on.isoformat(),
            )
        if release.quantity > self.balance:
            raise ObligationError(
                "that is more than the obligation still holds",
                obligation=self.identifier,
                balance=str(self.balance),
                asked=str(release.quantity),
            )
        if not self.kind.divisible and release.quantity != self.balance:
            raise ObligationError(
                "a warehouse receipt cannot be part released, cancel and reissue",
                obligation=self.identifier,
                balance=str(self.balance),
                asked=str(release.quantity),
            )
        releases = (*self.releases, release)
        released = sum(item.quantity.hundredths for item in releases)
        status = (
            ObligationStatus.CLOSED
            if released == self.quantity.hundredths
            else self.status
        )
        return replace(self, releases=releases, status=status)

    def priced(self, at: Price, on: date) -> StorageObligation:
        if not self.needs_pricing:
            raise ObligationError(
                "this obligation does not take a later price",
                obligation=self.identifier,
                kind=self.kind.value,
            )
        if on < self.opened_on:
            raise ObligationError("cannot price before the contract opened")
        return replace(self, priced_at=at, priced_on=on)

    def cancelled(self, superseded_by: str | None = None) -> StorageObligation:
        if not self.is_open:
            raise ObligationError(
                "only an open obligation can be cancelled", obligation=self.identifier
            )
        return replace(
            self, status=ObligationStatus.CANCELLED, superseded_by=superseded_by
        )

    def describe(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "account": self.account,
            "commodity": self.commodity_code,
            "kind": self.kind.value,
            "status": self.status.value,
            "quantity": str(self.quantity),
            "released": str(self.released),
            "balance": str(self.balance),
            "opened_on": self.opened_on.isoformat(),
            "storage_starts_on": self.storage_starts_on().isoformat(),
            "priced_at": None if self.priced_at is None else str(self.priced_at),
            "house_owns": self.kind.house_owns,
        }
