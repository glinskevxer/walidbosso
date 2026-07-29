"""One table type, three renderings.

Every report in this package ends up in one of three places: a terminal, a
spreadsheet, or another program. Writing three renderers per report is how the
CSV ends up with a column the printed version does not have, so reports build a
:class:`Table` and the rendering is chosen at the edge.

The table knows the type of each column, which is what lets the plain text
renderer right align numbers and the CSV renderer leave them unquoted. It does
not know about grain.

Numbers are already strings by the time they arrive. Everything upstream holds
exact types and knows how it wants to be printed, and handing a table a
``Decimal`` to format is how two reports end up disagreeing about how many
places a moisture reading has.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from binyard.errors import ValidationError

__all__ = ["Align", "Column", "Table", "to_csv", "to_json", "to_text"]


class Align(StrEnum):
    """Which way a column sits."""

    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class Column:
    """One column: what it is called, and how it sits."""

    key: str
    heading: str
    align: Align = Align.LEFT
    #: Printed in place of a missing or empty value.
    blank: str = ""

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValidationError("a column needs a key")
        if not self.heading.strip():
            raise ValidationError("a column needs a heading", key=self.key)

    @classmethod
    def number(cls, key: str, heading: str, blank: str = "-") -> Column:
        return cls(key=key, heading=heading, align=Align.RIGHT, blank=blank)


@dataclass(frozen=True, slots=True)
class Table:
    """A title, some columns, and rows keyed by column key."""

    title: str
    columns: tuple[Column, ...]
    rows: tuple[dict[str, Any], ...] = ()
    #: Printed under the rows. Used for totals and for notes.
    footers: tuple[dict[str, Any], ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.columns:
            raise ValidationError("a table needs at least one column", title=self.title)
        keys = [column.key for column in self.columns]
        if len(set(keys)) != len(keys):
            raise ValidationError("two columns share a key", title=self.title)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(column.key for column in self.columns)

    @property
    def is_empty(self) -> bool:
        return not self.rows

    def cell(self, row: dict[str, Any], column: Column) -> str:
        value = row.get(column.key)
        if value is None or value == "":
            return column.blank
        if isinstance(value, bool):
            return "yes" if value else "no"
        return str(value)

    def with_rows(self, rows: list[dict[str, Any]]) -> Table:
        return Table(
            title=self.title,
            columns=self.columns,
            rows=(*self.rows, *rows),
            footers=self.footers,
        )


def to_csv(table: Table, *, include_title: bool = False) -> str:
    """Render for a spreadsheet. Footers are ordinary rows."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    if include_title:
        writer.writerow([table.title])
    writer.writerow([column.heading for column in table.columns])
    for row in (*table.rows, *table.footers):
        writer.writerow([table.cell(row, column) for column in table.columns])
    return buffer.getvalue()


def to_text(table: Table, *, width_limit: int = 200) -> str:
    """Render for a terminal, with the columns lined up."""
    widths = [len(column.heading) for column in table.columns]
    every = (*table.rows, *table.footers)
    for row in every:
        for index, column in enumerate(table.columns):
            widths[index] = max(widths[index], len(table.cell(row, column)))

    total = sum(widths) + 2 * (len(widths) - 1)
    if total > width_limit:
        overflow = total - width_limit
        widest = max(range(len(widths)), key=lambda i: widths[i])
        widths[widest] = max(4, widths[widest] - overflow)

    lines = [table.title, "=" * min(len(table.title), width_limit)]
    lines.append(_row([c.heading for c in table.columns], widths, table.columns))
    lines.append("  ".join("-" * width for width in widths))
    if table.is_empty:
        lines.append("(nothing to report)")
    for row in table.rows:
        lines.append(_row([table.cell(row, c) for c in table.columns], widths, table.columns))
    if table.footers:
        lines.append("  ".join("-" * width for width in widths))
        for row in table.footers:
            lines.append(
                _row([table.cell(row, c) for c in table.columns], widths, table.columns)
            )
    return "\n".join(lines) + "\n"


def to_json(table: Table, *, indent: int | None = 2) -> str:
    """Render for another program. Keys, not headings."""
    payload = {
        "title": table.title,
        "columns": [
            {"key": column.key, "heading": column.heading, "align": column.align.value}
            for column in table.columns
        ],
        "rows": [
            {column.key: table.cell(row, column) for column in table.columns}
            for row in table.rows
        ],
        "footers": [
            {column.key: table.cell(row, column) for column in table.columns}
            for row in table.footers
        ],
    }
    return json.dumps(payload, indent=indent)


def _row(cells: list[str], widths: list[int], columns: tuple[Column, ...]) -> str:
    parts: list[str] = []
    for cell, width, column in zip(cells, widths, columns, strict=True):
        clipped = cell if len(cell) <= width else cell[: width - 1] + "…"
        parts.append(
            clipped.rjust(width) if column.align is Align.RIGHT else clipped.ljust(width)
        )
    return "  ".join(parts).rstrip()
