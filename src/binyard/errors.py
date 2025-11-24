"""The exception hierarchy.

Every error the package raises on purpose descends from :class:`BinyardError`,
so a caller that wants to be told "the elevator refused this" without catching
``ValueError`` from arbitrary library code can do it in one clause.

Errors carry structured detail rather than only a sentence. The HTTP layer
turns ``detail`` into a JSON body and the CLI prints it under the message, and
neither of them has to parse prose to do it.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BinCapacityExceeded",
    "BinyardError",
    "BlendError",
    "CommodityMismatch",
    "ConfigurationError",
    "Conflict",
    "FactorOutOfRange",
    "GradingError",
    "IncompatibleUnits",
    "InfeasibleBlend",
    "InsufficientInventory",
    "InventoryError",
    "LoadoutError",
    "NoScheduleInEffect",
    "NotFound",
    "ObligationError",
    "ScheduleError",
    "TicketError",
    "TicketStateError",
    "UnitError",
    "UnknownCommodity",
    "ValidationError",
]


class BinyardError(Exception):
    """Base class for everything this package raises deliberately."""

    #: Short stable token used by the API layer as the machine readable code.
    code = "binyard_error"

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail

    def __str__(self) -> str:
        if not self.detail:
            return self.message
        parts = ", ".join(f"{k}={v!r}" for k, v in sorted(self.detail.items()))
        return f"{self.message} ({parts})"

    def as_dict(self) -> dict[str, Any]:
        """Render the error the way the API and the CLI both want it."""
        return {"code": self.code, "message": self.message, "detail": dict(self.detail)}


class ConfigurationError(BinyardError):
    """The elevator is set up in a way the package cannot work with."""

    code = "configuration_error"


class ValidationError(BinyardError):
    """A value handed in does not satisfy a rule stated by the domain."""

    code = "validation_error"


class UnitError(BinyardError):
    """Something went wrong converting or comparing measured quantities."""

    code = "unit_error"


class IncompatibleUnits(UnitError):
    """Two quantities cannot be combined because they measure different things."""

    code = "incompatible_units"


class UnknownCommodity(BinyardError):
    """The elevator does not handle the crop that was named."""

    code = "unknown_commodity"


class GradingError(BinyardError):
    """A sample could not be graded."""

    code = "grading_error"


class FactorOutOfRange(GradingError):
    """A measured factor is outside anything the standards describe."""

    code = "factor_out_of_range"


class ScheduleError(BinyardError):
    """A posted discount schedule is malformed or unusable."""

    code = "schedule_error"


class NoScheduleInEffect(ScheduleError):
    """Nothing was posted for that commodity on that day."""

    code = "no_schedule_in_effect"


class TicketError(BinyardError):
    """A scale ticket cannot be built or changed the way it was asked."""

    code = "ticket_error"


class TicketStateError(TicketError):
    """The ticket is in a state that forbids the transition."""

    code = "ticket_state_error"


class InventoryError(BinyardError):
    """A bin cannot do what was asked of it."""

    code = "inventory_error"


class BinCapacityExceeded(InventoryError):
    """The fill would put more in the bin than it holds."""

    code = "bin_capacity_exceeded"


class CommodityMismatch(InventoryError):
    """The bin already holds something else."""

    code = "commodity_mismatch"


class InsufficientInventory(InventoryError):
    """There are not enough bushels in the bin to draw what was asked."""

    code = "insufficient_inventory"


class ObligationError(BinyardError):
    """A storage obligation cannot be created, filled or released."""

    code = "obligation_error"


class BlendError(BinyardError):
    """A blend could not be planned."""

    code = "blend_error"


class InfeasibleBlend(BlendError):
    """No combination of the offered bins satisfies the spec."""

    code = "infeasible_blend"


class LoadoutError(BinyardError):
    """A load could not be built, drawn or reconciled."""

    code = "loadout_error"


class NotFound(BinyardError):
    """A record was asked for by identity and does not exist."""

    code = "not_found"

    def __init__(self, kind: str, identity: Any) -> None:
        super().__init__(f"no {kind} with identity {identity!r}", kind=kind, identity=identity)


class Conflict(BinyardError):
    """A write lost a race, or would break a uniqueness rule."""

    code = "conflict"
