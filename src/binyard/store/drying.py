"""Storing dryer runs.

Fuel goes in as thousandths of a gallon, which is the same trick every other
quantity in this schema uses and for the same reason: the derived figure people
actually look at, gallons per point per bushel, is small enough that a whole
gallon column would round it to nothing useful.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from binyard.bushels import Bushels
from binyard.drying.run import DryerRun, FuelUse, RunStatus
from binyard.errors import Conflict, NotFound
from binyard.money import Price
from binyard.percent import Percent
from binyard.store.mappers import as_datetime, sample_from_json, sample_to_json

__all__ = ["DryerStore", "MemoryDryerStore", "SqliteDryerStore"]

THOUSANDTHS = Decimal(1000)


class DryerStore:
    """The interface, written as a base class so both backends read the same."""

    def add(self, run: DryerRun) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def get(self, identifier: str) -> DryerRun:  # pragma: no cover - interface
        raise NotImplementedError

    def exists(self, identifier: str) -> bool:  # pragma: no cover - interface
        raise NotImplementedError

    def replace(self, run: DryerRun) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def open_runs(self) -> tuple[DryerRun, ...]:  # pragma: no cover - interface
        raise NotImplementedError

    def save(self, run: DryerRun) -> None:
        if self.exists(run.identifier):
            self.replace(run)
        else:
            self.add(run)


class SqliteDryerStore(DryerStore):
    """Dryer runs in one table, with fuel in thousandths of a gallon."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def add(self, run: DryerRun) -> None:
        if self.exists(run.identifier):
            raise Conflict("that run identifier is already used", identifier=run.identifier)
        self._write(run, insert=True)

    def get(self, identifier: str) -> DryerRun:
        row = self._connection.execute(
            "SELECT * FROM dryer_runs WHERE identifier = ?", (identifier,)
        ).fetchone()
        if row is None:
            raise NotFound("dryer run", identifier)
        return self._run(row)

    def exists(self, identifier: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM dryer_runs WHERE identifier = ?", (identifier,)
            ).fetchone()
            is not None
        )

    def replace(self, run: DryerRun) -> None:
        if not self.exists(run.identifier):
            raise NotFound("dryer run", run.identifier)
        self._write(run, insert=False)

    def open_runs(self) -> tuple[DryerRun, ...]:
        rows = self._connection.execute(
            "SELECT * FROM dryer_runs WHERE status IN (?, ?) ORDER BY started_at",
            (RunStatus.LOADED.value, RunStatus.DRYING.value),
        ).fetchall()
        return tuple(self._run(row) for row in rows)

    def _write(self, run: DryerRun, *, insert: bool) -> None:
        values = (
            run.commodity_code,
            run.source_bin,
            run.destination_bin,
            run.wet_bushels.hundredths,
            run.wet_moisture.hundredths,
            run.started_at.isoformat(),
            run.status.value,
            None if run.dry_bushels is None else run.dry_bushels.hundredths,
            None if run.dry_moisture is None else run.dry_moisture.hundredths,
            None if run.finished_at is None else run.finished_at.isoformat(),
            None if run.fuel is None else int(run.fuel.gallons * THOUSANDTHS),
            None if run.fuel is None else run.fuel.price_per_gallon.ticks,
            run.note,
            sample_to_json(run.wet_quality),
            run.identifier,
        )
        if insert:
            self._connection.execute(
                "INSERT INTO dryer_runs (commodity_code, source_bin, destination_bin,"
                " wet_hundredths, wet_moisture, started_at, status, dry_hundredths,"
                " dry_moisture, finished_at, fuel_thousandths, fuel_price_ticks, note,"
                " wet_quality_json, identifier)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
        else:
            self._connection.execute(
                "UPDATE dryer_runs SET commodity_code = ?, source_bin = ?,"
                " destination_bin = ?, wet_hundredths = ?, wet_moisture = ?,"
                " started_at = ?, status = ?, dry_hundredths = ?, dry_moisture = ?,"
                " finished_at = ?, fuel_thousandths = ?, fuel_price_ticks = ?, note = ?,"
                " wet_quality_json = ? WHERE identifier = ?",
                values,
            )

    @staticmethod
    def _run(row: sqlite3.Row) -> DryerRun:
        mapping = dict(row)
        started = as_datetime(mapping["started_at"])
        assert started is not None
        fuel = None
        if mapping["fuel_thousandths"] is not None:
            fuel = FuelUse(
                gallons=Decimal(mapping["fuel_thousandths"]) / THOUSANDTHS,
                price_per_gallon=Price(int(mapping["fuel_price_ticks"] or 0)),
            )
        return DryerRun(
            identifier=mapping["identifier"],
            commodity_code=mapping["commodity_code"],
            source_bin=mapping["source_bin"],
            destination_bin=mapping["destination_bin"],
            wet_bushels=Bushels(int(mapping["wet_hundredths"])),
            wet_moisture=Percent(int(mapping["wet_moisture"])),
            started_at=started,
            status=RunStatus(mapping["status"]),
            dry_bushels=None
            if mapping["dry_hundredths"] is None
            else Bushels(int(mapping["dry_hundredths"])),
            dry_moisture=None
            if mapping["dry_moisture"] is None
            else Percent(int(mapping["dry_moisture"])),
            finished_at=as_datetime(mapping["finished_at"]),
            fuel=fuel,
            note=mapping["note"],
            wet_quality=sample_from_json(mapping["wet_quality_json"]),
        )


class MemoryDryerStore(DryerStore):
    """Dryer runs in a dictionary, keyed by run identifier."""

    def __init__(self, runs: dict[str, DryerRun] | None = None) -> None:
        self._runs: dict[str, DryerRun] = dict(runs or {})

    def add(self, run: DryerRun) -> None:
        if run.identifier in self._runs:
            raise Conflict("that run identifier is already used", identifier=run.identifier)
        self._runs[run.identifier] = run

    def get(self, identifier: str) -> DryerRun:
        try:
            return self._runs[identifier]
        except KeyError:
            raise NotFound("dryer run", identifier) from None

    def exists(self, identifier: str) -> bool:
        return identifier in self._runs

    def replace(self, run: DryerRun) -> None:
        if run.identifier not in self._runs:
            raise NotFound("dryer run", run.identifier)
        self._runs[run.identifier] = run

    def open_runs(self) -> tuple[DryerRun, ...]:
        return tuple(
            run
            for run in sorted(self._runs.values(), key=lambda item: item.started_at)
            if run.is_open
        )

    def snapshot(self) -> dict[str, DryerRun]:
        return dict(self._runs)

    def restore(self, snapshot: dict[str, DryerRun]) -> None:
        self._runs = dict(snapshot)
