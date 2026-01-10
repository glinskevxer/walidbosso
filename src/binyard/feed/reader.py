"""Reading the scale house indicator feed.

The indicator in the scale house writes a fixed width record for every
weighing, to a file on a share, and it has done so since before anybody here
was born. The layout is not negotiable, the field widths are what they are, and
a record that does not fit them is a hardware problem rather than a data
problem.

Two decisions that matter more than the parsing:

**A bad line does not stop the batch.** The file holds a whole day and one
scrambled record in the middle of it is normal, so the reader collects
rejections alongside accepted records and the caller decides. Raising on the
first bad line would mean a day's grain waits for somebody to hand edit a file.

**Nothing is coerced quietly.** A blank moisture field is a blank moisture
field, not zero. A load with no moisture cannot be graded and the reader says
so rather than handing the grading code a number the meter never produced.

The layout is data, so a house whose indicator writes a different one changes a
tuple rather than the parser.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final

from binyard.errors import ValidationError
from binyard.percent import Percent
from binyard.units import Weight

__all__ = [
    "LAYOUT",
    "RECORD_WIDTH",
    "FeedBatch",
    "FeedRecord",
    "Field",
    "FieldKind",
    "Rejection",
    "read_feed",
]


class FieldKind(StrEnum):
    TEXT = "text"
    INTEGER = "integer"
    DECIMAL = "decimal"
    DATE = "date"
    TIME = "time"


@dataclass(frozen=True, slots=True)
class Field:
    """One column of the record: where it sits and what it means."""

    name: str
    start: int
    width: int
    kind: FieldKind
    #: Implied decimal places. The indicator writes 048240 for 482.40 pounds.
    scale: int = 0
    required: bool = True

    @property
    def end(self) -> int:
        return self.start + self.width

    def slice_of(self, line: str) -> str:
        return line[self.start : self.end]


#: The layout this house's indicator writes. Positions are zero based.
LAYOUT: Final[tuple[Field, ...]] = (
    Field("record_type", 0, 2, FieldKind.TEXT),
    Field("sequence", 2, 6, FieldKind.INTEGER),
    Field("weighed_on", 8, 8, FieldKind.DATE),
    Field("weighed_at", 16, 6, FieldKind.TIME),
    Field("account", 22, 20, FieldKind.TEXT),
    Field("commodity", 42, 8, FieldKind.TEXT),
    Field("gross", 50, 8, FieldKind.DECIMAL, scale=2),
    Field("tare", 58, 8, FieldKind.DECIMAL, scale=2),
    Field("moisture", 66, 4, FieldKind.DECIMAL, scale=1, required=False),
    Field("test_weight", 70, 4, FieldKind.DECIMAL, scale=1, required=False),
    Field("hauler", 74, 12, FieldKind.TEXT, required=False),
)

RECORD_WIDTH: Final = LAYOUT[-1].end

#: Record types the reader accepts. Anything else is somebody else's problem.
INBOUND: Final = "IN"
OUTBOUND: Final = "OU"


@dataclass(frozen=True, slots=True)
class FeedRecord:
    """One weighing off the indicator, parsed but not yet believed."""

    line_number: int
    record_type: str
    sequence: int
    weighed_on: date
    weighed_at: datetime
    account: str
    commodity: str
    gross: Weight
    tare: Weight
    moisture: Percent | None
    test_weight: Decimal | None
    hauler: str | None
    raw: str

    @property
    def net(self) -> Weight:
        return self.gross - self.tare

    @property
    def is_inbound(self) -> bool:
        return self.record_type == INBOUND

    @property
    def fingerprint(self) -> str:
        """A stable hash of the record, used to recognise a re-sent file.

        The line number is deliberately not in it. The same weighing re-sent in
        a different position in the file is the same weighing.
        """
        return hashlib.sha256(self.raw.encode("ascii", "replace")).hexdigest()

    @property
    def is_gradeable(self) -> bool:
        return self.moisture is not None and self.test_weight is not None

    def describe(self) -> dict[str, object]:
        return {
            "line": self.line_number,
            "type": self.record_type,
            "sequence": self.sequence,
            "weighed_on": self.weighed_on.isoformat(),
            "account": self.account,
            "commodity": self.commodity,
            "gross": str(self.gross),
            "tare": str(self.tare),
            "net": str(self.net),
            "moisture": None if self.moisture is None else str(self.moisture),
            "test_weight": None if self.test_weight is None else str(self.test_weight),
            "gradeable": self.is_gradeable,
            "fingerprint": self.fingerprint[:12],
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    """A line the reader would not accept, and why."""

    line_number: int
    reason: str
    raw: str

    def describe(self) -> dict[str, object]:
        return {"line": self.line_number, "reason": self.reason, "raw": self.raw.rstrip()}


@dataclass(frozen=True, slots=True)
class FeedBatch:
    """Everything one file held."""

    records: tuple[FeedRecord, ...]
    rejections: tuple[Rejection, ...]
    lines_read: int

    @property
    def accepted(self) -> int:
        return len(self.records)

    @property
    def refused(self) -> int:
        return len(self.rejections)

    @property
    def is_clean(self) -> bool:
        return not self.rejections

    def describe(self) -> dict[str, object]:
        return {
            "lines_read": self.lines_read,
            "accepted": self.accepted,
            "refused": self.refused,
            "clean": self.is_clean,
            "rejections": [item.describe() for item in self.rejections],
        }


def read_feed(text: str) -> FeedBatch:
    """Parse a whole feed file, keeping the bad lines rather than raising."""
    records: list[FeedRecord] = []
    rejections: list[Rejection] = []
    lines = 0

    for index, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        lines += 1
        try:
            records.append(_record(index, line))
        except ValidationError as error:
            rejections.append(Rejection(index, str(error), line))

    return FeedBatch(records=tuple(records), rejections=tuple(rejections), lines_read=lines)


def _record(line_number: int, line: str) -> FeedRecord:
    stripped = line.rstrip("\r\n")
    if len(stripped) < RECORD_WIDTH:
        raise ValidationError("the record is short", expected=RECORD_WIDTH, got=len(stripped))
    padded = stripped.ljust(RECORD_WIDTH)

    record_type = _text(_field("record_type"), padded)
    if record_type not in {INBOUND, OUTBOUND}:
        raise ValidationError("unknown record type", got=record_type)

    weighed_on = _day(_field("weighed_on"), padded)
    hours, minutes, seconds = _clock(_field("weighed_at"), padded)
    moisture = _optional_digits(_field("moisture"), padded)
    test_weight = _optional_digits(_field("test_weight"), padded)
    hauler = _optional_text(_field("hauler"), padded)

    return FeedRecord(
        line_number=line_number,
        record_type=record_type,
        sequence=_digits(_field("sequence"), padded),
        weighed_on=weighed_on,
        weighed_at=datetime(
            weighed_on.year, weighed_on.month, weighed_on.day, hours, minutes, seconds
        ),
        account=_text(_field("account"), padded),
        commodity=_text(_field("commodity"), padded),
        gross=Weight(_digits(_field("gross"), padded)),
        tare=Weight(_digits(_field("tare"), padded)),
        moisture=None if moisture is None else Percent(moisture * 10),
        test_weight=None if test_weight is None else Decimal(test_weight) / 10,
        hauler=hauler,
        raw=padded[:RECORD_WIDTH],
    )


def _field(name: str) -> Field:
    for field in LAYOUT:
        if field.name == name:
            return field
    raise ValidationError("no such field in the layout", name=name)  # pragma: no cover


def _raw(field: Field, line: str) -> str:
    value = field.slice_of(line).strip()
    if not value and field.required:
        raise ValidationError(f"{field.name} is blank and cannot be")
    return value


def _text(field: Field, line: str) -> str:
    return _raw(field, line)


def _optional_text(field: Field, line: str) -> str | None:
    return _raw(field, line) or None


def _digits(field: Field, line: str) -> int:
    raw = _raw(field, line)
    try:
        return int(Decimal(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field.name} is not a number", got=raw) from exc


def _optional_digits(field: Field, line: str) -> int | None:
    raw = _raw(field, line)
    if not raw:
        return None
    try:
        return int(Decimal(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field.name} is not a number", got=raw) from exc


def _day(field: Field, line: str) -> date:
    raw = _raw(field, line)
    try:
        return datetime.strptime(raw, "%Y%m%d").date()
    except ValueError as exc:
        raise ValidationError(f"{field.name} is not a date", got=raw) from exc


def _clock(field: Field, line: str) -> tuple[int, int, int]:
    raw = _raw(field, line)
    if len(raw) != 6 or not raw.isdigit():
        raise ValidationError(f"{field.name} is not a time", got=raw)
    hours, minutes, seconds = int(raw[0:2]), int(raw[2:4]), int(raw[4:6])
    if hours > 23 or minutes > 59 or seconds > 59:
        raise ValidationError(f"{field.name} is not a time", got=raw)
    return hours, minutes, seconds
