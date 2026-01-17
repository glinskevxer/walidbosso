"""An in-memory unit of work, with real transaction semantics.

This is not a test double that ignores ``commit``. It snapshots on entry and
restores the snapshot on rollback, because a fake that always succeeds is how a
service ships with a missing commit and nobody finds out until the SQL backend
goes in.

Snapshotting is cheap here for the reason it is cheap everywhere in this
package: every domain object is frozen. A snapshot is a shallow copy of four
dictionaries, and restoring one cannot leave a half mutated aggregate behind
because there is nothing mutable to half mutate.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from binyard.contracts.contract import ContractStatus, PurchaseContract
from binyard.errors import Conflict, NotFound
from binyard.identity import TicketNumber
from binyard.inventory.yard import Yard
from binyard.obligations.obligation import ObligationStatus, StorageObligation
from binyard.settlement.statement import Settlement
from binyard.store.drying import MemoryDryerStore
from binyard.store.feed import MemoryFeedLog
from binyard.store.orders import MemoryOrderStore
from binyard.tickets.ticket import ScaleTicket, TicketStatus

__all__ = [
    "MemoryContractStore",
    "MemoryObligationStore",
    "MemorySettlementStore",
    "MemoryTicketStore",
    "MemoryUnitOfWork",
    "MemoryYardStore",
]


class MemoryTicketStore:
    def __init__(self, tickets: dict[str, ScaleTicket] | None = None) -> None:
        self._tickets: dict[str, ScaleTicket] = dict(tickets or {})

    def add(self, ticket: ScaleTicket) -> None:
        key = str(ticket.number)
        if key in self._tickets:
            raise Conflict("that ticket number is already used", number=key)
        self._tickets[key] = ticket

    def get(self, number: TicketNumber) -> ScaleTicket:
        try:
            return self._tickets[str(number)]
        except KeyError:
            raise NotFound("ticket", str(number)) from None

    def exists(self, number: TicketNumber) -> bool:
        return str(number) in self._tickets

    def replace(self, ticket: ScaleTicket) -> None:
        key = str(ticket.number)
        if key not in self._tickets:
            raise NotFound("ticket", key)
        self._tickets[key] = ticket

    def for_account(self, account: str) -> tuple[ScaleTicket, ...]:
        return tuple(ticket for ticket in self._sorted() if ticket.account == account)

    def weighed_on(self, day: date) -> tuple[ScaleTicket, ...]:
        return tuple(ticket for ticket in self._sorted() if ticket.weighed_on == day)

    def open_tickets(self) -> tuple[ScaleTicket, ...]:
        return tuple(
            ticket
            for ticket in self._sorted()
            if ticket.status not in {TicketStatus.SETTLED, TicketStatus.VOID}
        )

    def last_issued(self) -> TicketNumber | None:
        numbers = [ticket.number for ticket in self._tickets.values()]
        return max(numbers) if numbers else None

    def _sorted(self) -> list[ScaleTicket]:
        return sorted(self._tickets.values(), key=lambda ticket: ticket.number)

    def snapshot(self) -> dict[str, ScaleTicket]:
        return dict(self._tickets)

    def restore(self, snapshot: dict[str, ScaleTicket]) -> None:
        self._tickets = dict(snapshot)


class MemoryObligationStore:
    def __init__(self, obligations: dict[str, StorageObligation] | None = None) -> None:
        self._obligations: dict[str, StorageObligation] = dict(obligations or {})

    def add(self, obligation: StorageObligation) -> None:
        if obligation.identifier in self._obligations:
            raise Conflict(
                "that obligation identifier is already used",
                identifier=obligation.identifier,
            )
        self._obligations[obligation.identifier] = obligation

    def get(self, identifier: str) -> StorageObligation:
        try:
            return self._obligations[identifier]
        except KeyError:
            raise NotFound("obligation", identifier) from None

    def exists(self, identifier: str) -> bool:
        return identifier in self._obligations

    def replace(self, obligation: StorageObligation) -> None:
        if obligation.identifier not in self._obligations:
            raise NotFound("obligation", obligation.identifier)
        self._obligations[obligation.identifier] = obligation

    def open_for(self, account: str, commodity_code: str) -> tuple[StorageObligation, ...]:
        return tuple(
            obligation
            for obligation in self._sorted()
            if obligation.account == account
            and obligation.commodity_code == commodity_code
            and obligation.status is ObligationStatus.OPEN
        )

    def all_open(self) -> tuple[StorageObligation, ...]:
        return tuple(
            obligation
            for obligation in self._sorted()
            if obligation.status is ObligationStatus.OPEN
        )

    def as_of(self, day: date) -> tuple[StorageObligation, ...]:
        return tuple(obligation for obligation in self._sorted() if obligation.opened_on <= day)

    def _sorted(self) -> list[StorageObligation]:
        return sorted(
            self._obligations.values(),
            key=lambda obligation: (obligation.opened_on, obligation.identifier),
        )

    def snapshot(self) -> dict[str, StorageObligation]:
        return dict(self._obligations)

    def restore(self, snapshot: dict[str, StorageObligation]) -> None:
        self._obligations = dict(snapshot)


class MemoryContractStore:
    def __init__(self, contracts: dict[str, PurchaseContract] | None = None) -> None:
        self._contracts: dict[str, PurchaseContract] = dict(contracts or {})

    def add(self, contract: PurchaseContract) -> None:
        if contract.identifier in self._contracts:
            raise Conflict(
                "that contract identifier is already used", identifier=contract.identifier
            )
        self._contracts[contract.identifier] = contract

    def get(self, identifier: str) -> PurchaseContract:
        try:
            return self._contracts[identifier]
        except KeyError:
            raise NotFound("contract", identifier) from None

    def exists(self, identifier: str) -> bool:
        return identifier in self._contracts

    def replace(self, contract: PurchaseContract) -> None:
        if contract.identifier not in self._contracts:
            raise NotFound("contract", contract.identifier)
        self._contracts[contract.identifier] = contract

    def open_for(
        self, account: str, commodity_code: str, on: date
    ) -> tuple[PurchaseContract, ...]:
        return tuple(
            contract
            for contract in sorted(self._contracts.values(), key=lambda c: c.identifier)
            if contract.account == account
            and contract.commodity_code == commodity_code
            and contract.status in {ContractStatus.OPEN, ContractStatus.FILLED}
            and contract.covers(on)
        )

    def snapshot(self) -> dict[str, PurchaseContract]:
        return dict(self._contracts)

    def restore(self, snapshot: dict[str, PurchaseContract]) -> None:
        self._contracts = dict(snapshot)


class MemorySettlementStore:
    def __init__(self, settlements: dict[str, Settlement] | None = None) -> None:
        self._settlements: dict[str, Settlement] = dict(settlements or {})

    def add(self, settlement: Settlement) -> None:
        if settlement.number in self._settlements:
            raise Conflict("that settlement number is already used", number=settlement.number)
        self._settlements[settlement.number] = settlement

    def get(self, number: str) -> Settlement:
        try:
            return self._settlements[number]
        except KeyError:
            raise NotFound("settlement", number) from None

    def for_account(self, account: str) -> tuple[Settlement, ...]:
        return tuple(
            settlement
            for settlement in sorted(self._settlements.values(), key=lambda s: s.number)
            if settlement.account == account
        )

    def snapshot(self) -> dict[str, Settlement]:
        return dict(self._settlements)

    def restore(self, snapshot: dict[str, Settlement]) -> None:
        self._settlements = dict(snapshot)


class MemoryYardStore:
    def __init__(self, yard: Yard | None = None) -> None:
        self._yard = yard if yard is not None else Yard()

    def load(self) -> Yard:
        return self._yard

    def save(self, yard: Yard) -> None:
        self._yard = yard

    def snapshot(self) -> Yard:
        return self._yard

    def restore(self, snapshot: Yard) -> None:
        self._yard = snapshot


class MemoryUnitOfWork:
    """Holds every store and gives them one transaction between them."""

    def __init__(self, yard: Yard | None = None) -> None:
        self.tickets = MemoryTicketStore()
        self.obligations = MemoryObligationStore()
        self.contracts = MemoryContractStore()
        self.settlements = MemorySettlementStore()
        self.orders = MemoryOrderStore()
        self.dryer = MemoryDryerStore()
        self.feed = MemoryFeedLog()
        self.yard = MemoryYardStore(yard)
        self.committed = 0
        self._snapshot: dict[str, Any] = self._take()

    def _stores(self) -> dict[str, Any]:
        return {
            "tickets": self.tickets,
            "obligations": self.obligations,
            "contracts": self.contracts,
            "settlements": self.settlements,
            "orders": self.orders,
            "dryer": self.dryer,
            "feed": self.feed,
            "yard": self.yard,
        }

    def _take(self) -> dict[str, Any]:
        return {name: store.snapshot() for name, store in self._stores().items()}

    def __enter__(self) -> MemoryUnitOfWork:
        return self

    def __exit__(self, *exception: Any) -> None:
        self.rollback()

    def commit(self) -> None:
        """Make the current state the one a later rollback returns to."""
        self._snapshot = self._take()
        self.committed += 1

    def rollback(self) -> None:
        """Throw away everything written since the last commit.

        The snapshot is taken at construction and refreshed on every commit,
        not on entering a context manager. A service that rolls back without
        ever having used ``with`` still gets its writes undone, which is what
        the SQL backend does and therefore what the protocol has to mean.
        """
        for name, store in self._stores().items():
            store.restore(self._snapshot[name])
