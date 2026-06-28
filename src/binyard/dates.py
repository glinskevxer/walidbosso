"""Reading dates the way this package means them, in one place.

Four call sites had grown their own version of "read a date off a query string
and blame the field if it will not parse", and they had drifted: two of them
said ``YYYY-MM-DD``, one said "an ISO date", and one raised a bare
``ValueError`` that came back as a 500.

There is one more thing here than parsing, and it is the reason the module
exists rather than being three lines in a utility file. **A date and a moment
are different questions.** A report for the fourteenth means everything up to
the end of the fourteenth, and a bin reading "as of" a timestamp means exactly
that instant. Converting between them is one function, :func:`end_of`, and
having it in one place is what stopped a report quietly cutting off at midnight
at the *start* of its own day.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from binyard.errors import ValidationError

__all__ = [
    "DAY_FORMAT",
    "days_between",
    "each_day",
    "end_of",
    "read_day",
    "read_moment",
    "start_of",
]

DAY_FORMAT = "YYYY-MM-DD"


def read_day(value: str | date | None, field: str, default: date | None = None) -> date:
    """Read a calendar date, blaming ``field`` when it will not parse."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValidationError("this field is required", field=field)
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(
            f"{field} is written {DAY_FORMAT}", field=field, got=value
        ) from exc


def read_moment(
    value: str | datetime | None, field: str, default: datetime | None = None
) -> datetime:
    """Read an instant, blaming ``field`` when it will not parse."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise ValidationError("this field is required", field=field)
        return default
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValidationError(f"{field} is an ISO timestamp", field=field, got=value) from exc


def start_of(day: date) -> datetime:
    """Midnight at the beginning of ``day``."""
    return datetime.combine(day, datetime.min.time())


def end_of(day: date) -> datetime:
    """The last instant of ``day``.

    Every report that reads "as of" a date wants this and not
    :func:`start_of`. A report built on the start of its own day shows the
    yard as it stood before any of that day happened, which is a subtly wrong
    answer that looks entirely reasonable.
    """
    return datetime.combine(day, datetime.max.time())


def days_between(first: date, last: date) -> int:
    """Whole days from ``first`` to ``last``, never negative."""
    if last < first:
        return 0
    return (last - first).days


def each_day(first: date, last: date):
    """Every date from ``first`` to ``last`` inclusive."""
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)
