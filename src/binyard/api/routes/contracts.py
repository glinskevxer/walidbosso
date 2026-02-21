"""Purchase contracts over HTTP.

A contract can be written here, and its futures leg fixed here, and that is
all. Deliveries land against it through ``POST /tickets``, which is the only
place a delivery has a ticket behind it.

Fixing the futures leg is a POST rather than a PATCH because it is an event
with a date and a price, not a field being corrected. The distinction shows up
the first time somebody asks what the basis was on the day it was set.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.api.parsing import reader_for
from binyard.contracts.contract import PriceKind, PurchaseContract
from binyard.errors import ValidationError
from binyard.money import Price

__all__ = ["routes"]


def routes() -> list[Route]:
    return [
        Route("/contracts", listing, methods=["GET"], name="list_contracts"),
        Route("/contracts", create, methods=["POST"], name="create_contract"),
        Route("/contracts/{identifier}", show, methods=["GET"], name="show_contract"),
        Route(
            "/contracts/{identifier}/futures",
            fix_futures,
            methods=["POST"],
            name="fix_futures",
        ),
    ]


async def listing(request: Request) -> JSONResponse:
    account = request.query_params.get("account")
    crop = request.query_params.get("commodity")
    on = request.query_params.get("on")
    if not (account and crop and on):
        raise ValidationError("listing contracts needs an account, a commodity and a date")
    from datetime import date

    try:
        day = date.fromisoformat(on)
    except ValueError as exc:
        raise ValidationError("on is written YYYY-MM-DD", got=on) from exc

    work = work_for(request)
    try:
        found = work.contracts.open_for(account, crop, day)
        return JSONResponse(
            {"contracts": [item.describe() for item in found], "count": len(found)}
        )
    finally:
        work.close()


async def show(request: Request) -> JSONResponse:
    work = work_for(request)
    try:
        return JSONResponse(work.contracts.get(request.path_params["identifier"]).describe())
    finally:
        work.close()


async def create(request: Request) -> JSONResponse:
    reader = reader_for(await request.json())
    standard = reader.commodity("commodity")
    kind = reader.choice("price_kind", PriceKind, PriceKind.FLAT)

    flat = reader.optional_text("price")
    basis = reader.optional_text("basis")
    tolerance = reader.percent("tolerance", "5.0")

    contract = PurchaseContract(
        identifier=reader.text("identifier"),
        account=reader.text("account"),
        commodity_code=standard.code,
        quantity=reader.bushels("quantity"),
        delivery_from=reader.day("delivery_from"),
        delivery_to=reader.day("delivery_to"),
        price_kind=kind,
        flat_price=Price.of(flat) if flat is not None and kind is PriceKind.FLAT else None,
        basis=Price.of(basis) if basis is not None else None,
        futures_month=reader.optional_text("futures_month"),
        tolerance=tolerance,
        written_on=reader.optional_day("written_on"),
    )

    work = work_for(request)
    try:
        work.contracts.add(contract)
        work.commit()
        return JSONResponse(contract.describe(), status_code=201)
    finally:
        work.close()


async def fix_futures(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    reader = reader_for(await request.json())
    at = Price.of(reader.decimal("price"))
    work = work_for(request)
    try:
        fixed = work.contracts.get(identifier).fix_futures(at)
        work.contracts.replace(fixed)
        work.commit()
        return JSONResponse(fixed.describe())
    finally:
        work.close()
