"""What the domain needs from storage, said as narrowly as possible.

These protocols are written from the caller's side. A service that settles a
ticket needs to find one ticket by number and put it back changed; it does not
need a query language, and giving it one means every future storage backend has
to implement one.

Two decisions worth stating, because both are the kind that get reversed by
accident later:

**The yard is loaded and saved whole.** It is one aggregate: the bins and the
ledger have to agree with each other, and a partial save that wrote a movement
without its capacity check is exactly the corruption the aggregate exists to
prevent. The ledger is append only, so saving it whole is cheap in practice
because a backend can write only the entries it has not seen.

**Nothing here returns ``None`` for a missing record.** A lookup by identity
either finds it or raises :class:`~binyard.errors.NotFound`. Callers that want
to test for existence say so with ``exists``, which reads better at the call
site than a comparison against ``None`` and cannot be forgotten.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from binyard.contracts.contract import PurchaseContract
from binyard.identity import TicketNumber
from binyard.inventory.yard import Yard
from binyard.obligations.obligation import StorageObligation
from binyard.settlement.statement import Settlement
from binyard.tickets.ticket import ScaleTicket

__all__ = [
    "TicketStore",
    "ObligationStore",
    "ContractStore",
    "SettlementStore",
    "YardStore",
    "UnitOfWork",
]


@runtime_checkable
class TicketStore(Protocol):
    """Scale tickets, addressed by their printed number."""

    def add(self, ticket: ScaleTicket) -> None:
        """Store a ticket that does not exist yet. Raises on a duplicate number."""

    def get(self, number: TicketNumber) -> ScaleTicket:
        """Fetch by number, raising :class:`NotFound` when it is not there."""

    def exists(self, number: TicketNumber) -> bool: ...

    def replace(self, ticket: ScaleTicket) -> None:
        """Write back a ticket that moved state. Raises if it is not there."""

    def for_account(self, account: str) -> tuple[ScaleTicket, ...]: ...

    def weighed_on(self, day: date) -> tuple[ScaleTicket, ...]: ...

    def open_tickets(self) -> tuple[ScaleTicket, ...]:
        """Everything not yet settled or voided, which is the work queue."""

    def last_issued(self) -> TicketNumber | None:
        """The highest number used, so a restarted sequence does not collide."""


@runtime_checkable
class ObligationStore(Protocol):
    """Storage obligations, addressed by identifier."""

    def add(self, obligation: StorageObligation) -> None: ...

    def get(self, identifier: str) -> StorageObligation: ...

    def exists(self, identifier: str) -> bool: ...

    def replace(self, obligation: StorageObligation) -> None: ...

    def open_for(self, account: str, commodity_code: str) -> tuple[StorageObligation, ...]:
        """Open obligations for one grower and crop, oldest first."""

    def all_open(self) -> tuple[StorageObligation, ...]: ...

    def as_of(self, day: date) -> tuple[StorageObligation, ...]:
        """Everything that existed on that day, for a position report."""


@runtime_checkable
class ContractStore(Protocol):
    """Purchase contracts, addressed by identifier."""

    def add(self, contract: PurchaseContract) -> None: ...

    def get(self, identifier: str) -> PurchaseContract: ...

    def exists(self, identifier: str) -> bool: ...

    def replace(self, contract: PurchaseContract) -> None: ...

    def open_for(
        self, account: str, commodity_code: str, on: date
    ) -> tuple[PurchaseContract, ...]:
        """Contracts a load delivered on ``on`` could land against."""


@runtime_checkable
class SettlementStore(Protocol):
    """Finished statements. Written once and never changed."""

    def add(self, settlement: Settlement) -> None: ...

    def get(self, number: str) -> Settlement: ...

    def for_account(self, account: str) -> tuple[Settlement, ...]: ...


@runtime_checkable
class YardStore(Protocol):
    """The yard, loaded and saved as one aggregate."""

    def load(self) -> Yard: ...

    def save(self, yard: Yard) -> None: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """One transaction, holding every store.

    A service takes a unit of work, does its reads and writes, and commits at
    the end. Leaving the context manager without committing rolls back, which
    means a service that raises halfway through a two obligation transfer
    cannot leave one side of it written.
    """

    tickets: TicketStore
    obligations: ObligationStore
    contracts: ContractStore
    settlements: SettlementStore
    yard: YardStore

    def __enter__(self) -> UnitOfWork: ...

    def __exit__(self, *exception) -> None: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...
