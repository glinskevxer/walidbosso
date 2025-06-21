"""Load-out orders, and turning one into a sequence of carriers.

An order is for a quantity against a destination. What actually leaves the yard
is a list of loads, each on a carrier that can legally hold it and each drawn
from bins that keep the running average inside the spec.

Splitting an order into loads is not division. The last load is short unless
the arithmetic is lucky, and a load under a floor is worse than useless because
a truck sent out a quarter full costs the same to run as a full one. So the
splitter builds full loads and then decides what to do with the tail: roll it
into the previous load if the carrier will take it, or ship it on its own if it
clears the floor, or leave it in the bin and say so.

The order tracks what has shipped against it and refuses to over ship, which is
the same tolerance idea as a purchase contract and deliberately not the same
code, because the two have different tolerances and merging them means one of
them is wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.errors import LoadoutError, ValidationError
from binyard.loadout.carrier import Carrier, capacity_bushels
from binyard.commodity import CropStandard

__all__ = ["OrderStatus", "Load", "LoadoutOrder", "split_into_loads"]


class OrderStatus(StrEnum):
    OPEN = "open"
    LOADING = "loading"
    COMPLETE = "complete"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Load:
    """One carrier's worth, planned or shipped."""

    sequence: int
    carrier_identifier: str
    quantity: Bushels
    shipped_on: date | None = None
    ticket_number: str | None = None

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValidationError("loads are numbered from one", sequence=self.sequence)
        if not self.quantity.is_positive():
            raise ValidationError("a load has to carry something", sequence=self.sequence)

    @property
    def is_shipped(self) -> bool:
        return self.shipped_on is not None

    def describe(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "carrier": self.carrier_identifier,
            "quantity": str(self.quantity),
            "shipped_on": None if self.shipped_on is None else self.shipped_on.isoformat(),
            "ticket": self.ticket_number,
        }


@dataclass(frozen=True, slots=True)
class LoadoutOrder:
    """One shipping order, and everything that has gone out against it."""

    identifier: str
    destination: str
    commodity_code: str
    quantity: Bushels
    ship_from: date
    ship_to: date
    status: OrderStatus = OrderStatus.OPEN
    #: How far over the ordered quantity a shipment may run.
    overship_tolerance: Bushels = Bushels.zero()
    loads: tuple[Load, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.identifier.strip():
            raise ValidationError("an order needs an identifier")
        if not self.destination.strip():
            raise ValidationError("an order needs a destination")
        if not self.quantity.is_positive():
            raise ValidationError("an order needs a positive quantity", order=self.identifier)
        if self.ship_to < self.ship_from:
            raise ValidationError("the shipping window ends before it starts")
        if self.overship_tolerance.hundredths < 0:
            raise ValidationError("an overship tolerance cannot be negative")
        sequences = [load.sequence for load in self.loads]
        if len(set(sequences)) != len(sequences):
            raise ValidationError("two loads share a sequence number", order=self.identifier)

    @property
    def planned(self) -> Bushels:
        total = Bushels.zero()
        for load in self.loads:
            total = total + load.quantity
        return total

    @property
    def shipped(self) -> Bushels:
        total = Bushels.zero()
        for load in self.loads:
            if load.is_shipped:
                total = total + load.quantity
        return total

    @property
    def remaining(self) -> Bushels:
        left = self.quantity - self.shipped
        return left if left.is_positive() else Bushels.zero()

    @property
    def ceiling(self) -> Bushels:
        return self.quantity + self.overship_tolerance

    @property
    def is_complete(self) -> bool:
        return self.shipped >= self.quantity

    def next_sequence(self) -> int:
        return 1 + max((load.sequence for load in self.loads), default=0)

    def add_load(self, carrier_identifier: str, quantity: Bushels) -> LoadoutOrder:
        if self.status in {OrderStatus.COMPLETE, OrderStatus.CANCELLED}:
            raise LoadoutError(
                "that order is not taking loads",
                order=self.identifier,
                status=self.status.value,
            )
        if self.planned + quantity > self.ceiling:
            raise LoadoutError(
                "that load would put the order over its tolerance",
                order=self.identifier,
                ceiling=str(self.ceiling),
                planned=str(self.planned),
                adding=str(quantity),
            )
        load = Load(
            sequence=self.next_sequence(),
            carrier_identifier=carrier_identifier,
            quantity=quantity,
        )
        return replace(
            self, loads=(*self.loads, load), status=OrderStatus.LOADING
        )

    def ship_load(self, sequence: int, on: date, ticket_number: str) -> LoadoutOrder:
        found = [load for load in self.loads if load.sequence == sequence]
        if not found:
            raise LoadoutError(
                "no such load on this order", order=self.identifier, sequence=sequence
            )
        if found[0].is_shipped:
            raise LoadoutError(
                "that load has already gone", order=self.identifier, sequence=sequence
            )
        shipped = replace(found[0], shipped_on=on, ticket_number=ticket_number)
        loads = tuple(shipped if load.sequence == sequence else load for load in self.loads)
        moved = replace(self, loads=loads)
        if moved.is_complete:
            return replace(moved, status=OrderStatus.COMPLETE)
        return moved

    def cancelled(self) -> LoadoutOrder:
        if any(load.is_shipped for load in self.loads):
            raise LoadoutError(
                "an order with shipments cannot be cancelled", order=self.identifier
            )
        return replace(self, status=OrderStatus.CANCELLED, loads=())

    def describe(self) -> dict[str, object]:
        return {
            "identifier": self.identifier,
            "destination": self.destination,
            "commodity": self.commodity_code,
            "quantity": str(self.quantity),
            "planned": str(self.planned),
            "shipped": str(self.shipped),
            "remaining": str(self.remaining),
            "status": self.status.value,
            "loads": [load.describe() for load in self.loads],
        }


def split_into_loads(
    quantity: Bushels,
    carriers: list[Carrier],
    standard: CropStandard,
    *,
    minimum_load: Bushels = Bushels.zero(),
) -> tuple[tuple[str, Bushels], ...]:
    """Fill carriers in turn until the quantity is gone or they run out.

    Carriers are used in the order given. The tail is rolled back into the
    previous load when that load has room, which is the ordinary case for a
    fleet of identical trucks and saves a run.
    """
    if not quantity.is_positive():
        raise ValidationError("nothing to split", quantity=str(quantity))
    if not carriers:
        raise LoadoutError("no carrier was offered")

    planned: list[tuple[str, Bushels]] = []
    left = quantity
    for carrier in carriers:
        if not left.is_positive():
            break
        capacity = capacity_bushels(carrier, standard)
        if not capacity.is_positive():
            continue
        take = min(left, capacity)
        planned.append((carrier.identifier, take))
        left = left - take

    if left.is_positive():
        raise LoadoutError(
            "the carriers offered cannot hold the whole quantity",
            wanted=str(quantity),
            short=str(left),
        )

    if len(planned) > 1 and planned[-1][1] < minimum_load:
        tail_identifier, tail = planned[-1]
        previous_identifier, previous = planned[-2]
        room = capacity_bushels(
            next(c for c in carriers if c.identifier == previous_identifier), standard
        ) - previous
        if room >= tail:
            planned = planned[:-1]
            planned[-1] = (previous_identifier, previous + tail)
        else:
            raise LoadoutError(
                "the tail load is under the minimum and will not roll back",
                tail=str(tail),
                minimum=str(minimum_load),
                carrier=tail_identifier,
            )
    return tuple(planned)
