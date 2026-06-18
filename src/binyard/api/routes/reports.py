"""The reports, over HTTP, in whichever of the three formats was asked for.

``Accept: text/csv`` gets a spreadsheet, ``Accept: text/plain`` gets the same
table a terminal would print, and anything else gets JSON. The report code does
not know which, and that is the point of :class:`binyard.reporting.table.Table`
having three renderers and no fourth.

Everything here is a GET with no body and no writes, so it is safe to point a
dashboard at and safe to retry.
"""

from __future__ import annotations

from datetime import date

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.bushels import Bushels
from binyard.commodity import lookup
from binyard.errors import ValidationError
from binyard.reporting.table import Table, to_csv, to_json, to_text
from binyard.services.reporting import (
    day_sheet,
    examiner_report,
    exposure_on,
    position_on,
    review_queue,
)

__all__ = ["render", "routes"]


def routes() -> list[Route]:
    return [
        Route("/reports/position", position, methods=["GET"], name="position"),
        Route("/reports/examiner", examiner, methods=["GET"], name="examiner"),
        Route("/reports/day", day, methods=["GET"], name="day_sheet"),
        Route("/reports/exposure", exposure, methods=["GET"], name="exposure"),
        Route("/reports/review", review, methods=["GET"], name="review_queue"),
    ]


def _on(request: Request) -> date:
    raw = request.query_params.get("on")
    if raw is None:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("on is written YYYY-MM-DD", got=raw) from exc


def render(request: Request, table: Table) -> Response:
    """Pick a rendering off the Accept header."""
    accept = request.headers.get("accept", "")
    if "text/csv" in accept:
        return PlainTextResponse(to_csv(table), media_type="text/csv")
    if "text/plain" in accept:
        return PlainTextResponse(to_text(table))
    return Response(to_json(table), media_type="application/json")


async def position(request: Request) -> Response:
    work = work_for(request)
    try:
        settings = request.app.state.settings
        report = position_on(work, on=_on(request), location=settings.location)
        if "text/" in request.headers.get("accept", ""):
            from binyard.reporting.reports import position_table

            return render(request, position_table(report))
        return JSONResponse(report.describe())
    finally:
        work.close()


async def examiner(request: Request) -> Response:
    work = work_for(request)
    try:
        settings = request.app.state.settings
        return render(
            request, examiner_report(work, on=_on(request), location=settings.location)
        )
    finally:
        work.close()


async def day(request: Request) -> Response:
    work = work_for(request)
    try:
        settings = request.app.state.settings
        sheet = day_sheet(work, on=_on(request), location=settings.location)
        which = request.query_params.get("only")
        tables = {
            "tickets": sheet.tickets,
            "bins": sheet.bins,
            "obligations": sheet.obligations,
            "position": sheet.position,
        }
        if which is not None:
            if which not in tables:
                raise ValidationError(
                    "no such table on the day sheet", got=which, known=sorted(tables)
                )
            return render(request, tables[which])
        payload = sheet.describe()
        payload["tables"] = {name: to_json(table) for name, table in tables.items()}
        return JSONResponse(payload)
    finally:
        work.close()


async def exposure(request: Request) -> Response:
    sales: dict[str, Bushels] = {}
    for raw in request.query_params.getlist("sold"):
        if ":" not in raw:
            raise ValidationError("a sale is written CROP:BUSHELS", got=raw)
        crop, _, quantity = raw.partition(":")
        sales[lookup(crop).code] = Bushels.of(quantity.strip())

    work = work_for(request)
    try:
        report = exposure_on(work, on=_on(request), sales=sales)
        if "text/" in request.headers.get("accept", ""):
            from binyard.reporting.reports import exposure_table

            return render(request, exposure_table(report))
        return JSONResponse(report.describe())
    finally:
        work.close()


async def review(request: Request) -> Response:
    work = work_for(request)
    try:
        return render(request, review_queue(work))
    finally:
        work.close()
