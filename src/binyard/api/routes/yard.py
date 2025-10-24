"""Bins and what is in them.

The read endpoints answer as of a moment when one is given, which is what makes
this useful to anybody reconciling. ``GET /bins/N1?when=2025-11-12T08:00:00``
folds the ledger to that instant and reports what the bin held, and the answer
is derived from the movements rather than looked up in a snapshot table, so it
cannot disagree with the ledger.

Writes are narrow on purpose. A bin can be registered and grain can be
transferred or adjusted, and that is all. Receiving is not here; it is a whole
transaction and it lives on ``POST /tickets`` where the ticket is written in
the same breath.
"""

from __future__ import annotations

from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.api.parsing import reader_for
from binyard.errors import ValidationError
from binyard.inventory.bin import Bin, BinKind
from binyard.reporting.reports import bin_table

__all__ = ["routes"]


def routes() -> list[Route]:
    return [
        Route("/bins", listing, methods=["GET"], name="list_bins"),
        Route("/bins", create, methods=["POST"], name="create_bin"),
        Route("/bins/{identifier}", show, methods=["GET"], name="show_bin"),
        Route("/bins/{identifier}/transfer", transfer, methods=["POST"], name="transfer"),
        Route("/bins/{identifier}/adjust", adjust, methods=["POST"], name="adjust"),
    ]


def _when(request: Request) -> datetime | None:
    raw = request.query_params.get("when")
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("when is an ISO timestamp", got=raw) from exc


async def listing(request: Request) -> JSONResponse:
    when = _when(request)
    work = work_for(request)
    try:
        yard = work.yard.load()
        states = yard.states(when=when)
        return JSONResponse(
            {
                "as_of": None if when is None else when.isoformat(),
                "bins": [state.describe() for state in states],
                "table": bin_table(yard).title,
            }
        )
    finally:
        work.close()


async def show(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    when = _when(request)
    work = work_for(request)
    try:
        state = work.yard.load().state_of(identifier, when=when)
        payload = state.describe()
        payload["as_of"] = None if when is None else when.isoformat()
        return JSONResponse(payload)
    finally:
        work.close()


async def create(request: Request) -> JSONResponse:
    reader = reader_for(await request.json())
    structure = Bin(
        identifier=reader.text("identifier"),
        name=reader.text("name"),
        kind=reader.choice("kind", BinKind, BinKind.UPRIGHT),
        capacity=reader.bushels("capacity"),
        allowed_commodities=frozenset(
            item.upper() for item in reader.strings("only", [])
        ),
        aerated=reader.flag("aerated", True),
        pile_allowance=reader.percent("pile_allowance", "0"),
    )
    work = work_for(request)
    try:
        yard = work.yard.load()
        work.yard.save(yard.with_bin(structure))
        work.commit()
        return JSONResponse(
            work.yard.load().state_of(structure.identifier).describe(), status_code=201
        )
    finally:
        work.close()


async def transfer(request: Request) -> JSONResponse:
    source = request.path_params["identifier"]
    reader = reader_for(await request.json())
    destination = reader.text("to")
    quantity = reader.bushels("quantity")
    work = work_for(request)
    try:
        yard = work.yard.load().transfer(
            source,
            destination,
            quantity,
            at=datetime.now(),
            reference=reader.text("reference"),
            by=reader.text("by", "api"),
        )
        work.yard.save(yard)
        work.commit()
        return JSONResponse(
            {
                "from": yard.state_of(source).describe(),
                "to": yard.state_of(destination).describe(),
            }
        )
    finally:
        work.close()


async def adjust(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    reader = reader_for(await request.json())
    quantity = reader.bushels("quantity")
    work = work_for(request)
    try:
        yard = work.yard.load().adjust(
            identifier,
            quantity,
            reader.text("reason"),
            at=datetime.now(),
            reference=reader.text("reference"),
            by=reader.text("by", "api"),
        )
        work.yard.save(yard)
        work.commit()
        return JSONResponse(yard.state_of(identifier).describe())
    finally:
        work.close()
