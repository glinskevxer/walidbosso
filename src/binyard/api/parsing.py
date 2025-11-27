"""Reading a JSON body into the domain's own types.

There is no schema library in here and that is a decision, not an omission.
Every value this API takes already has a parser on the type that owns it:
:func:`binyard.units.parse_weight` reads a weight, :meth:`Percent.of_points`
reads a percentage, :func:`binyard.commodity.lookup` reads a crop. A schema
layer would restate all of those as strings and floats and then convert, which
means two places to change when a rule moves and one of them will be missed.

So the reader below is thin. It pulls a field out of a mapping, says which
field when it is missing or the wrong shape, and hands the raw value to the
type that knows what it means. Anything the domain refuses comes back as the
domain's own :class:`ValidationError` with its own message, which is almost
always better than "value is not a valid decimal".

Field paths are tracked so a nested body reports ``sample.moisture`` rather
than ``moisture``, because the caller is looking at their own JSON and needs to
find it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, TypeVar

from binyard.bushels import Bushels
from binyard.commodity import CropStandard, lookup
from binyard.errors import ValidationError
from binyard.percent import Percent
from binyard.units import Weight, parse_weight

__all__ = ["Reader", "reader_for"]

E = TypeVar("E", bound=StrEnum)

_MISSING = object()


class Reader:
    """Pulls typed values out of a decoded JSON object."""

    __slots__ = ("_body", "_path")

    def __init__(self, body: Any, path: str = "") -> None:
        if not isinstance(body, Mapping):
            raise ValidationError(
                "expected a JSON object",
                field=path or "body",
                got=type(body).__name__,
            )
        self._body = body
        self._path = path

    def _where(self, name: str) -> str:
        return f"{self._path}.{name}" if self._path else name

    def _raw(self, name: str, default: Any = _MISSING) -> Any:
        if name in self._body:
            return self._body[name]
        if default is _MISSING:
            raise ValidationError("this field is required", field=self._where(name))
        return default

    # -- plain types -----------------------------------------------------

    def text(self, name: str, default: str | None = _MISSING) -> str:  # type: ignore[assignment]
        value = self._raw(name, default)
        if not isinstance(value, str):
            raise ValidationError(
                "expected a string", field=self._where(name), got=type(value).__name__
            )
        stripped = value.strip()
        if not stripped:
            raise ValidationError("this field cannot be blank", field=self._where(name))
        return stripped

    def optional_text(self, name: str) -> str | None:
        value = self._raw(name, None)
        if value is None:
            return None
        return self.text(name)

    def integer(self, name: str, default: int | None = _MISSING) -> int:  # type: ignore[assignment]
        value = self._raw(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(
                "expected a whole number", field=self._where(name), got=repr(value)
            )
        return value

    def flag(self, name: str, default: bool = False) -> bool:
        value = self._raw(name, default)
        if not isinstance(value, bool):
            raise ValidationError(
                "expected true or false", field=self._where(name), got=repr(value)
            )
        return value

    def decimal(self, name: str, default: Any = _MISSING) -> Decimal:
        value = self._raw(name, default)
        if isinstance(value, bool):
            raise ValidationError("expected a number", field=self._where(name))
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(
                "expected a number", field=self._where(name), got=repr(value)
            ) from exc

    def day(self, name: str, default: Any = _MISSING) -> date:
        value = self._raw(name, default)
        if isinstance(value, date):
            return value
        try:
            return date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError(
                "expected a date as YYYY-MM-DD", field=self._where(name), got=repr(value)
            ) from exc

    def timestamp(self, name: str, default: Any = _MISSING) -> datetime:
        value = self._raw(name, default)
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError as exc:
            raise ValidationError(
                "expected an ISO timestamp", field=self._where(name), got=repr(value)
            ) from exc

    def choice(self, name: str, options: type[E], default: Any = _MISSING) -> E:
        value = self._raw(name, default)
        if isinstance(value, options):
            return value
        try:
            return options(str(value))
        except ValueError as exc:
            raise ValidationError(
                "not one of the allowed values",
                field=self._where(name),
                got=repr(value),
                allowed=[item.value for item in options],
            ) from exc

    # -- domain types ----------------------------------------------------

    def weight(self, name: str, default: Any = _MISSING) -> Weight:
        value = self._raw(name, default)
        return self._blamed(name, lambda: parse_weight(str(value)))

    def bushels(self, name: str, default: Any = _MISSING) -> Bushels:
        value = self._raw(name, default)
        return self._blamed(name, lambda: Bushels.of(str(value)))

    def percent(self, name: str, default: Any = _MISSING) -> Percent:
        value = self._raw(name, default)
        return self._blamed(name, lambda: Percent.of_points(str(value)))

    def commodity(self, name: str, default: Any = _MISSING) -> CropStandard:
        value = self._raw(name, default)
        return self._blamed(name, lambda: lookup(str(value)))

    # -- structure -------------------------------------------------------

    def nested(self, name: str) -> Reader:
        return Reader(self._raw(name), self._where(name))

    def optional_nested(self, name: str) -> Reader | None:
        value = self._raw(name, None)
        return None if value is None else Reader(value, self._where(name))

    def each(self, name: str) -> tuple[Reader, ...]:
        value = self._raw(name)
        if not isinstance(value, list):
            raise ValidationError(
                "expected a list", field=self._where(name), got=type(value).__name__
            )
        return tuple(
            Reader(item, f"{self._where(name)}[{index}]") for index, item in enumerate(value)
        )

    def strings(self, name: str, default: Any = _MISSING) -> tuple[str, ...]:
        value = self._raw(name, default)
        if value is None:
            return ()
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValidationError("expected a list of strings", field=self._where(name))
        return tuple(item.strip() for item in value if item.strip())

    def unexpected(self, *known: str) -> tuple[str, ...]:
        """Fields the caller sent that this endpoint does not read."""
        return tuple(sorted(set(self._body) - set(known)))

    def _blamed(self, name: str, build):
        try:
            return build()
        except ValidationError as error:
            raise ValidationError(
                error.message, field=self._where(name), **error.detail
            ) from None


def reader_for(body: Any) -> Reader:
    return Reader(body)
