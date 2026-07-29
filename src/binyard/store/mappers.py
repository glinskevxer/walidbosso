"""Turning domain objects into rows and back.

Mapping lives here rather than on the domain types. A dataclass that knows how
to write itself to SQL is a dataclass that changes when the schema changes, and
the whole reason the domain is pure is so that it does not.

The JSON blobs are read and written by these functions only. Their shape is
part of the schema, so it is versioned by migration like a column would be, and
:func:`sample_from_json` is written to tolerate a missing key rather than
raising, because a blob written by an older version of this package is a normal
thing to meet after a deploy.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from binyard.bushels import Bushels
from binyard.grading.factors import FactorName, FactorSample, Odor
from binyard.grading.grade import GradeResult, LimitingFactor, SpecialGrade
from binyard.percent import Percent
from binyard.shrink import ShrinkResult
from binyard.tickets.ticket import TicketEvent, TicketStatus
from binyard.tickets.weights import ScaleWeights, TareSource
from binyard.units import Weight

__all__ = [
    "as_date",
    "as_datetime",
    "grade_from_json",
    "grade_to_json",
    "history_from_json",
    "history_to_json",
    "sample_from_json",
    "sample_to_json",
    "shrink_from_json",
    "shrink_to_json",
    "weights_from_row",
    "weights_to_row",
]

_PERCENT_FIELDS = (
    "moisture",
    "total_damage",
    "heat_damage",
    "foreign_material",
    "broken",
    "dockage",
    "other_classes",
    "shrunken_broken",
)


def as_date(value: str | date) -> date:
    """Read a stored date, whatever shape the driver handed back."""
    return (
        value
        if isinstance(value, date) and not isinstance(value, datetime)
        else (value.date() if isinstance(value, datetime) else date.fromisoformat(str(value)))
    )


def as_datetime(value: str | datetime | None) -> datetime | None:
    """Read a stored timestamp, or ``None`` when the column is null."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def sample_to_json(sample: FactorSample | None) -> str | None:
    """Render a composite sample as the JSON a row carries, or ``None``."""
    if sample is None:
        return None
    payload: dict[str, Any] = {
        name: getattr(sample, name).hundredths for name in _PERCENT_FIELDS
    }
    payload["test_weight"] = str(sample.test_weight)
    payload["odor"] = sample.odor.value
    payload["infested"] = sample.infested
    return json.dumps(payload, sort_keys=True)


def sample_from_json(blob: str | None) -> FactorSample | None:
    """Read a sample back, tolerating a key an older version did not write."""
    if blob is None:
        return None
    payload = json.loads(blob)
    fields: dict[str, Any] = {
        name: Percent(int(payload.get(name, 0))) for name in _PERCENT_FIELDS
    }
    fields["test_weight"] = Decimal(payload["test_weight"])
    fields["odor"] = Odor(payload.get("odor", Odor.NONE.value))
    fields["infested"] = bool(payload.get("infested", False))
    return FactorSample(**fields)


def grade_to_json(result: GradeResult | None) -> str | None:
    """Render a grade result, its sample, its limits and its notes."""
    if result is None:
        return None
    payload = {
        "commodity_code": result.commodity_code,
        "grade": result.grade,
        "graded_sample": json.loads(sample_to_json(result.graded_sample) or "{}"),
        "limiting": [
            {
                "factor": item.factor.value,
                "reading": str(item.reading),
                "limit": str(item.limit),
            }
            for item in result.limiting
        ],
        "special": [item.value for item in result.special],
        "notes": list(result.notes),
    }
    return json.dumps(payload, sort_keys=True)


def grade_from_json(blob: str | None) -> GradeResult | None:
    """Read a grade result back whole."""
    if blob is None:
        return None
    payload = json.loads(blob)
    sample = sample_from_json(json.dumps(payload["graded_sample"]))
    assert sample is not None
    return GradeResult(
        commodity_code=payload["commodity_code"],
        grade=int(payload["grade"]),
        graded_sample=sample,
        limiting=tuple(
            LimitingFactor(
                factor=FactorName(item["factor"]),
                reading=Decimal(item["reading"]),
                limit=Decimal(item["limit"]),
            )
            for item in payload.get("limiting", [])
        ),
        special=tuple(SpecialGrade(item) for item in payload.get("special", [])),
        notes=tuple(payload.get("notes", [])),
    )


def shrink_to_json(result: ShrinkResult | None) -> str | None:
    """Render a shrink result as its five exact counts."""
    if result is None:
        return None
    payload = {
        "gross": result.gross.hundredths,
        "points_removed": result.points_removed.hundredths,
        "water_shrink": result.water_shrink.hundredths,
        "handling_shrink": result.handling_shrink.hundredths,
        "net": result.net.hundredths,
    }
    return json.dumps(payload, sort_keys=True)


def shrink_from_json(blob: str | None) -> ShrinkResult | None:
    """Read a shrink result back from its counts."""
    if blob is None:
        return None
    payload = json.loads(blob)
    return ShrinkResult(
        gross=Bushels(int(payload["gross"])),
        points_removed=Percent(int(payload["points_removed"])),
        water_shrink=Bushels(int(payload["water_shrink"])),
        handling_shrink=Bushels(int(payload["handling_shrink"])),
        net=Bushels(int(payload["net"])),
    )


def history_to_json(history: tuple[TicketEvent, ...]) -> str:
    """Render a ticket's audit trail as a list of events."""
    return json.dumps(
        [
            {
                "at": event.at.isoformat(),
                "became": event.became.value,
                "by": event.by,
                "note": event.note,
            }
            for event in history
        ]
    )


def history_from_json(blob: str | None) -> tuple[TicketEvent, ...]:
    """Read an audit trail back, treating a missing blob as no history."""
    if not blob:
        return ()
    return tuple(
        TicketEvent(
            at=datetime.fromisoformat(item["at"]),
            became=TicketStatus(item["became"]),
            by=item["by"],
            note=item.get("note", ""),
        )
        for item in json.loads(blob)
    )


def weights_to_row(weights: ScaleWeights) -> dict[str, Any]:
    """Spread a weighing across the five columns the tickets table has."""
    return {
        "gross_hundredths": weights.gross.hundredths,
        "tare_hundredths": weights.tare.hundredths,
        "tare_source": weights.tare_source.value,
        "gross_at": None if weights.gross_at is None else weights.gross_at.isoformat(),
        "tare_at": None if weights.tare_at is None else weights.tare_at.isoformat(),
    }


def weights_from_row(row: dict[str, Any]) -> ScaleWeights:
    """Rebuild a weighing from a ticket row."""
    return ScaleWeights(
        gross=Weight(int(row["gross_hundredths"])),
        tare=Weight(int(row["tare_hundredths"])),
        tare_source=TareSource(row["tare_source"]),
        gross_at=as_datetime(row.get("gross_at")),
        tare_at=as_datetime(row.get("tare_at")),
    )
