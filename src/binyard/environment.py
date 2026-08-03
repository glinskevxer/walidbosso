"""What is actually installed, against what this build was pinned to.

The pins in ``pyproject.toml`` say what should be there. This says what is,
and the two disagree more often than anybody expects: a developer with an older
virtualenv, a container built before the lock changed, a machine where
somebody pip installed something to get a script to run.

The check is reported rather than enforced. A version drift is worth knowing
about and is almost never worth refusing to start over, because refusing to
start means an elevator cannot weigh a truck. So ``/ready`` carries it, the
CLI can print it, and a human decides.

Python's own version is checked the other way round. Below the floor the
package genuinely will not work, so that one raises.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Final

from binyard.errors import ConfigurationError

__all__ = [
    "MINIMUM_PYTHON",
    "Installed",
    "check_python",
    "declared_pins",
    "drifted",
    "inspect_environment",
]

#: Below this the package uses syntax the interpreter does not have.
MINIMUM_PYTHON: Final = (3, 12)

#: Distributions this package can be asked about. Names as PyPI spells them.
WATCHED: Final = (
    "starlette",
    "uvicorn",
    "gunicorn",
    "httpx",
    "pytest",
    "hypothesis",
    "ruff",
    "mypy",
)


@dataclass(frozen=True, slots=True)
class Installed:
    """One distribution: what is pinned, and what is actually there."""

    name: str
    declared: str | None
    found: str | None

    @property
    def missing(self) -> bool:
        return self.found is None

    @property
    def drifted(self) -> bool:
        return (
            self.declared is not None and self.found is not None and self.declared != self.found
        )

    @property
    def note(self) -> str:
        if self.missing:
            return "not installed"
        if self.drifted:
            return f"pinned {self.declared}, installed {self.found}"
        return "as pinned"

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "declared": self.declared,
            "found": self.found,
            "drifted": self.drifted,
            "missing": self.missing,
            "note": self.note,
        }


def check_python() -> None:
    """Refuse to run on an interpreter too old for the syntax in here."""
    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise ConfigurationError(
            "this package needs a newer python",
            needs=".".join(str(part) for part in MINIMUM_PYTHON),
            running=".".join(str(part) for part in sys.version_info[:3]),
        )


def declared_pins() -> dict[str, str]:
    """The exact versions this build was pinned to, read off its own metadata.

    Read from the installed distribution rather than from ``pyproject.toml``,
    because in a container the source tree is not there and the metadata is.
    """
    pins: dict[str, str] = {}
    try:
        requirements = metadata.requires("binyard") or []
    except metadata.PackageNotFoundError:
        return pins
    for requirement in requirements:
        head = requirement.split(";")[0].strip()
        if "==" not in head:
            continue
        name, _, version = head.partition("==")
        pins[name.strip().lower()] = version.strip()
    return pins


def inspect_environment(names: tuple[str, ...] = WATCHED) -> tuple[Installed, ...]:
    """Compare the pins against what is importable, without importing anything."""
    pins = declared_pins()
    found: list[Installed] = []
    for name in names:
        key = name.lower()
        try:
            version: str | None = metadata.version(name)
        except metadata.PackageNotFoundError:
            version = None
        found.append(Installed(name=name, declared=pins.get(key), found=version))
    return tuple(found)


def drifted(names: tuple[str, ...] = WATCHED) -> tuple[Installed, ...]:
    """Only the ones that do not match, which is what a report wants."""
    return tuple(item for item in inspect_environment(names) if item.drifted)
