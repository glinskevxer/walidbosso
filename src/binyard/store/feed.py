"""The record of what has already been imported.

One table, one primary key, and the primary key is the whole point: an insert
that collides is a file that has been seen before, and the database says so
without a read-then-write race.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from binyard.errors import Conflict, NotFound

__all__ = ["FeedEntry", "FeedLog", "MemoryFeedLog", "SqliteFeedLog"]


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """One record the importer has seen."""

    fingerprint: str
    imported_at: datetime
    source: str
    outcome: str
    ticket_number: str | None = None
    note: str = ""

    def describe(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint[:12],
            "imported_at": self.imported_at.isoformat(),
            "source": self.source,
            "outcome": self.outcome,
            "ticket": self.ticket_number,
            "note": self.note,
        }


class FeedLog:
    """The interface, written as a base class so both backends read the same."""

    def add(self, entry: FeedEntry) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def seen(self, fingerprint: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, fingerprint: str) -> FeedEntry:  # pragma: no cover - interface
        raise NotImplementedError

    def for_source(self, source: str) -> tuple[FeedEntry, ...]:  # pragma: no cover
        raise NotImplementedError


class SqliteFeedLog(FeedLog):
    """Feed fingerprints in one table, with the fingerprint as the key."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, entry: FeedEntry) -> None:
        if self.seen(entry.fingerprint):
            raise Conflict(
                "that record has been imported already", fingerprint=entry.fingerprint
            )
        self._connection.execute(
            "INSERT INTO feed_log (fingerprint, imported_at, source, ticket_number,"
            " outcome, note) VALUES (?, ?, ?, ?, ?, ?)",
            (
                entry.fingerprint,
                entry.imported_at.isoformat(),
                entry.source,
                entry.ticket_number,
                entry.outcome,
                entry.note,
            ),
        )

    def seen(self, fingerprint: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM feed_log WHERE fingerprint = ?", (fingerprint,)
            ).fetchone()
            is not None
        )

    def get(self, fingerprint: str) -> FeedEntry:
        row = self._connection.execute(
            "SELECT * FROM feed_log WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        if row is None:
            raise NotFound("feed record", fingerprint)
        return self._entry(row)

    def for_source(self, source: str) -> tuple[FeedEntry, ...]:
        rows = self._connection.execute(
            "SELECT * FROM feed_log WHERE source = ? ORDER BY imported_at, fingerprint",
            (source,),
        ).fetchall()
        return tuple(self._entry(row) for row in rows)

    @staticmethod
    def _entry(row: sqlite3.Row) -> FeedEntry:
        mapping = dict(row)
        return FeedEntry(
            fingerprint=mapping["fingerprint"],
            imported_at=datetime.fromisoformat(mapping["imported_at"]),
            source=mapping["source"],
            outcome=mapping["outcome"],
            ticket_number=mapping["ticket_number"],
            note=mapping["note"],
        )


class MemoryFeedLog(FeedLog):
    """Feed fingerprints in a dictionary, which is all the log needs to be."""

    def __init__(self, entries: dict[str, FeedEntry] | None = None) -> None:
        self._entries: dict[str, FeedEntry] = dict(entries or {})

    def add(self, entry: FeedEntry) -> None:
        if entry.fingerprint in self._entries:
            raise Conflict(
                "that record has been imported already", fingerprint=entry.fingerprint
            )
        self._entries[entry.fingerprint] = entry

    def seen(self, fingerprint: str) -> bool:
        return fingerprint in self._entries

    def get(self, fingerprint: str) -> FeedEntry:
        try:
            return self._entries[fingerprint]
        except KeyError:
            raise NotFound("feed record", fingerprint) from None

    def for_source(self, source: str) -> tuple[FeedEntry, ...]:
        return tuple(
            entry
            for entry in sorted(
                self._entries.values(), key=lambda item: (item.imported_at, item.fingerprint)
            )
            if entry.source == source
        )

    def snapshot(self) -> dict[str, FeedEntry]:
        return dict(self._entries)

    def restore(self, snapshot: dict[str, FeedEntry]) -> None:
        self._entries = dict(snapshot)
