"""The dryer over HTTP.

Three endpoints and they are three because a run is three separate events with
hours between them. Lighting the dryer takes grain out of a bin; finishing it
puts grain into another; and between the two the bushels belong to the run.

``GET /dryer`` answers the question the operator asks at shift change, which is
what is on the dryer right now and how long it has been there.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.api.parsing import reader_for
from binyard.commodity import lookup
from binyard.drying.run import FuelUse
from binyard.money import Price
from binyard.services.drying import abandon_run, finish_run, start_run
from binyard.shrink import ShrinkPolicy

__all__ = ["routes"]


def routes() -> list[Route]:
    return [
        Route("/dryer", listing, methods=["GET"], name="list_runs"),
        Route("/dryer", start, methods=["POST"], name="start_run"),
        Route("/dryer/{identifier}", show, methods=["GET"], name="show_run"),
        Route("/dryer/{identifier}/finish", finish, methods=["POST"], name="finish_run"),
        Route("/dryer/{identifier}/abandon", abandon, methods=["POST"], name="abandon_run"),
    ]


async def listing(request: Request) -> JSONResponse:
    work = work_for(request)
    try:
        runs = work.dryer.open_runs()
        return JSONResponse({"runs": [run.describe() for run in runs], "count": len(runs)})
    finally:
        work.close()


async def show(request: Request) -> JSONResponse:
    work = work_for(request)
    try:
        run = work.dryer.get(request.path_params["identifier"])
        policy = ShrinkPolicy.market_default(lookup(run.commodity_code))
        return JSONResponse(run.describe(policy))
    finally:
        work.close()


async def start(request: Request) -> JSONResponse:
    reader = reader_for(await request.json())
    work = work_for(request)
    try:
        run = start_run(
            work,
            identifier=reader.text("identifier"),
            source_bin=reader.text("from"),
            destination_bin=reader.text("to"),
            quantity=reader.bushels("quantity"),
            at=reader.timestamp("at", datetime.now().isoformat()),
            by=reader.text("by", "api"),
        )
        return JSONResponse(run.describe(), status_code=201)
    finally:
        work.close()


async def finish(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    reader = reader_for(await request.json())
    gallons = reader.optional_text("fuel_gallons")
    fuel_price = reader.optional_text("fuel_price")
    fuel = (
        FuelUse(
            gallons=Decimal(gallons),
            price_per_gallon=Price.of(fuel_price or "0"),
        )
        if gallons is not None
        else None
    )

    work = work_for(request)
    try:
        run = work.dryer.get(identifier)
        standard = lookup(run.commodity_code)
        outcome = finish_run(
            work,
            identifier=identifier,
            dry_bushels=reader.bushels("dry_bushels"),
            dry_moisture=reader.percent("dry_moisture"),
            standard=standard,
            at=reader.timestamp("at", datetime.now().isoformat()),
            by=reader.text("by", "api"),
            fuel=fuel,
        )
        return JSONResponse(outcome.describe(ShrinkPolicy.market_default(standard)))
    finally:
        work.close()


async def abandon(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    reader = reader_for(await request.json())
    work = work_for(request)
    try:
        run = abandon_run(
            work,
            identifier=identifier,
            reason=reader.text("reason"),
            dry_bushels=reader.bushels("dry_bushels"),
            dry_moisture=reader.percent("dry_moisture"),
            at=reader.timestamp("at", datetime.now().isoformat()),
            by=reader.text("by", "api"),
        )
        return JSONResponse(run.describe())
    finally:
        work.close()
