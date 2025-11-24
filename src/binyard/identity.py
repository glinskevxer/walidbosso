"""Identifiers, and the sequences that hand them out.

A ticket number is not a database key. It is printed on paper, read aloud over
a radio, and quoted back three months later by someone holding a cheque stub.
That means it has to be short, has to sort, and has to be unique per location
rather than globally, because two elevators in the same company both start at
one every season and neither is going to change.

The shape is ``LOC-YY-NNNNNN``: the location code, the two digit crop year, and
a six digit sequence. Parsing is strict. A ticket number that arrives from the
scale house feed with the wrong shape is a data problem to be raised at the
door, not something to normalise quietly and discover in a settlement run.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date
from typing import Final

from binyard.errors import ValidationError

__all__ = [
    "LocationCode",
    "Sequence",
    "TicketNumber",
    "crop_year_of",
    "crop_year_suffix",
]

#: Crop year rolls at the start of September, before harvest reaches the pit.
CROP_YEAR_START_MONTH: Final = 9

_LOCATION = re.compile(r"^[A-Z]{2,4}$")
_TICKET = re.compile(r"^(?P<location>[A-Z]{2,4})-(?P<year>\d{2})-(?P<sequence>\d{6})$")


@dataclass(frozen=True, order=True, slots=True)
class LocationCode:
    """A short code for one physical elevator, uppercase and two to four letters."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str) or not _LOCATION.match(self.value):
            raise ValidationError(
                "a location code is two to four uppercase letters", got=repr(self.value)
            )

    @classmethod
    def parse(cls, text: str) -> LocationCode:
        return cls(text.strip().upper())

    def __str__(self) -> str:
        return self.value


def crop_year_of(day: date) -> int:
    """The crop year a day falls in, rolling on 1 September."""
    return day.year if day.month >= CROP_YEAR_START_MONTH else day.year - 1


def crop_year_suffix(year: int) -> str:
    if not (1900 <= year <= 2999):
        raise ValidationError("that is not a crop year we can print", year=year)
    return f"{year % 100:02d}"


@dataclass(frozen=True, order=True, slots=True)
class TicketNumber:
    """``LOC-YY-NNNNNN``, the thing printed on the paper."""

    location: LocationCode
    crop_year: int
    sequence: int

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise ValidationError("a ticket sequence is an integer", got=repr(self.sequence))
        if not (1 <= self.sequence <= 999_999):
            raise ValidationError("a ticket sequence runs from 1 to 999999", got=self.sequence)
        crop_year_suffix(self.crop_year)

    @classmethod
    def parse(cls, text: str) -> TicketNumber:
        match = _TICKET.match(text.strip().upper())
        if match is None:
            raise ValidationError("that is not a ticket number", got=text)
        two_digit = int(match.group("year"))
        return cls(
            location=LocationCode(match.group("location")),
            crop_year=2000 + two_digit,
            sequence=int(match.group("sequence")),
        )

    def __str__(self) -> str:
        return f"{self.location}-{crop_year_suffix(self.crop_year)}-{self.sequence:06d}"

    def next(self) -> TicketNumber:
        return TicketNumber(self.location, self.crop_year, self.sequence + 1)


class Sequence:
    """A per location, per crop year counter that hands out ticket numbers.

    Thread safe because the scale house runs two lanes and both call it. The
    persistent version lives in the store; this one is the in process default
    and the thing the tests use.
    """

    def __init__(self, location: LocationCode, crop_year: int, start: int = 0) -> None:
        if start < 0:
            raise ValidationError("a sequence cannot start below zero", start=start)
        self._location = location
        self._crop_year = crop_year
        self._issued = start
        self._lock = threading.Lock()

    @property
    def issued(self) -> int:
        return self._issued

    @property
    def location(self) -> LocationCode:
        return self._location

    @property
    def crop_year(self) -> int:
        return self._crop_year

    def next(self) -> TicketNumber:
        with self._lock:
            self._issued += 1
            return TicketNumber(self._location, self._crop_year, self._issued)

    def reserve(self, count: int) -> tuple[TicketNumber, ...]:
        """Take a block at once, for a feed import that has to keep its own order."""
        if count < 1:
            raise ValidationError("cannot reserve fewer than one number", count=count)
        with self._lock:
            first = self._issued + 1
            self._issued += count
            return tuple(
                TicketNumber(self._location, self._crop_year, n)
                for n in range(first, first + count)
            )

    def advance_past(self, number: TicketNumber) -> None:
        """Move the counter up so a restored ticket cannot be issued twice."""
        if number.location != self._location or number.crop_year != self._crop_year:
            raise ValidationError(
                "that ticket belongs to a different sequence",
                ticket=str(number),
                sequence=f"{self._location}-{crop_year_suffix(self._crop_year)}",
            )
        with self._lock:
            self._issued = max(self._issued, number.sequence)
