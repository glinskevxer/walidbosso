"""The movement ledger: every bushel that entered or left a bin, in order.

The bin's contents are not stored. They are a fold over this ledger, which
sounds like extra work and buys three things worth more than the work.

A state warehouse examiner asks what bin 4 held on the morning of 12 November.
With stored contents that question needs a nightly snapshot table that somebody
has to remember to write. With a ledger it is a fold with a cutoff.

Second, an adjustment is a first class entry rather than an edit. When the
annual measure-up says the bin holds four hundred fewer bushels than the book,
that four hundred is posted with a reason and a name against it, and it shows
on the position report as shrink rather than quietly disappearing.

Third, a receipt and the ticket that caused it are tied together by reference,
so an ordinary void can find every movement it has to reverse.

Entries are never removed or rewritten. A mistake is corrected by posting its
reverse, which is why :class:`Movement` carries a signed quantity and the
ledger will accept a negative receipt.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import ValidationError
from binyard.grading.factors import FactorSample

__all__ = ["Ledger", "Movement", "MovementKind"]


class MovementKind(StrEnum):
    """Why grain moved."""

    #: A truck dumped into this bin.
    RECEIPT = "receipt"
    #: A truck or a car loaded out of this bin.
    SHIPMENT = "shipment"
    #: Grain moved between two bins in this yard. Posted twice, once each way.
    TRANSFER = "transfer"
    #: The dryer took water and mass out of what came back.
    DRYER_LOSS = "dryer_loss"
    #: A measure-up found the book and the bin disagree.
    ADJUSTMENT = "adjustment"
    #: A pile was emptied and the allowance was taken.
    PILE_ALLOWANCE = "pile_allowance"

    @property
    def adds(self) -> bool:
        return self in {MovementKind.RECEIPT, MovementKind.TRANSFER, MovementKind.ADJUSTMENT}

    @property
    def is_correction(self) -> bool:
        return self in {MovementKind.ADJUSTMENT, MovementKind.PILE_ALLOWANCE}


@dataclass(frozen=True, slots=True)
class Movement:
    """One posting. The quantity is signed: positive in, negative out."""

    at: datetime
    bin_identifier: str
    kind: MovementKind
    commodity_code: str
    quantity: Bushels
    reference: str
    by: str
    quality: FactorSample | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.quantity.is_zero():
            raise ValidationError(
                "a movement of nothing is not a movement", reference=self.reference
            )
        if not self.reference.strip():
            raise ValidationError("a movement needs a reference")
        if not self.by.strip():
            raise ValidationError("a movement needs a name against it")
        if self.quantity.is_positive() and self.quality is None:
            raise ValidationError(
                "grain going into a bin has to carry a quality",
                reference=self.reference,
            )

    @property
    def is_inbound(self) -> bool:
        return self.quantity.is_positive()

    def reversal(self, at: datetime, by: str, reason: str) -> Movement:
        """The entry that undoes this one."""
        return Movement(
            at=at,
            bin_identifier=self.bin_identifier,
            kind=self.kind,
            commodity_code=self.commodity_code,
            quantity=-self.quantity,
            reference=self.reference,
            by=by,
            quality=self.quality,
            note=f"reverses {self.reference}: {reason}",
        )

    def describe(self) -> dict[str, object]:
        return {
            "at": self.at.isoformat(),
            "bin": self.bin_identifier,
            "kind": self.kind.value,
            "commodity": self.commodity_code,
            "quantity": str(self.quantity),
            "reference": self.reference,
            "by": self.by,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class Ledger:
    """An append only, time ordered list of movements."""

    entries: tuple[Movement, ...] = ()

    def __post_init__(self) -> None:
        times = [entry.at for entry in self.entries]
        if times != sorted(times):
            raise ValidationError("ledger entries have to be in time order")

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)

    def post(self, movement: Movement) -> Ledger:
        """Append one movement, keeping the ledger in time order."""
        index = bisect_right([entry.at for entry in self.entries], movement.at)
        entries = (*self.entries[:index], movement, *self.entries[index:])
        return Ledger(entries=entries)

    def post_all(self, movements: list[Movement]) -> Ledger:
        ledger = self
        for movement in movements:
            ledger = ledger.post(movement)
        return ledger

    def up_to(self, when: datetime) -> Ledger:
        """Everything posted at or before ``when``, which is how history is read."""
        index = bisect_right([entry.at for entry in self.entries], when)
        return Ledger(entries=self.entries[:index])

    def for_bin(self, identifier: str) -> Ledger:
        return Ledger(entries=tuple(e for e in self.entries if e.bin_identifier == identifier))

    def for_reference(self, reference: str) -> Ledger:
        return Ledger(entries=tuple(e for e in self.entries if e.reference == reference))

    def bins(self) -> tuple[str, ...]:
        seen: list[str] = []
        for entry in self.entries:
            if entry.bin_identifier not in seen:
                seen.append(entry.bin_identifier)
        return tuple(seen)

    def balance(self, identifier: str) -> Bushels:
        """The net quantity in one bin, without recomputing quality."""
        total = Bushels.zero()
        for entry in self.entries:
            if entry.bin_identifier == identifier:
                total = total + entry.quantity
        return total
