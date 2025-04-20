"""The yard: the bins this house has, and the ledger that says what is in them.

This is the only place that knows both. :class:`Bin` does not know its own
contents, and the ledger does not know a bin's capacity, so neither of them can
tell you on its own whether the next load fits. The yard can, and every
movement goes through it so that check happens exactly once.

Contents are folded from the ledger every time they are asked for. That is
linear in the number of movements for that bin, which for a season and a bin is
a few thousand entries, and it takes microseconds. When it stops being cheap
the fix is a cached fold keyed on the ledger length, not a stored total that
can drift from the entries that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from binyard.bushels import Bushels
from binyard.errors import (
    CommodityMismatch,
    InsufficientInventory,
    NotFound,
    ValidationError,
)
from binyard.grading.factors import FactorSample
from binyard.inventory.bin import Bin, BinContents
from binyard.inventory.ledger import Ledger, Movement, MovementKind

__all__ = ["Yard", "BinState"]


@dataclass(frozen=True, slots=True)
class BinState:
    """A bin and what it is holding, together."""

    structure: Bin
    contents: BinContents

    @property
    def identifier(self) -> str:
        return self.structure.identifier

    @property
    def headroom(self) -> Bushels:
        return self.structure.headroom(self.contents.quantity)

    @property
    def is_empty(self) -> bool:
        return self.contents.is_empty

    @property
    def utilisation(self):
        return self.contents.quantity.share_of(self.structure.capacity)

    def describe(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "name": self.structure.name,
            "kind": self.structure.kind.value,
            "capacity": str(self.structure.capacity),
            "headroom": str(self.headroom),
            "contents": self.contents.describe(),
        }


@dataclass(frozen=True, slots=True)
class Yard:
    """Every bin at one location, plus the movement ledger."""

    bins: tuple[Bin, ...] = ()
    ledger: Ledger = Ledger()

    def __post_init__(self) -> None:
        identifiers = [structure.identifier for structure in self.bins]
        if len(set(identifiers)) != len(identifiers):
            raise ValidationError("two bins share an identifier", bins=identifiers)

    # -- reading ---------------------------------------------------------

    def bin(self, identifier: str) -> Bin:
        for structure in self.bins:
            if structure.identifier == identifier:
                return structure
        raise NotFound("bin", identifier)

    def contents_of(self, identifier: str, *, when: datetime | None = None) -> BinContents:
        """Fold the ledger for one bin, optionally as it stood at a moment."""
        self.bin(identifier)
        ledger = self.ledger if when is None else self.ledger.up_to(when)
        contents = BinContents.empty()
        structure = self.bin(identifier)
        for entry in ledger.for_bin(identifier):
            contents = _apply(structure, contents, entry)
        return contents

    def state_of(self, identifier: str, *, when: datetime | None = None) -> BinState:
        return BinState(self.bin(identifier), self.contents_of(identifier, when=when))

    def states(self, *, when: datetime | None = None) -> tuple[BinState, ...]:
        return tuple(self.state_of(s.identifier, when=when) for s in self.bins)

    def holding(self, commodity_code: str, *, when: datetime | None = None) -> tuple[BinState, ...]:
        return tuple(
            state
            for state in self.states(when=when)
            if state.contents.commodity_code == commodity_code
        )

    def total_of(self, commodity_code: str, *, when: datetime | None = None) -> Bushels:
        total = Bushels.zero()
        for state in self.holding(commodity_code, when=when):
            total = total + state.contents.quantity
        return total

    def with_bin(self, structure: Bin) -> Yard:
        return replace(self, bins=(*self.bins, structure))

    # -- writing ---------------------------------------------------------

    def post(self, movement: Movement) -> Yard:
        """Post one movement, refusing anything the bin cannot take."""
        structure = self.bin(movement.bin_identifier)
        contents = self.contents_of(movement.bin_identifier)
        _apply(structure, contents, movement)
        return replace(self, ledger=self.ledger.post(movement))

    def receive(
        self,
        identifier: str,
        commodity_code: str,
        quantity: Bushels,
        quality: FactorSample,
        *,
        at: datetime,
        reference: str,
        by: str,
        kind: MovementKind = MovementKind.RECEIPT,
    ) -> Yard:
        return self.post(
            Movement(
                at=at,
                bin_identifier=identifier,
                kind=kind,
                commodity_code=commodity_code,
                quantity=quantity,
                reference=reference,
                by=by,
                quality=quality,
            )
        )

    def ship(
        self,
        identifier: str,
        quantity: Bushels,
        *,
        at: datetime,
        reference: str,
        by: str,
    ) -> Yard:
        contents = self.contents_of(identifier)
        if contents.is_empty:
            raise InsufficientInventory("that bin is empty", bin=identifier)
        assert contents.commodity_code is not None
        return self.post(
            Movement(
                at=at,
                bin_identifier=identifier,
                kind=MovementKind.SHIPMENT,
                commodity_code=contents.commodity_code,
                quantity=-quantity,
                reference=reference,
                by=by,
            )
        )

    def transfer(
        self,
        source: str,
        destination: str,
        quantity: Bushels,
        *,
        at: datetime,
        reference: str,
        by: str,
    ) -> Yard:
        """Move grain between two bins, posted once each way."""
        if source == destination:
            raise ValidationError("a transfer needs two different bins", bin=source)
        contents = self.contents_of(source)
        if contents.is_empty:
            raise InsufficientInventory("the source bin is empty", bin=source)
        assert contents.commodity_code is not None and contents.quality is not None
        out = Movement(
            at=at,
            bin_identifier=source,
            kind=MovementKind.TRANSFER,
            commodity_code=contents.commodity_code,
            quantity=-quantity,
            reference=reference,
            by=by,
            note=f"to {destination}",
        )
        into = Movement(
            at=at,
            bin_identifier=destination,
            kind=MovementKind.TRANSFER,
            commodity_code=contents.commodity_code,
            quantity=quantity,
            reference=reference,
            by=by,
            quality=contents.quality,
            note=f"from {source}",
        )
        return self.post(out).post(into)

    def adjust(
        self,
        identifier: str,
        quantity: Bushels,
        reason: str,
        *,
        at: datetime,
        reference: str,
        by: str,
    ) -> Yard:
        """Post a measure-up difference, up or down."""
        contents = self.contents_of(identifier)
        if contents.is_empty and quantity.is_positive():
            raise ValidationError(
                "an adjustment cannot conjure grain into an empty bin", bin=identifier
            )
        if contents.commodity_code is None:
            raise InsufficientInventory("that bin is empty", bin=identifier)
        return self.post(
            Movement(
                at=at,
                bin_identifier=identifier,
                kind=MovementKind.ADJUSTMENT,
                commodity_code=contents.commodity_code,
                quantity=quantity,
                reference=reference,
                by=by,
                quality=contents.quality if quantity.is_positive() else None,
                note=reason,
            )
        )


def _apply(structure: Bin, contents: BinContents, entry: Movement) -> BinContents:
    """Fold one movement into a bin's contents, enforcing the bin's rules."""
    if entry.quantity.is_positive():
        assert entry.quality is not None
        return contents.add(structure, entry.commodity_code, entry.quantity, entry.quality)
    taken = -entry.quantity
    if contents.is_empty:
        raise InsufficientInventory(
            "nothing to draw from that bin", bin=structure.identifier, asked=str(taken)
        )
    if contents.commodity_code != entry.commodity_code:
        raise CommodityMismatch(
            "the movement is for a crop the bin does not hold",
            bin=structure.identifier,
            holding=contents.commodity_code,
            drawing=entry.commodity_code,
        )
    remaining, _ = contents.draw(taken)
    return remaining
