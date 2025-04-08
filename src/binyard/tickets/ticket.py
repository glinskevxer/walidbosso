"""The scale ticket itself, and the states it is allowed to move between.

A ticket starts when a truck crosses the inbound scale and it never gets
deleted. It gets graded, applied against an obligation, settled, and sometimes
voided or corrected, and every one of those is a transition with a rule about
what may follow. The state machine is small on purpose, and it is here rather
than in the service layer so that a ticket restored out of the database cannot
be moved somewhere the domain forbids.

Tickets are immutable. A transition returns a new ticket carrying the same
number and a longer history. Nothing in this module mutates in place, which
means a caller holding an old reference sees the old state and cannot be
surprised by a change made three frames up the stack.

**Corrections are not edits.** A wrong ticket is voided and a new one issued
that points back at it. The pair stays in the record, the audit trail shows
the reason, and the position report can net them. A house that edits tickets in
place cannot answer a state warehouse examiner asking what the bin held on a
Tuesday in November.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import StrEnum

from binyard.bushels import Bushels, from_weight
from binyard.commodity import CropStandard
from binyard.errors import TicketStateError, ValidationError
from binyard.grading.factors import FactorSample
from binyard.grading.grade import GradeResult, determine
from binyard.identity import TicketNumber
from binyard.shrink import ShrinkPolicy, ShrinkResult, apply_shrink
from binyard.tickets.weights import ScaleWeights

__all__ = ["TicketKind", "TicketStatus", "TicketEvent", "ScaleTicket"]


class TicketKind(StrEnum):
    """Which way the grain was going."""

    INBOUND = "inbound"
    OUTBOUND = "outbound"
    TRANSFER = "transfer"


class TicketStatus(StrEnum):
    """Where the ticket is in its life."""

    WEIGHED = "weighed"
    GRADED = "graded"
    APPLIED = "applied"
    SETTLED = "settled"
    VOID = "void"


_ALLOWED: dict[TicketStatus, frozenset[TicketStatus]] = {
    TicketStatus.WEIGHED: frozenset({TicketStatus.GRADED, TicketStatus.VOID}),
    TicketStatus.GRADED: frozenset({TicketStatus.APPLIED, TicketStatus.VOID}),
    TicketStatus.APPLIED: frozenset({TicketStatus.SETTLED, TicketStatus.VOID}),
    TicketStatus.SETTLED: frozenset(),
    TicketStatus.VOID: frozenset(),
}


@dataclass(frozen=True, slots=True)
class TicketEvent:
    """One line of the audit trail."""

    at: datetime
    became: TicketStatus
    by: str
    note: str = ""

    def describe(self) -> dict[str, str]:
        return {
            "at": self.at.isoformat(),
            "became": self.became.value,
            "by": self.by,
            "note": self.note,
        }


@dataclass(frozen=True, slots=True)
class ScaleTicket:
    """One truck, one crossing, one number."""

    number: TicketNumber
    kind: TicketKind
    commodity_code: str
    account: str
    weighed_on: date
    weights: ScaleWeights
    status: TicketStatus = TicketStatus.WEIGHED
    hauler: str | None = None
    sample: FactorSample | None = None
    grade: GradeResult | None = None
    shrink: ShrinkResult | None = None
    voids: TicketNumber | None = None
    void_reason: str | None = None
    history: tuple[TicketEvent, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.account.strip():
            raise ValidationError("a ticket needs an account", number=str(self.number))
        if self.grade is not None and self.grade.commodity_code != self.commodity_code:
            raise ValidationError(
                "the grade on this ticket is for a different crop",
                ticket=self.commodity_code,
                graded=self.grade.commodity_code,
            )
        if self.status is TicketStatus.VOID and not self.void_reason:
            raise ValidationError("a void ticket needs a reason", number=str(self.number))

    # -- reading ---------------------------------------------------------

    @property
    def gross_bushels(self) -> Bushels | None:
        """Bushels on the scale, before any shrink. Needs a crop standard to know."""
        return None if self.shrink is None else self.shrink.gross

    @property
    def net_bushels(self) -> Bushels | None:
        """Bushels that go into a bin, after shrink. ``None`` until graded."""
        return None if self.shrink is None else self.shrink.net

    @property
    def is_open(self) -> bool:
        return self.status not in {TicketStatus.SETTLED, TicketStatus.VOID}

    @property
    def needs_review(self) -> bool:
        return self.weights.needs_review

    def can_become(self, status: TicketStatus) -> bool:
        return status in _ALLOWED[self.status]

    # -- transitions -----------------------------------------------------

    def _moved(
        self, status: TicketStatus, at: datetime, by: str, note: str = "", **changes
    ) -> ScaleTicket:
        if not self.can_become(status):
            raise TicketStateError(
                "that is not a move this ticket can make",
                number=str(self.number),
                current=self.status.value,
                attempted=status.value,
            )
        event = TicketEvent(at=at, became=status, by=by, note=note)
        return replace(
            self, status=status, history=(*self.history, event), **changes
        )

    def graded_as(
        self,
        sample: FactorSample,
        standard: CropStandard,
        policy: ShrinkPolicy,
        *,
        at: datetime,
        by: str,
    ) -> ScaleTicket:
        """Grade the load and work out what it is worth in bushels."""
        if standard.code != self.commodity_code:
            raise ValidationError(
                "graded against the wrong crop standard",
                ticket=self.commodity_code,
                standard=standard.code,
            )
        if policy.commodity_code != self.commodity_code:
            raise ValidationError(
                "the shrink policy is for a different crop",
                ticket=self.commodity_code,
                policy=policy.commodity_code,
            )
        result = determine(sample, standard)
        gross = from_weight(self.weights.net, standard)
        shrunk = apply_shrink(gross, sample.moisture, policy)
        note = f"{result.label}, {shrunk.net} net"
        return self._moved(
            TicketStatus.GRADED,
            at,
            by,
            note,
            sample=sample,
            grade=result,
            shrink=shrunk,
        )

    def applied(self, *, at: datetime, by: str, note: str = "") -> ScaleTicket:
        """Mark the bushels as having landed against an obligation."""
        if self.shrink is None:
            raise TicketStateError(
                "a ticket cannot be applied before it is graded", number=str(self.number)
            )
        return self._moved(TicketStatus.APPLIED, at, by, note)

    def settled(self, *, at: datetime, by: str, note: str = "") -> ScaleTicket:
        return self._moved(TicketStatus.SETTLED, at, by, note)

    def voided(self, reason: str, *, at: datetime, by: str) -> ScaleTicket:
        if not reason.strip():
            raise ValidationError("voiding a ticket needs a reason", number=str(self.number))
        return self._moved(
            TicketStatus.VOID, at, by, reason, void_reason=reason.strip()
        )

    def corrected_by(
        self, number: TicketNumber, reason: str, *, at: datetime, by: str
    ) -> tuple[ScaleTicket, ScaleTicket]:
        """Void this ticket and hand back a fresh one that replaces it.

        The replacement carries the same weights and grading as this ticket, so
        the caller only has to change what was wrong. It points back at the
        original through :attr:`voids`, which is what lets the position report
        net the pair instead of double counting them.
        """
        if number == self.number:
            raise ValidationError(
                "a correction needs a new number", number=str(self.number)
            )
        void = self.voided(f"corrected by {number}: {reason}", at=at, by=by)
        replacement = ScaleTicket(
            number=number,
            kind=self.kind,
            commodity_code=self.commodity_code,
            account=self.account,
            weighed_on=self.weighed_on,
            weights=self.weights,
            hauler=self.hauler,
            voids=self.number,
            history=(
                TicketEvent(
                    at=at,
                    became=TicketStatus.WEIGHED,
                    by=by,
                    note=f"corrects {self.number}: {reason}",
                ),
            ),
        )
        return void, replacement

    # -- rendering -------------------------------------------------------

    def describe(self) -> dict[str, object]:
        return {
            "number": str(self.number),
            "kind": self.kind.value,
            "commodity": self.commodity_code,
            "account": self.account,
            "weighed_on": self.weighed_on.isoformat(),
            "status": self.status.value,
            "weights": self.weights.describe(),
            "grade": None if self.grade is None else self.grade.describe(),
            "shrink": None if self.shrink is None else self.shrink.describe(),
            "voids": None if self.voids is None else str(self.voids),
            "void_reason": self.void_reason,
            "needs_review": self.needs_review,
            "history": [event.describe() for event in self.history],
        }
