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
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
