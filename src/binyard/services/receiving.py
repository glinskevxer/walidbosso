"""Taking a load in: weigh, grade, shrink, bin it, apply it.

This is the busiest path in the building and the one with the most ways to be
half done. A truck can be weighed and then refused on grade. A load can be
graded and binned and then found to be on the wrong account. So every step here
happens inside one unit of work and commits once at the end, and the ticket,
the bin movement and the contract application either all land or none do.

The order is fixed and it is not arbitrary:

1. Weigh. The ticket exists from this point and has a number.
2. Grade. This is where the load can be refused outright, before any grain has
   gone anywhere.
3. Shrink. Net bushels come out of the crop's shrink policy.
4. Bin. The yard checks capacity and crop compatibility, and this can fail even
   though everything above passed, which is why nothing is committed yet.
5. Apply. Contracts first, then whatever is left goes to storage.

Grading before binning matters. A load that would be rejected on moisture has
to be turned away at the pit, and a house that bins first and grades later
finds out it has put 26% corn on top of a bin of 14% and has no way to get it
back out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from binyard.bushels import Bushels
from binyard.commodity import CropStandard
from binyard.contracts.application import ApplicationPlan, Priority, plan_application
from binyard.contracts.contract import Delivery
from binyard.discounts.apply import Assessment, assess
from binyard.discounts.schedule import ScheduleBook
from binyard.errors import TicketError, ValidationError
from binyard.grading.factors import FactorSample
from binyard.identity import Sequence, TicketNumber
from binyard.obligations.obligation import ObligationKind, StorageObligation
from binyard.shrink import ShrinkPolicy
from binyard.store.protocol import UnitOfWork
from binyard.tickets.ticket import ScaleTicket, TicketKind
from binyard.tickets.weights import ScaleWeights

__all__ = ["Receipt", "ReceiptRequest", "receive_load"]


@dataclass(frozen=True, slots=True)
class ReceiptRequest:
    """Everything the scale house knows when the truck pulls off."""

    account: str
    standard: CropStandard
    weights: ScaleWeights
    sample: FactorSample
    bin_identifier: str
    weighed_on: date
    at: datetime
    by: str
    hauler: str | None = None
    priority: Priority = Priority.EARLIEST_DEADLINE
    storage_kind: ObligationKind = ObligationKind.OPEN_STORAGE
    obligation_identifier: str | None = None


@dataclass(frozen=True, slots=True)
class Receipt:
    """What came of taking the load."""

    ticket: ScaleTicket
    assessment: Assessment
    application: ApplicationPlan
    obligation: StorageObligation | None
    contracts_filled: tuple[str, ...]

    @property
    def net_bushels(self) -> Bushels:
        net = self.ticket.net_bushels
        assert net is not None
        return net

    def describe(self) -> dict[str, object]:
        return {
            "ticket": self.ticket.describe(),
            "discounts": self.assessment.describe(),
            "application": self.application.describe(),
            "obligation": None if self.obligation is None else self.obligation.describe(),
            "contracts_filled": list(self.contracts_filled),
        }


def receive_load(
    work: UnitOfWork,
    sequence: Sequence,
    request: ReceiptRequest,
    *,
    book: ScheduleBook,
    policy: ShrinkPolicy | None = None,
) -> Receipt:
    """Run one load all the way from the scale to the bin, or none of the way."""
    standard = request.standard
    shrink_policy = policy or ShrinkPolicy.market_default(standard)

    if request.sample.moisture > standard.receiving_moisture_ceiling:
        raise TicketError(
            "that load is too wet to take without drying",
            moisture=str(request.sample.moisture),
            ceiling=str(standard.receiving_moisture_ceiling),
        )

    number = _next_number(work, sequence)
    ticket = ScaleTicket(
        number=number,
        kind=TicketKind.INBOUND,
        commodity_code=standard.code,
        account=request.account,
        weighed_on=request.weighed_on,
        weights=request.weights,
        hauler=request.hauler,
    )
    ticket = ticket.graded_as(
        request.sample, standard, shrink_policy, at=request.at, by=request.by
    )

    schedule = book.in_effect(standard.code, request.weighed_on)
    assert ticket.grade is not None
    assessment = assess(ticket.grade, ticket.net_bushels or Bushels.zero(), schedule)
    if assessment.rejected:
        raise TicketError(
            "the board rejects that load outright",
            factor=(
                assessment.rejecting_factor.value if assessment.rejecting_factor else "unknown"
            ),
            number=str(number),
        )

    net = ticket.net_bushels
    assert net is not None

    yard = work.yard.load()
    yard = yard.receive(
        request.bin_identifier,
        standard.code,
        net,
        ticket.grade.graded_sample,
        at=request.at,
        reference=str(number),
        by=request.by,
    )

    contracts = list(
        work.contracts.open_for(request.account, standard.code, request.weighed_on)
    )
    plan = plan_application(
        ticket_number=str(number),
        account=request.account,
        commodity_code=standard.code,
        quantity=net,
        on=request.weighed_on,
        contracts=contracts,
        priority=request.priority,
    )

    filled: list[str] = []
    by_identifier = {contract.identifier: contract for contract in contracts}
    for allocation in plan.allocations:
        if allocation.contract_identifier is None:
            continue
        contract = by_identifier[allocation.contract_identifier]
        moved = contract.deliver(
            Delivery(
                on=request.weighed_on,
                ticket_number=str(number),
                quantity=allocation.quantity,
            )
        )
        work.contracts.replace(moved)
        filled.append(moved.identifier)

    obligation: StorageObligation | None = None
    if plan.to_storage.is_positive():
        obligation = _store_the_rest(work, request, standard, plan.to_storage, number)

    ticket = ticket.applied(at=request.at, by=request.by, note=f"{len(filled)} contract lines")
    work.tickets.add(ticket)
    work.yard.save(yard)
    work.commit()

    return Receipt(
        ticket=ticket,
        assessment=assessment,
        application=plan,
        obligation=obligation,
        contracts_filled=tuple(filled),
    )


def _next_number(work: UnitOfWork, sequence: Sequence) -> TicketNumber:
    last = work.tickets.last_issued()
    if last is not None:
        sequence.advance_past(last)
    number = sequence.next()
    while work.tickets.exists(number):  # pragma: no cover - only after a manual insert
        number = sequence.next()
    return number


def _store_the_rest(
    work: UnitOfWork,
    request: ReceiptRequest,
    standard: CropStandard,
    quantity: Bushels,
    number: TicketNumber,
) -> StorageObligation:
    """Open or top up the storage obligation the leftovers go into."""
    if request.obligation_identifier is not None:
        existing = work.obligations.get(request.obligation_identifier)
        if existing.account != request.account:
            raise ValidationError(
                "that obligation belongs to another account",
                obligation=existing.identifier,
            )
        if existing.commodity_code != standard.code:
            raise ValidationError(
                "that obligation is for another crop", obligation=existing.identifier
            )
        topped = StorageObligation(
            identifier=existing.identifier,
            account=existing.account,
            commodity_code=existing.commodity_code,
            kind=existing.kind,
            quantity=existing.quantity + quantity,
            opened_on=existing.opened_on,
            free_days=existing.free_days,
            storage_rate=existing.storage_rate,
            priced_at=existing.priced_at,
            priced_on=existing.priced_on,
            status=existing.status,
            releases=existing.releases,
        )
        work.obligations.replace(topped)
        return topped

    identifier = f"{request.storage_kind.value[:2].upper()}-{number}"
    opened = StorageObligation(
        identifier=identifier,
        account=request.account,
        commodity_code=standard.code,
        kind=request.storage_kind,
        quantity=quantity,
        opened_on=request.weighed_on,
    )
    work.obligations.add(opened)
    return opened
