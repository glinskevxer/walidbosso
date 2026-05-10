"""Shipping a load out: blend it, draw it, release it, ticket it.

Receiving is one truck at a time and shipping is not. A rail order is a hundred
and ten cars against one spec, drawn out of eight bins, over four days, and the
draw list has to stay legal as the bins come down. So the service does the
whole plan up front and then executes it car by car, re-checking the yard each
time rather than trusting a plan made on Monday to still be true on Thursday.

Releasing obligations is the part that has to be right. Grain leaving a bin
belongs to somebody, and shipping it means drawing down what the house owes.
The rule here is oldest obligation first, which is both what growers expect and
what keeps storage charges honest, since the oldest is the one accruing the
most. Company owned grain is drawn last: the house takes the loss on its own
position before it touches somebody else's bushels.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from binyard.blend.planner import BlendPlan, Objective, plan_blend
from binyard.blend.spec import BlendSpec, sources_from
from binyard.bushels import Bushels
from binyard.commodity import CropStandard
from binyard.errors import InsufficientInventory, LoadoutError, ValidationError
from binyard.identity import Sequence, TicketNumber
from binyard.loadout.carrier import Carrier, capacity_bushels
from binyard.loadout.order import LoadoutOrder, split_into_loads
from binyard.obligations.obligation import ObligationKind, Release, StorageObligation
from binyard.observability import metrics
from binyard.observability.logging import get_logger
from binyard.store.protocol import UnitOfWork
from binyard.tickets.ticket import ScaleTicket, TicketKind
from binyard.tickets.weights import ScaleWeights, TareSource
from binyard.units import Weight

__all__ = ["Shipment", "ShipmentRequest", "plan_shipment", "ship_next_load"]

_log = get_logger("binyard.shipping")


@dataclass(frozen=True, slots=True)
class ShipmentRequest:
    """One order, and the fleet available to move it."""

    order: LoadoutOrder
    spec: BlendSpec
    standard: CropStandard
    carriers: tuple[Carrier, ...]
    at: datetime
    by: str
    objective: Objective = Objective.SHIP_WORST_FIRST
    locked_bins: frozenset[str] = frozenset()
    minimum_draw: Bushels = Bushels.zero()


@dataclass(frozen=True, slots=True)
class Shipment:
    """The plan for one order: what to blend and how to split it."""

    order: LoadoutOrder
    blend: BlendPlan
    loads: tuple[tuple[str, Bushels], ...]

    @property
    def load_count(self) -> int:
        return len(self.loads)

    def describe(self) -> dict[str, object]:
        return {
            "order": self.order.describe(),
            "blend": self.blend.describe(),
            "loads": [
                {"carrier": carrier, "quantity": str(quantity)}
                for carrier, quantity in self.loads
            ],
        }


def plan_shipment(work: UnitOfWork, request: ShipmentRequest) -> Shipment:
    """Work out the blend and the split without writing anything."""
    if request.order.commodity_code != request.spec.commodity_code:
        raise ValidationError(
            "the spec is for a different crop than the order",
            order=request.order.commodity_code,
            spec=request.spec.commodity_code,
        )
    yard = work.yard.load()
    sources = sources_from(
        yard.states(),
        request.spec.commodity_code,
        minimum_draw=request.minimum_draw,
        locked=request.locked_bins,
    )
    blend = plan_blend(request.spec, sources, objective=request.objective)
    loads = split_into_loads(request.spec.quantity, list(request.carriers), request.standard)
    return Shipment(order=request.order, blend=blend, loads=loads)


def ship_next_load(
    work: UnitOfWork,
    sequence: Sequence,
    *,
    order: LoadoutOrder,
    standard: CropStandard,
    carrier: Carrier,
    quantity: Bushels,
    draws: tuple[tuple[str, Bushels], ...],
    on: date,
    at: datetime,
    by: str,
) -> tuple[LoadoutOrder, ScaleTicket]:
    """Draw one load out of the named bins and put it on the road."""
    if not quantity.is_positive():
        raise ValidationError("a load has to carry something", order=order.identifier)
    if quantity > capacity_bushels(carrier, standard):
        raise LoadoutError(
            "that load is over what the carrier may hold",
            carrier=carrier.identifier,
            quantity=str(quantity),
            capacity=str(capacity_bushels(carrier, standard)),
        )
    drawn = Bushels.zero()
    for _, amount in draws:
        drawn = drawn + amount
    if drawn != quantity:
        raise LoadoutError(
            "the draw list does not add up to the load",
            drawn=str(drawn),
            load=str(quantity),
        )

    yard = work.yard.load()
    number = _next_number(work, sequence)

    for bin_identifier, amount in draws:
        yard = yard.ship(bin_identifier, amount, at=at, reference=str(number), by=by)

    released = _release_obligations(work, order.commodity_code, quantity, on, str(number))

    weights = ScaleWeights(
        gross=carrier.empty_weight + _weight_of(quantity, standard),
        tare=carrier.empty_weight,
        tare_source=TareSource.WEIGHED,
        gross_at=at,
        tare_at=at,
    )
    ticket = ScaleTicket(
        number=number,
        kind=TicketKind.OUTBOUND,
        commodity_code=order.commodity_code,
        account=order.destination,
        weighed_on=on,
        weights=weights,
        hauler=carrier.identifier,
    )

    moved = order.add_load(carrier.identifier, quantity)
    moved = moved.ship_load(moved.loads[-1].sequence, on, str(number))

    work.tickets.add(ticket)
    work.yard.save(yard)
    work.commit()

    _log.info(
        "load shipped",
        ticket=str(number),
        order=order.identifier,
        carrier=carrier.identifier,
        quantity=str(quantity),
        bins=len(draws),
        obligations=len(released),
    )
    metrics.counter("loads_shipped_total", commodity=order.commodity_code)
    return moved, ticket


def _weight_of(quantity: Bushels, standard: CropStandard) -> Weight:
    from binyard.bushels import to_weight

    return to_weight(quantity, standard)


def _next_number(work: UnitOfWork, sequence: Sequence) -> TicketNumber:
    last = work.tickets.last_issued()
    if last is not None:
        sequence.advance_past(last)
    return sequence.next()


def _release_obligations(
    work: UnitOfWork, commodity_code: str, quantity: Bushels, on: date, reference: str
) -> tuple[StorageObligation, ...]:
    """Draw down what the house owes, oldest first and its own grain last."""
    open_obligations = [
        obligation
        for obligation in work.obligations.all_open()
        if obligation.commodity_code == commodity_code
    ]
    ordered = sorted(
        open_obligations,
        key=lambda obligation: (
            1 if obligation.kind is ObligationKind.COMPANY_OWNED else 0,
            obligation.opened_on,
            obligation.identifier,
        ),
    )
    left = quantity
    touched: list[StorageObligation] = []
    for obligation in ordered:
        if not left.is_positive():
            break
        take = min(left, obligation.balance)
        if not take.is_positive():
            continue
        if not obligation.kind.divisible and take != obligation.balance:
            continue
        moved = obligation.release(Release(on=on, quantity=take, reference=reference))
        work.obligations.replace(moved)
        touched.append(moved)
        left = left - take

    if left.is_positive():
        raise InsufficientInventory(
            "there is not enough open obligation to cover that shipment",
            commodity=commodity_code,
            short=str(left),
        )
    return tuple(touched)
