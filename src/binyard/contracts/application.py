"""Deciding which contract a load lands against.

A grower pulls up with 860 bushels of corn. They have three open contracts and
an open storage account. Somebody has to say where the bushels go, and that
decision is worth real money because the contracts carry different prices and
different deadlines.

The rule this house runs is **earliest deadline first, among contracts that
actually cover today**. It is not the only defensible rule. Filling the highest
priced contract first pays the grower more today and leaves the cheap contract
to be filled in the spring when they may be short. Filling the earliest
deadline first keeps them out of default. Both are implemented, the desk picks,
and the plan says which one it used so the choice is on the settlement rather
than in somebody's head.

A load is never split across contracts unless it has to be. Splitting is how a
single ticket ends up on two settlements with two different prices, which is
correct and is also the thing growers phone about, so the plan records the
split explicitly and the stub prints both lines.

**Nothing here writes anything.** The plan is a proposal. Applying it is the
service layer's job, and it can be shown to a human first.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from binyard.bushels import Bushels
from binyard.contracts.contract import ContractStatus, PurchaseContract
from binyard.errors import ValidationError

__all__ = ["Priority", "Allocation", "ApplicationPlan", "plan_application"]


class Priority(StrEnum):
    """The order eligible contracts are filled in."""

    #: Whichever contract runs out of delivery window first.
    EARLIEST_DEADLINE = "earliest_deadline"
    #: Whichever contract pays most per bushel. Unpriced contracts go last.
    BEST_PRICE = "best_price"
    #: The order the desk listed them in.
    AS_LISTED = "as_listed"


@dataclass(frozen=True, slots=True)
class Allocation:
    """One slice of a load, and where it went."""

    quantity: Bushels
    contract_identifier: str | None
    reason: str

    @property
    def is_storage(self) -> bool:
        return self.contract_identifier is None

    def describe(self) -> dict[str, object]:
        return {
            "quantity": str(self.quantity),
            "contract": self.contract_identifier,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ApplicationPlan:
    """Where a load would go, if the desk agrees."""

    ticket_number: str
    commodity_code: str
    account: str
    on: date
    quantity: Bushels
    priority: Priority
    allocations: tuple[Allocation, ...]

    @property
    def to_storage(self) -> Bushels:
        total = Bushels.zero()
        for allocation in self.allocations:
            if allocation.is_storage:
                total = total + allocation.quantity
        return total

    @property
    def to_contracts(self) -> Bushels:
        return self.quantity - self.to_storage

    @property
    def is_split(self) -> bool:
        return len(self.allocations) > 1

    def describe(self) -> dict[str, object]:
        return {
            "ticket": self.ticket_number,
            "commodity": self.commodity_code,
            "account": self.account,
            "on": self.on.isoformat(),
            "quantity": str(self.quantity),
            "priority": self.priority.value,
            "to_contracts": str(self.to_contracts),
            "to_storage": str(self.to_storage),
            "allocations": [a.describe() for a in self.allocations],
        }


def eligible(
    contracts: list[PurchaseContract], account: str, commodity_code: str, on: date
) -> list[PurchaseContract]:
    """The contracts a load delivered today could actually land against."""
    return [
        contract
        for contract in contracts
        if contract.account == account
        and contract.commodity_code == commodity_code
        and contract.status in {ContractStatus.OPEN, ContractStatus.FILLED}
        and contract.covers(on)
        and contract.remaining.is_positive()
    ]


def _ordered(
    contracts: list[PurchaseContract], priority: Priority
) -> list[PurchaseContract]:
    if priority is Priority.AS_LISTED:
        return list(contracts)
    if priority is Priority.EARLIEST_DEADLINE:
        return sorted(contracts, key=lambda c: (c.delivery_to, c.identifier))
    ranked = sorted(
        contracts,
        key=lambda c: (
            0 if c.is_priced else 1,
            -(c.price().ticks if c.price() is not None else 0),
            c.identifier,
        ),
    )
    return ranked


def plan_application(
    *,
    ticket_number: str,
    account: str,
    commodity_code: str,
    quantity: Bushels,
    on: date,
    contracts: list[PurchaseContract],
    priority: Priority = Priority.EARLIEST_DEADLINE,
) -> ApplicationPlan:
    """Work out where one graded load should go, without writing anything."""
    if not quantity.is_positive():
        raise ValidationError(
            "cannot apply a non positive quantity", ticket=ticket_number
        )

    candidates = _ordered(eligible(contracts, account, commodity_code, on), priority)
    allocations: list[Allocation] = []
    left = quantity

    for contract in candidates:
        if not left.is_positive():
            break
        take = min(left, contract.remaining)
        if not take.is_positive():
            continue
        allocations.append(
            Allocation(
                quantity=take,
                contract_identifier=contract.identifier,
                reason=_reason(contract, priority),
            )
        )
        left = left - take

    if left.is_positive():
        allocations.append(
            Allocation(
                quantity=left,
                contract_identifier=None,
                reason=(
                    "no open contract covers this delivery"
                    if not candidates
                    else "every covering contract is full"
                ),
            )
        )

    return ApplicationPlan(
        ticket_number=ticket_number,
        commodity_code=commodity_code,
        account=account,
        on=on,
        quantity=quantity,
        priority=priority,
        allocations=tuple(allocations),
    )


def _reason(contract: PurchaseContract, priority: Priority) -> str:
    if priority is Priority.EARLIEST_DEADLINE:
        return f"window closes {contract.delivery_to.isoformat()}"
    if priority is Priority.BEST_PRICE:
        settled = contract.price()
        return "unpriced" if settled is None else f"pays {settled}"
    return "listed order"
