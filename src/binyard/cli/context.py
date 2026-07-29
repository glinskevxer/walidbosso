"""What every command needs: settings, a connection and somewhere to print.

The context is built once in :mod:`binyard.cli.main` and handed to whichever
command ran. Commands never reach for the environment, open a database or call
``print``, which is what lets the tests drive them without a filesystem and
without capturing stdout.

Output goes through :meth:`Context.emit`, which takes a table and renders it in
whichever format the user asked for. A command that formats its own output is a
command whose ``--format json`` quietly does nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TextIO

from binyard.config import Settings, load_settings
from binyard.errors import ConfigurationError
from binyard.reporting.table import Table, to_csv, to_json, to_text
from binyard.store.sqlite import SqliteUnitOfWork, connect

__all__ = ["Context", "OutputFormat", "build_context", "database_path"]


class OutputFormat(StrEnum):
    """How a table is rendered at the edge."""

    TEXT = "text"
    CSV = "csv"
    JSON = "json"


def database_path(database_url: str) -> Path | str:
    """Turn the configured URL into something :func:`sqlite3.connect` takes."""
    if database_url == ":memory:":
        return ":memory:"
    if database_url.startswith("sqlite:///"):
        return Path(database_url[len("sqlite:///") :])
    if database_url.startswith("sqlite://"):
        return Path(database_url[len("sqlite://") :])
    raise ConfigurationError("this build only speaks sqlite urls", database_url=database_url)


@dataclass(slots=True)
class Context:
    """One command's world."""

    settings: Settings
    connection: sqlite3.Connection
    out: TextIO
    err: TextIO
    output_format: OutputFormat = OutputFormat.TEXT
    today: date | None = None
    quiet: bool = False

    def work(self) -> SqliteUnitOfWork:
        return SqliteUnitOfWork(self.connection)

    @property
    def on(self) -> date:
        return self.today or date.today()

    def emit(self, table: Table) -> None:
        """Render one table in whichever format was asked for."""
        if self.output_format is OutputFormat.CSV:
            self.out.write(to_csv(table))
        elif self.output_format is OutputFormat.JSON:
            self.out.write(to_json(table) + "\n")
        else:
            self.out.write(to_text(table))

    def write(self, table: Table, path: str, *, overwrite: bool = False) -> None:
        """Send a table to a file instead of to the terminal."""
        from binyard.reporting.exports import Format, write_table

        written = write_table(
            table, path, Format(self.output_format.value), overwrite=overwrite
        )
        self.say(f"wrote {written.rows} rows to {written.path}")

    def emit_or_write(self, table: Table, path: str | None, *, overwrite: bool = False) -> None:
        if path is None:
            self.emit(table)
        else:
            self.write(table, path, overwrite=overwrite)

    def say(self, line: str) -> None:
        """A note for a human. Suppressed by ``--quiet`` and never in JSON."""
        if self.quiet or self.output_format is OutputFormat.JSON:
            return
        self.out.write(line + "\n")

    def warn(self, line: str) -> None:
        self.err.write(line + "\n")

    def close(self) -> None:
        self.connection.close()


def build_context(
    *,
    out: TextIO,
    err: TextIO,
    database: str | None = None,
    output_format: OutputFormat = OutputFormat.TEXT,
    environment: dict[str, str] | None = None,
    today: date | None = None,
    quiet: bool = False,
    run_migrations: bool = True,
) -> Context:
    """Assemble the context, applying the command line over the environment."""
    settings = load_settings(environment)
    url = database or settings.database_url
    connection = connect(database_path(url), run_migrations=run_migrations)
    return Context(
        settings=settings,
        connection=connection,
        out=out,
        err=err,
        output_format=output_format,
        today=today,
        quiet=quiet,
    )
