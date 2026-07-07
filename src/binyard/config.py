"""Settings, read once from the environment and then frozen.

Everything here is something that differs between two installs of this package
and nothing here is something that differs between two loads. The location code
is settings. The moisture of the load in the pit is not.

Reading is strict. An unparseable ``BINYARD_STORAGE_RATE_CENTS`` stops the
process at startup with the name of the variable in the message, rather than
turning into a zero rate that nobody notices until the first storage invoice
comes out at nothing. Every value has a default that makes sense for a single
location country elevator, so a fresh checkout runs with no environment set at
all.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Final

from binyard.errors import ConfigurationError
from binyard.identity import LocationCode, crop_year_of
from binyard.money import Money, Price
from binyard.obligations.storage import Accrual, StorageTerms
from binyard.percent import Percent

__all__ = ["PREFIX", "Settings", "load_settings"]

PREFIX: Final = "BINYARD_"


@dataclass(frozen=True, slots=True)
class Settings:
    """One elevator's configuration."""

    location: LocationCode
    location_name: str
    crop_year: int
    database_url: str
    storage_terms: StorageTerms
    #: Fraction of a shipment that may go missing in transit without a query.
    transit_allowance: Percent
    #: Charged per point of moisture removed, per bushel.
    drying_rate_per_point: Price
    #: Minimum bushels worth turning a leg on for.
    minimum_bin_draw: Decimal
    #: Written on every ticket. Kept short because it prints on a narrow stub.
    house_name: str
    api_key: str | None = None
    max_request_bytes: int = 1_048_576
    #: Writes per minute per caller. Zero turns throttling off entirely.
    writes_per_minute: int = 240
    log_level: str = "INFO"
    debug: bool = False
    allowed_origins: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if not self.location_name.strip():
            raise ConfigurationError("the location needs a name")
        if not self.house_name.strip():
            raise ConfigurationError("the house needs a name")
        if not self.database_url.strip():
            raise ConfigurationError("a database url is required")
        if self.writes_per_minute < 0:
            raise ConfigurationError(
                "a write rate cannot be negative", rate=self.writes_per_minute
            )
        if self.max_request_bytes < 1024:
            raise ConfigurationError(
                "the request ceiling is too small to hold a ticket",
                bytes=self.max_request_bytes,
            )
        if self.minimum_bin_draw < 0:
            raise ConfigurationError("a minimum draw cannot be negative")
        if self.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("that is not a log level", level=self.log_level)

    @property
    def requires_api_key(self) -> bool:
        return self.api_key is not None

    def describe(self) -> dict[str, object]:
        """Everything except the key, which never gets printed anywhere."""
        return {
            "location": str(self.location),
            "location_name": self.location_name,
            "crop_year": self.crop_year,
            "house_name": self.house_name,
            "storage_rate": str(self.storage_terms.rate_per_bushel_month),
            "storage_accrual": self.storage_terms.accrual.value,
            "free_days": self.storage_terms.free_days,
            "transit_allowance": str(self.transit_allowance),
            "drying_rate_per_point": str(self.drying_rate_per_point),
            "minimum_bin_draw": str(self.minimum_bin_draw),
            "requires_api_key": self.requires_api_key,
            "writes_per_minute": self.writes_per_minute,
            "log_level": self.log_level.upper(),
            "debug": self.debug,
        }


def load_settings(environment: Mapping[str, str] | None = None, *, today=None) -> Settings:
    """Read settings out of the environment, falling back to sane defaults."""
    source = os.environ if environment is None else environment
    read = _Reader(source)

    from datetime import date

    when = today if today is not None else date.today()

    terms = StorageTerms(
        rate_per_bushel_month=Price.from_cents(read.decimal("STORAGE_RATE_CENTS", "4")),
        accrual=read.accrual("STORAGE_ACCRUAL", Accrual.PER_MONTH_STARTED),
        free_days=read.integer("STORAGE_FREE_DAYS", 30),
        minimum_charge=Money.of(read.decimal("STORAGE_MINIMUM", "0")),
    )

    return Settings(
        location=read.location("LOCATION", "WBS"),
        location_name=read.text("LOCATION_NAME", "West Branch Station"),
        crop_year=read.integer("CROP_YEAR", crop_year_of(when)),
        database_url=read.text("DATABASE_URL", "sqlite:///binyard.sqlite3"),
        storage_terms=terms,
        transit_allowance=Percent.of_points(read.decimal("TRANSIT_ALLOWANCE", "0.1")),
        drying_rate_per_point=Price.from_cents(read.decimal("DRYING_RATE_CENTS", "2")),
        minimum_bin_draw=read.decimal("MINIMUM_BIN_DRAW", "500"),
        house_name=read.text("HOUSE_NAME", "West Branch Grain"),
        api_key=read.optional("API_KEY"),
        max_request_bytes=read.integer("MAX_REQUEST_BYTES", 1_048_576),
        writes_per_minute=read.integer("WRITES_PER_MINUTE", 240),
        log_level=read.text("LOG_LEVEL", "INFO"),
        debug=read.flag("DEBUG", False),
        allowed_origins=read.csv("ALLOWED_ORIGINS"),
    )


class _Reader:
    """Pulls typed values out of a mapping and blames the variable by name."""

    def __init__(self, source: Mapping[str, str]) -> None:
        self._source = source

    def _raw(self, name: str) -> str | None:
        value = self._source.get(PREFIX + name)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def text(self, name: str, default: str) -> str:
        return self._raw(name) or default

    def optional(self, name: str) -> str | None:
        return self._raw(name)

    def integer(self, name: str, default: int) -> int:
        raw = self._raw(name)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError as exc:
            raise ConfigurationError(
                "expected a whole number", variable=PREFIX + name, got=raw
            ) from exc

    def decimal(self, name: str, default: str) -> Decimal:
        raw = self._raw(name)
        if raw is None:
            return Decimal(default)
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise ConfigurationError(
                "expected a number", variable=PREFIX + name, got=raw
            ) from exc

    def flag(self, name: str, default: bool) -> bool:
        raw = self._raw(name)
        if raw is None:
            return default
        lowered = raw.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigurationError("expected a yes or a no", variable=PREFIX + name, got=raw)

    def csv(self, name: str) -> tuple[str, ...]:
        raw = self._raw(name)
        if raw is None:
            return ()
        return tuple(part.strip() for part in raw.split(",") if part.strip())

    def location(self, name: str, default: str) -> LocationCode:
        raw = self._raw(name) or default
        try:
            return LocationCode.parse(raw)
        except Exception as exc:
            raise ConfigurationError(
                "that is not a location code", variable=PREFIX + name, got=raw
            ) from exc

    def accrual(self, name: str, default: Accrual) -> Accrual:
        raw = self._raw(name)
        if raw is None:
            return default
        try:
            return Accrual(raw.lower())
        except ValueError as exc:
            raise ConfigurationError(
                "that is not a storage accrual",
                variable=PREFIX + name,
                got=raw,
                allowed=[a.value for a in Accrual],
            ) from exc
