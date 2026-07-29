"""Replies already sent, so a retry gets the same answer rather than a new load.

Keyed by whatever the caller put in ``Idempotency-Key``, with a hash of the
request beside it. Three cases and they are all different:

* **No key stored.** A first attempt. Do the work and remember the answer.
* **Key stored, same request.** A retry. Replay the stored answer and touch
  nothing.
* **Key stored, different request.** The caller reused a key for a different
  body, which is a bug on their side and gets a 409 rather than either answer.

Only successful replies are stored. A 400 that is retried should be a 400
again, and it will be, because the request that produced it has not changed.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from binyard.errors import Conflict

__all__ = ["MemoryReplyStore", "ReplyStore", "SqliteReplyStore", "StoredReply", "digest"]


def digest(route: str, body: bytes) -> str:
    """A stable fingerprint of one request."""
    return hashlib.sha256(route.encode("utf-8") + b"\0" + body).hexdigest()


@dataclass(frozen=True, slots=True)
class StoredReply:
    """One answer, kept in case the caller asks again."""

    key: str
    route: str
    request_hash: str
    status: int
    body: str
    stored_at: datetime

    def matches(self, route: str, request_hash: str) -> bool:
        return self.route == route and self.request_hash == request_hash


class ReplyStore:
    """The interface, written as a base class so both backends read the same."""

    def get(self, key: str) -> StoredReply | None:  # pragma: no cover - interface
        raise NotImplementedError

    def remember(self, reply: StoredReply) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def replay(self, key: str, route: str, request_hash: str) -> StoredReply | None:
        """The stored answer for this exact request, or nothing.

        Raises when the key is known and the request is not the one it was
        stored for, because handing back somebody else's ticket is worse than
        any error.
        """
        stored = self.get(key)
        if stored is None:
            return None
        if not stored.matches(route, request_hash):
            raise Conflict(
                "that idempotency key was used for a different request",
                key=key,
                stored_for=stored.route,
            )
        return stored


class SqliteReplyStore(ReplyStore):
    """Replies already sent, in one table with the key as its primary key."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def get(self, key: str) -> StoredReply | None:
        row = self._connection.execute(
            "SELECT * FROM idempotent_replies WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        mapping = dict(row)
        return StoredReply(
            key=mapping["key"],
            route=mapping["route"],
            request_hash=mapping["request_hash"],
            status=int(mapping["status"]),
            body=mapping["body"],
            stored_at=datetime.fromisoformat(mapping["stored_at"]),
        )

    def remember(self, reply: StoredReply) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO idempotent_replies"
            " (key, route, request_hash, status, body, stored_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                reply.key,
                reply.route,
                reply.request_hash,
                reply.status,
                reply.body,
                reply.stored_at.isoformat(),
            ),
        )


class MemoryReplyStore(ReplyStore):
    """Replies already sent, in a dictionary keyed by idempotency key."""

    def __init__(self, replies: dict[str, StoredReply] | None = None) -> None:
        self._replies: dict[str, StoredReply] = dict(replies or {})

    def get(self, key: str) -> StoredReply | None:
        return self._replies.get(key)

    def remember(self, reply: StoredReply) -> None:
        self._replies[reply.key] = reply

    def snapshot(self) -> dict[str, StoredReply]:
        return dict(self._replies)

    def restore(self, snapshot: dict[str, StoredReply]) -> None:
        self._replies = dict(snapshot)
