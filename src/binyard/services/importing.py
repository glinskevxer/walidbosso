"""Turning a feed file into tickets, once.

The importer is the only part of this package that is allowed to see the same
input twice and do nothing the second time. Everywhere else a repeat is an
error; here it is Tuesday.

Each record gets one of five outcomes and every one of them is written to the
feed log, including the ones that did nothing. A record that was refused for
being too wet has to stay refused when the file is resent, or the same load
gets re-examined every morning until somebody hand edits the file.

The whole file is one transaction. A file that fails halfway leaves nothing
behind, which matters because the alternative is a partially imported day and
no way to tell where it stopped.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from binyard import commodity
from binyard.bushels import Bushels
from binyard.contracts.application import Priority
from binyard.discounts.schedule import ScheduleBook
from binyard.errors import BinyardError, ValidationError
from binyard.feed.reader import FeedBatch, FeedRecord
from binyard.grading.factors import FactorSample
from binyard.identity import Sequence
from binyard.observability import metrics
from binyard.observability.logging import bound, get_logger
from binyard.services.receiving import ReceiptRequest, receive_load
from binyard.store.feed import FeedEntry
from binyard.store.protocol import UnitOfWork
from binyard.tickets.weights import ScaleWeights, TareSource

__all__ = ["ImportResult", "ImportedRecord", "Outcome", "import_feed"]

_log = get_logger("binyard.importing")


class Outcome:
    """The five things that can happen to one record."""

    TAKEN = "taken"
    DUPLICATE = "duplicate"
    NOT_GRADEABLE = "not_gradeable"
    REFUSED = "refused"
    UNSUPPORTED = "unsupported"

    ALL = (TAKEN, DUPLICATE, NOT_GRADEABLE, REFUSED, UNSUPPORTED)


@dataclass(frozen=True, slots=True)
class ImportedRecord:
    """One record and what became of it."""

    line_number: int
    fingerprint: str
    outcome: str
    ticket_number: str | None = None
    reason: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "line": self.line_number,
            "outcome": self.outcome,
            "ticket": self.ticket_number,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What one file did."""

    source: str
    records: tuple[ImportedRecord, ...]
    rejected_lines: int
    bushels_taken: Bushels

    def count(self, outcome: str) -> int:
        return sum(1 for record in self.records if record.outcome == outcome)

    @property
    def taken(self) -> int:
        return self.count(Outcome.TAKEN)

    @property
    def duplicates(self) -> int:
        return self.count(Outcome.DUPLICATE)

    @property
    def is_clean(self) -> bool:
        return self.rejected_lines == 0 and all(
            record.outcome in {Outcome.TAKEN, Outcome.DUPLICATE} for record in self.records
        )

    def describe(self) -> dict[str, object]:
        return {
            "source": self.source,
            "rejected_lines": self.rejected_lines,
            "bushels_taken": str(self.bushels_taken),
            "clean": self.is_clean,
            "counts": {outcome: self.count(outcome) for outcome in Outcome.ALL},
            "records": [record.describe() for record in self.records],
        }


def import_feed(
    work: UnitOfWork,
    batch: FeedBatch,
    sequence: Sequence,
    *,
    source: str,
    book: ScheduleBook,
    bin_for: dict[str, str],
    at: datetime | None = None,
    priority: Priority = Priority.EARLIEST_DEADLINE,
) -> ImportResult:
    """Import a parsed feed file. Everything or nothing."""
    if not source.strip():
        raise ValidationError("an import needs to say where it came from")
    stamped = at or datetime.now()

    outcomes: list[ImportedRecord] = []
    taken = Bushels.zero()

    with bound(feed=source):
        for record in batch.records:
            if work.feed.seen(record.fingerprint):
                previous = work.feed.get(record.fingerprint)
                outcomes.append(
                    ImportedRecord(
                        line_number=record.line_number,
                        fingerprint=record.fingerprint,
                        outcome=Outcome.DUPLICATE,
                        ticket_number=previous.ticket_number,
                        reason=f"already imported from {previous.source}",
                    )
                )
                continue

            result = _one(
                work,
                record,
                sequence,
                book=book,
                bin_for=bin_for,
                at=stamped,
                priority=priority,
            )
            outcomes.append(result)
            work.feed.add(
                FeedEntry(
                    fingerprint=record.fingerprint,
                    imported_at=stamped,
                    source=source,
                    outcome=result.outcome,
                    ticket_number=result.ticket_number,
                    note=result.reason,
                )
            )
            if result.outcome == Outcome.TAKEN:
                taken = taken + _net_of(work, result)

    work.commit()
    imported = ImportResult(
        source=source,
        records=tuple(outcomes),
        rejected_lines=batch.refused,
        bushels_taken=taken,
    )
    _log.info(
        "feed imported",
        source=source,
        taken=imported.taken,
        duplicates=imported.duplicates,
        refused=imported.count(Outcome.REFUSED),
        unreadable=batch.refused,
        bushels=str(taken),
    )
    for outcome in Outcome.ALL:
        count = imported.count(outcome)
        if count:
            metrics.counter("feed_records_total", float(count), outcome=outcome)
    return imported


def _one(
    work: UnitOfWork,
    record: FeedRecord,
    sequence: Sequence,
    *,
    book: ScheduleBook,
    bin_for: dict[str, str],
    at: datetime,
    priority: Priority,
) -> ImportedRecord:
    if not record.is_inbound:
        return ImportedRecord(
            record.line_number,
            record.fingerprint,
            Outcome.UNSUPPORTED,
            reason="outbound records are ticketed by the load-out desk",
        )
    if not record.is_gradeable:
        return ImportedRecord(
            record.line_number,
            record.fingerprint,
            Outcome.NOT_GRADEABLE,
            reason="the meter gave no moisture or no test weight",
        )

    try:
        standard = commodity.lookup(record.commodity)
    except BinyardError as error:
        return ImportedRecord(
            record.line_number, record.fingerprint, Outcome.REFUSED, reason=str(error)
        )

    destination = bin_for.get(standard.code)
    if destination is None:
        return ImportedRecord(
            record.line_number,
            record.fingerprint,
            Outcome.REFUSED,
            reason=f"no bin is nominated for {standard.code}",
        )

    assert record.moisture is not None and record.test_weight is not None
    request = ReceiptRequest(
        account=record.account,
        standard=standard,
        weights=ScaleWeights(
            gross=record.gross,
            tare=record.tare,
            tare_source=TareSource.WEIGHED,
            gross_at=record.weighed_at,
            tare_at=record.weighed_at,
        ),
        sample=FactorSample(moisture=record.moisture, test_weight=record.test_weight),
        bin_identifier=destination,
        weighed_on=record.weighed_on,
        at=record.weighed_at,
        by=f"feed:{record.sequence:06d}",
        hauler=record.hauler,
        priority=priority,
    )

    try:
        receipt = receive_load(work, sequence, request, book=book)
    except BinyardError as error:
        return ImportedRecord(
            record.line_number, record.fingerprint, Outcome.REFUSED, reason=str(error)
        )

    return ImportedRecord(
        line_number=record.line_number,
        fingerprint=record.fingerprint,
        outcome=Outcome.TAKEN,
        ticket_number=str(receipt.ticket.number),
    )


def _net_of(work: UnitOfWork, result: ImportedRecord) -> Bushels:
    from binyard.identity import TicketNumber

    if result.ticket_number is None:  # pragma: no cover - only TAKEN reaches here
        return Bushels.zero()
    ticket = work.tickets.get(TicketNumber.parse(result.ticket_number))
    return ticket.net_bushels or Bushels.zero()
