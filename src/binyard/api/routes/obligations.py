"""Storage obligations over HTTP.

Reads are a grower's balance, which is the question the counter is asked all
day. Writes are the two that cannot be done any other way: pricing a deferred
contract, and cancelling a warehouse receipt so a new one can be issued for the
remainder.

There is no endpoint that opens an obligation. One is opened by delivering
grain, which happens on ``POST /tickets``, and an endpoint that could conjure
one without a ticket behind it would be the easiest way in the whole API to
make the position report wrong.
"""

from __future__ import annotations

from datetime import date

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.api.parsing import reader_for
from binyard.errors import ValidationError
from binyard.obligations.storage import Accrual, StorageTerms, accrue

__all__ = ["routes"]


def routes() -> list[Route]:
    return [
        Route("/obligations", listing, methods=["GET"], name="list_obligations"),
        Route("/obligations/{identifier}", show, methods=["GET"], name="show_obligation"),
        Route(
            "/obligations/{identifier}/price",
            price,
            methods=["POST"],
            name="price_obligation",
        ),
        Route(
            "/obligations/{identifier}/cancel",
            cancel,
            methods=["POST"],
            name="cancel_obligation",
        ),
        Route(
            "/obligations/{identifier}/storage",
            storage,
            methods=["GET"],
            name="accrue_storage",
        ),
    ]


async def listing(request: Request) -> JSONResponse:
    account = request.query_params.get("account")
    crop = request.query_params.get("commodity")
    if account and not crop:
        raise ValidationError("asking by account needs a commodity too")

    work = work_for(request)
    try:
        found = (
            work.obligations.open_for(account, crop)
            if account and crop
            else work.obligations.all_open()
        )
        return JSONResponse(
            {
                "obligations": [item.describe() for item in found],
                "count": len(found),
            }
        )
    finally:
        work.close()


async def show(request: Request) -> JSONResponse:
    work = work_for(request)
    try:
        return JSONResponse(work.obligations.get(request.path_params["identifier"]).describe())
    finally:
        work.close()


async def price(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    reader = reader_for(await request.json())
    named = reader.decimal("price")
    on = reader.day("on", date.today())

    from binyard.money import Price

    work = work_for(request)
    try:
        priced = work.obligations.get(identifier).priced(Price.of(named), on)
        work.obligations.replace(priced)
        work.commit()
        payload = priced.describe()
        payload["value"] = str(priced.value())
        return JSONResponse(payload)
    finally:
        work.close()


async def cancel(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    reader = reader_for(await request.json())
    work = work_for(request)
    try:
        cancelled = work.obligations.get(identifier).cancelled(
            superseded_by=reader.optional_text("superseded_by")
        )
        work.obligations.replace(cancelled)
        work.commit()
        return JSONResponse(cancelled.describe())
    finally:
        work.close()


async def storage(request: Request) -> JSONResponse:
    identifier = request.path_params["identifier"]
    settings = request.app.state.settings
    raw = request.query_params.get("through")
    try:
        through = date.fromisoformat(raw) if raw else date.today()
    except ValueError as exc:
        raise ValidationError("through is written YYYY-MM-DD", got=raw) from exc

    accrual = request.query_params.get("accrual")
    terms = settings.storage_terms
    if accrual:
        try:
            terms = StorageTerms(
                rate_per_bushel_month=terms.rate_per_bushel_month,
                accrual=Accrual(accrual),
                free_days=terms.free_days,
                minimum_charge=terms.minimum_charge,
            )
        except ValueError as exc:
            raise ValidationError(
                "not an accrual convention",
                got=accrual,
                allowed=[item.value for item in Accrual],
            ) from exc

    work = work_for(request)
    try:
        obligation = work.obligations.get(identifier)
        return JSONResponse(accrue(obligation, terms, through).describe())
    finally:
        work.close()
