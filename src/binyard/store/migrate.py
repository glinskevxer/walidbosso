"""A migration runner, small enough to read in one sitting.

Migrations are numbered SQL files in ``binyard/store/migrations``. The runner
applies the ones the database has not seen, in order, each in its own
transaction, and records what it did.

The part that earns its keep is the checksum. Every applied migration's
contents are hashed and stored. If a file that has already run comes back
changed, the runner stops and says which one, rather than skipping it because
the version number is familiar. Editing an applied migration is the most common
way a staging database and a production one quietly diverge, and it is silent
by default in most hand rolled runners.

There is no down migration and that is on purpose. Reversing a migration on a
live grain accounting database means deciding what happens to the rows the new
column already holds, and no generic answer to that is correct. A mistake is
fixed by a new migration forward.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from binyard.errors import ConfigurationError

__all__ = ["MIGRATIONS_DIR", "Migration", "applied", "discover", "migrate", "pending"]

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
)
"""


class Connection(Protocol):
    """The slice of DB-API this runner needs."""

    def execute(self, sql: str, parameters: Any = ...) -> Any: ...

    def executescript(self, sql: str) -> Any: ...

    def commit(self) -> None: ...

    def rollback(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered file."""

    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def filename(self) -> str:
        return f"{self.version:04d}_{self.name}.sql"


def discover(directory: Path | None = None) -> tuple[Migration, ...]:
    """Read the migration files, in order, refusing anything malformed."""
    where = directory or MIGRATIONS_DIR
    if not where.is_dir():
        raise ConfigurationError("no migrations directory", path=str(where))

    found: list[Migration] = []
    for path in sorted(where.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME.match(path.name)
        if match is None:
            raise ConfigurationError(
                "a migration filename must be NNNN_lower_snake.sql", filename=path.name
            )
        found.append(
            Migration(
                version=int(match.group("version")),
                name=match.group("name"),
                sql=path.read_text(encoding="utf-8"),
            )
        )

    versions = [migration.version for migration in found]
    if len(set(versions)) != len(versions):
        raise ConfigurationError("two migrations share a version", versions=versions)
    if versions and versions != list(range(1, len(versions) + 1)):
        raise ConfigurationError(
            "migration versions have to run from 1 with no gaps", versions=versions
        )
    return tuple(found)


def applied(connection: Connection) -> dict[int, str]:
    """The versions the database has already run, and their checksums."""
    connection.execute(_LEDGER)
    rows = connection.execute(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    return {int(row[0]): str(row[1]) for row in rows}


def pending(connection: Connection, directory: Path | None = None) -> tuple[Migration, ...]:
    """What is left to run, after checking nothing already run has changed."""
    known = applied(connection)
    everything = discover(directory)
    for migration in everything:
        recorded = known.get(migration.version)
        if recorded is not None and recorded != migration.checksum:
            raise ConfigurationError(
                "an already applied migration has been edited",
                filename=migration.filename,
                applied=recorded[:12],
                found=migration.checksum[:12],
            )
    return tuple(m for m in everything if m.version not in known)


def migrate(connection: Connection, directory: Path | None = None) -> tuple[Migration, ...]:
    """Apply everything outstanding and return what was applied."""
    outstanding = pending(connection, directory)
    done: list[Migration] = []
    for migration in outstanding:
        try:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at)"
                " VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        done.append(migration)
    return tuple(done)
