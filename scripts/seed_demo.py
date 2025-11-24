#!/usr/bin/env python3
"""Build a yard with a plausible harvest in it.

A fresh checkout has an empty database, and an empty database cannot answer any
of the questions this package exists to answer. This script fills one: five
bins, three growers, two contracts and six weeks of loads with the moisture
drifting down the way it does as a harvest dries out.

It is deterministic. The same seed gives the same yard, which matters because
the numbers in the README and in a demo have to keep matching.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from binyard import commodity
from binyard.bushels import bushels
from binyard.contracts.contract import PriceKind, PurchaseContract
from binyard.discounts.posted import default_book
from binyard.errors import BinyardError
from binyard.grading.factors import FactorSample
from binyard.identity import LocationCode, Sequence
from binyard.inventory.bin import Bin, BinKind
from binyard.money import Price
from binyard.percent import percent
from binyard.services.receiving import ReceiptRequest, receive_load
from binyard.store.sqlite import SqliteUnitOfWork, connect
from binyard.tickets.weights import ScaleWeights
from binyard.units import pounds

LOCATION = LocationCode("WBS")
HARVEST_START = date(2025, 9, 22)
GROWERS = ("HALVORSEN FARMS", "REDDING BROTHERS", "OKSNEVAD LAND CO")

BINS = (
    ("N1", "North 1", BinKind.UPRIGHT, "180000", frozenset()),
    ("N2", "North 2", BinKind.UPRIGHT, "180000", frozenset()),
    ("S1", "South 1", BinKind.UPRIGHT, "95000", frozenset({"SOYBEANS"})),
    ("F1", "Flat house", BinKind.FLAT, "260000", frozenset()),
    ("P1", "West pile", BinKind.PILE, "400000", frozenset({"CORN"})),
)


def build_bins():
    for identifier, name, kind, capacity, only in BINS:
        yield Bin(
            identifier=identifier,
            name=name,
            kind=kind,
            capacity=bushels(capacity),
            allowed_commodities=only,
            pile_allowance=percent("1.0") if kind is BinKind.PILE else percent("0"),
        )


def contracts() -> list[PurchaseContract]:
    return [
        PurchaseContract(
            identifier="C-2025-014",
            account="HALVORSEN FARMS",
            commodity_code="CORN",
            quantity=bushels("40000"),
            delivery_from=HARVEST_START,
            delivery_to=date(2025, 12, 31),
            price_kind=PriceKind.FLAT,
            flat_price=Price.of("4.5225"),
            written_on=date(2025, 6, 3),
        ),
        PurchaseContract(
            identifier="C-2025-021",
            account="REDDING BROTHERS",
            commodity_code="SOYBEANS",
            quantity=bushels("18000"),
            delivery_from=HARVEST_START,
            delivery_to=date(2025, 11, 30),
            price_kind=PriceKind.BASIS,
            basis=Price.of("-0.35"),
            futures_month="SX25",
            written_on=date(2025, 7, 18),
        ),
    ]


def sample_for(standard, rng: random.Random, day_index: int) -> FactorSample:
    """Moisture drifts down over the season, damage drifts up a little."""
    if standard.code == "CORN":
        moisture = Decimal("21.5") - Decimal(day_index) * Decimal("0.18")
        floor, test_weight = Decimal("14.2"), Decimal("57.5")
    else:
        moisture = Decimal("14.8") - Decimal(day_index) * Decimal("0.09")
        floor, test_weight = Decimal("11.5"), Decimal("56.5")
    moisture = max(moisture, floor) + Decimal(str(rng.choice(["-0.4", "0.0", "0.3", "0.7"])))
    return FactorSample(
        moisture=percent(f"{moisture:.1f}"),
        test_weight=test_weight + Decimal(str(rng.choice(["-1.5", "-0.5", "0.0", "0.5"]))),
        total_damage=percent(rng.choice(["0.4", "1.1", "2.3", "3.8"])),
        heat_damage=percent("0.0"),
        broken=percent(rng.choice(["0.6", "1.4", "2.2"])),
        foreign_material=percent(rng.choice(["0.2", "0.6", "1.1"])),
    )


def seed(database: Path, *, loads: int, seed_value: int) -> dict[str, object]:
    rng = random.Random(seed_value)
    work = SqliteUnitOfWork(connect(database))
    yard = work.yard.load()
    for structure in build_bins():
        yard = yard.with_bin(structure)
    work.yard.save(yard)
    for contract in contracts():
        work.contracts.add(contract)
    work.commit()

    book = default_book()
    sequence = Sequence(LOCATION, 2025)
    taken = 0
    refused = 0

    for index in range(loads):
        day_index = index // 6
        day = HARVEST_START + timedelta(days=day_index)
        standard = commodity.CORN if index % 4 else commodity.SOYBEANS
        destination = "S1" if standard.code == "SOYBEANS" else rng.choice(["N1", "N2", "F1"])
        net = rng.randrange(44_000, 50_000, 20)
        request = ReceiptRequest(
            account=GROWERS[index % len(GROWERS)],
            standard=standard,
            weights=ScaleWeights(gross=pounds(str(30_180 + net)), tare=pounds("30180")),
            sample=sample_for(standard, rng, day_index),
            bin_identifier=destination,
            weighed_on=day,
            at=datetime.combine(day, datetime.min.time()) + timedelta(hours=7 + index % 10),
            by="seed",
        )
        try:
            receive_load(work, sequence, request, book=book)
            taken += 1
        except BinyardError:
            work.rollback()
            refused += 1

    yard = work.yard.load()
    return {
        "bins": len(yard.bins),
        "loads_taken": taken,
        "loads_refused": refused,
        "corn": str(yard.total_of("CORN")),
        "soybeans": str(yard.total_of("SOYBEANS")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="binyard.sqlite3")
    parser.add_argument("--loads", type=int, default=180)
    parser.add_argument("--seed", type=int, default=20250922)
    parser.add_argument("--force", action="store_true", help="delete the file first")
    arguments = parser.parse_args()

    database = Path(arguments.database)
    if database.exists():
        if not arguments.force:
            print(f"{database} already exists, pass --force to replace it")
            return 1
        database.unlink()

    summary = seed(database, loads=arguments.loads, seed_value=arguments.seed)
    for key, value in summary.items():
        print(f"{key:>14}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
