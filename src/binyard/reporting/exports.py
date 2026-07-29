"""Writing a report to a file somebody else is going to open.

The renderers already turn a table into text, CSV or JSON. What is left is the
part that goes wrong at three in the afternoon on the last day of the month:

**Write it whole or not at all.** A report is written to a temporary file
beside the destination and then moved into place, which is atomic on every
filesystem this runs on. An examiner opening a half written CSV because the
process was killed is worse than an examiner waiting.

**Do not overwrite by accident.** Writing over an existing file needs saying
so. The examiner statement for November is written once and a second run that
silently replaced it would destroy the thing somebody signed.

**Name it for what it is.** A file called ``report.csv`` in a folder of forty
of them is useless, so the naming is here rather than left to whoever calls it.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path

from binyard.errors import ConfigurationError, ValidationError
from binyard.reporting.table import Table, to_csv, to_json, to_text

__all__ = ["Format", "Written", "suggested_name", "write_table"]


class Format(StrEnum):
    """How a report is rendered on its way to a file."""

    TEXT = "text"
    CSV = "csv"
    JSON = "json"

    @property
    def suffix(self) -> str:
        return {Format.TEXT: ".txt", Format.CSV: ".csv", Format.JSON: ".json"}[self]

    def render(self, table: Table) -> str:
        if self is Format.CSV:
            return to_csv(table, include_title=True)
        if self is Format.JSON:
            return to_json(table)
        return to_text(table)


@dataclass(frozen=True, slots=True)
class Written:
    """What was written, and how much of it."""

    path: Path
    format: Format
    rows: int
    bytes_written: int

    def describe(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "format": self.format.value,
            "rows": self.rows,
            "bytes": self.bytes_written,
        }


def suggested_name(kind: str, on: date, location: str, fmt: Format) -> str:
    """``position-WBS-2025-11-30.csv``, which sorts and says what it is."""
    slug = "-".join(part for part in kind.lower().split() if part)
    if not slug:
        raise ValidationError("a report name needs a kind")
    return f"{slug}-{location.upper()}-{on.isoformat()}{fmt.suffix}"


def write_table(
    table: Table,
    path: Path | str,
    fmt: Format = Format.CSV,
    *,
    overwrite: bool = False,
) -> Written:
    """Render and write, atomically, refusing to clobber unless told to."""
    destination = Path(path)
    if destination.is_dir():
        raise ValidationError("that is a directory, not a file", path=str(destination))
    if destination.exists() and not overwrite:
        raise ConfigurationError(
            "that file is already there, pass overwrite to replace it",
            path=str(destination),
        )
    parent = destination.parent
    if not parent.exists():
        raise ValidationError("no such directory", path=str(parent))

    body = fmt.render(table)
    encoded = body.encode("utf-8")

    handle, temporary = tempfile.mkstemp(
        dir=parent, prefix=f".{destination.name}.", suffix=".part"
    )
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, destination)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise

    return Written(
        path=destination,
        format=fmt,
        rows=len(table.rows),
        bytes_written=len(encoded),
    )
