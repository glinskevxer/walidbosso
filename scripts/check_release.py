#!/usr/bin/env python3
"""Refuse to tag a tree that is not actually releasable.

Everything here has been wrong at least once in a package somewhere, and every
one of them is invisible until somebody installs the wheel:

**The version is in two places.** ``pyproject.toml`` and ``binyard.__version__``
have to agree, or ``binyard --version`` reports one thing and PyPI another.

**A module is not reachable.** A package that imports cleanly in the source
tree can still be missing from the wheel, so every module is imported by name
rather than trusted to be there.

**A migration has been edited.** The runner refuses one at startup, which is
the right time to find out about a mistake and the wrong time to find out about
a release.

**Something exported does not exist.** An ``__all__`` entry with a typo in it
is an ``AttributeError`` for whoever does ``from binyard.x import *``, and
nothing else catches it.

**The schema and the mappers disagree.** Every column the mapper writes has to
be a column the schema has.
"""

from __future__ import annotations

import argparse
import importlib
import pkgutil
import re
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import binyard  # noqa: E402
from binyard.store.migrate import discover, migrate  # noqa: E402

VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[.-]?(?:a|b|rc|dev)\d+)?$")


@dataclass(frozen=True)
class Problem:
    check: str
    detail: str

    def __str__(self) -> str:
        return f"{self.check}: {self.detail}"


def check_version_tuple() -> list[Problem]:
    """The three number view has to agree with the string."""
    parts = binyard.version_tuple()
    if ".".join(str(part) for part in parts) != binyard.__version__.split("-")[0]:
        return [
            Problem(
                "version",
                f"version_tuple says {parts}, the string says {binyard.__version__}",
            )
        ]
    return []


def check_version() -> list[Problem]:
    declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    problems: list[Problem] = []
    if declared != binyard.__version__:
        problems.append(
            Problem(
                "version",
                f"pyproject says {declared}, the package says {binyard.__version__}",
            )
        )
    if not VERSION.match(declared):
        problems.append(Problem("version", f"{declared} is not a version we can release"))
    return problems


def check_modules() -> list[Problem]:
    problems: list[Problem] = []
    found = 0
    for info in pkgutil.walk_packages(binyard.__path__, prefix="binyard."):
        found += 1
        try:
            importlib.import_module(info.name)
        except Exception as error:
            problems.append(Problem("import", f"{info.name}: {error}"))
    if found < 40:
        problems.append(Problem("import", f"only {found} modules found, that is too few"))
    return problems


def check_exports() -> list[Problem]:
    problems: list[Problem] = []
    for info in pkgutil.walk_packages(binyard.__path__, prefix="binyard."):
        try:
            module = importlib.import_module(info.name)
        except Exception:
            continue
        for name in getattr(module, "__all__", ()):
            if not hasattr(module, name):
                problems.append(Problem("exports", f"{info.name} exports missing {name}"))
    return problems


def check_migrations() -> list[Problem]:
    problems: list[Problem] = []
    try:
        found = discover()
    except Exception as error:
        return [Problem("migrations", str(error))]
    if not found:
        return [Problem("migrations", "there are no migrations at all")]

    connection = sqlite3.connect(":memory:")
    try:
        applied = migrate(connection)
        if len(applied) != len(found):
            problems.append(
                Problem(
                    "migrations",
                    f"{len(found)} on disk, {len(applied)} applied to a fresh database",
                )
            )
        again = migrate(connection)
        if again:
            problems.append(
                Problem("migrations", f"{len(again)} applied twice to the same database")
            )
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for expected in ("tickets", "bins", "movements", "obligations", "contracts"):
            if expected not in tables:
                problems.append(Problem("migrations", f"no {expected} table"))
    finally:
        connection.close()
    return problems


def check_schema_columns() -> list[Problem]:
    """Every column the ticket mapper writes has to exist in the schema."""
    connection = sqlite3.connect(":memory:")
    try:
        migrate(connection)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(tickets)")}
    finally:
        connection.close()

    from binyard.store.sqlite import SqliteTicketStore

    written = set(re.findall(r"\"([a-z_]+)\":", SqliteTicketStore._row.__doc__ or ""))
    source = (ROOT / "src/binyard/store/sqlite.py").read_text()
    block = source.partition("def _row(ticket: ScaleTicket)")[2].partition("@staticmethod")[0]
    written |= set(re.findall(r'"([a-z_]+)":', block))
    missing = sorted(name for name in written if name not in columns)
    if missing:
        return [Problem("schema", f"the ticket mapper writes {missing}, the schema has not")]
    return []


def check_console_script() -> list[Problem]:
    entries = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"].get("scripts", {})
    if "binyard" not in entries:
        return [Problem("scripts", "there is no binyard console script")]
    target = entries["binyard"]
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except Exception as error:
        return [Problem("scripts", f"{module_name} will not import: {error}")]
    if not hasattr(module, attribute):
        return [Problem("scripts", f"{target} does not exist")]
    return []


CHECKS = (
    ("version", check_version),
    ("version tuple", check_version_tuple),
    ("modules", check_modules),
    ("exports", check_exports),
    ("migrations", check_migrations),
    ("schema", check_schema_columns),
    ("console script", check_console_script),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args()

    problems: list[Problem] = []
    for name, check in CHECKS:
        found = check()
        problems.extend(found)
        if not arguments.quiet:
            mark = "ok" if not found else f"{len(found)} problems"
            print(f"{name:>16}: {mark}")

    for problem in problems:
        print(problem, file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} problems, this tree is not releasable", file=sys.stderr)
        return 1
    print(f"\nbinyard {binyard.__version__} looks releasable")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
