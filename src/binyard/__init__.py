"""binyard: grain handling accounting for a country elevator.

The package is layered so the parts that decide numbers never reach for a
database or a socket:

``binyard.units``       weights, moisture, test weight, bushel conversion
``binyard.commodity``   what the elevator handles and the standards per crop
``binyard.grading``     factor tables and grade determination
``binyard.discounts``   the posted schedules applied to a graded lot
``binyard.tickets``     scale tickets and their lifecycle
``binyard.inventory``   bins, commingling and the movement ledger
``binyard.obligations`` who owns the bushels sitting in the bins
``binyard.blend``       picking bins that satisfy a contract spec
``binyard.loadout``     shipping the result and reconciling it

Everything above is pure. ``binyard.store``, ``binyard.services``,
``binyard.api`` and ``binyard.cli`` sit on top and are the only places that
touch the outside world.

Version one means the shapes below are settled. The schema moves forward by
migration and never sideways, the exact types keep their units, and a caller
holding a :class:`~binyard.tickets.ticket.ScaleTicket` from this release will
still be able to read it from the next one.
"""

from binyard.environment import MINIMUM_PYTHON, check_python
from binyard.errors import BinyardError

__all__ = [
    "MINIMUM_PYTHON",
    "BinyardError",
    "__version__",
    "check_python",
    "version_tuple",
]

__version__ = "1.0.0"


def version_tuple() -> tuple[int, int, int]:
    """The version as three numbers, for a caller comparing releases."""
    major, minor, patch = __version__.split(".", 2)
    return int(major), int(minor), int(patch.split("-")[0].split("a")[0].split("b")[0])
