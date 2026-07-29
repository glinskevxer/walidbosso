"""The scale house feed over HTTP.

``POST /feed`` takes the file as a plain text body rather than as JSON. The
indicator writes fixed width ASCII and wrapping it in a JSON string would mean
escaping it, sending it, and unescaping it, for no gain and one more place for
a stray newline to change what was weighed.

``?dry_run=true`` parses and reports without writing, which is what a new
operator runs before pointing the nightly job at a folder.
"""

from __future__ import annotations

from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.contracts.application import Priority
from binyard.discounts.posted import default_book
from binyard.errors import ValidationError
from binyard.feed.reader import read_feed
from binyard.identity import Sequence, crop_year_of
from binyard.services.importing import import_feed

__all__ = ["routes"]


def routes() -> list[Route]:
    """The routes this module serves."""
    return [
        Route("/feed", accept, methods=["POST"], name="import_feed"),
        Route("/feed/{source}", history, methods=["GET"], name="feed_history"),
    ]


def _bins(request: Request) -> dict[str, str]:
    """``?bin=CORN:N1&bin=SOYBEANS:S1``, because a query string has no objects."""
    mapping: dict[str, str] = {}
    for raw in request.query_params.getlist("bin"):
        if ":" not in raw:
            raise ValidationError("a bin is written CROP:BIN", got=raw)
        crop, _, identifier = raw.partition(":")
        mapping[crop.strip().upper()] = identifier.strip()
    if not mapping:
        raise ValidationError("say which bin each crop goes into with ?bin=CROP:BIN")
    return mapping


async def accept(request: Request) -> JSONResponse:
    content_type = request.headers.get("content-type", "")
    if content_type and not content_type.startswith("text/"):
        raise ValidationError("send the feed file as text/plain, not as json", got=content_type)
    body = (await request.body()).decode("ascii", errors="replace")
    if not body.strip():
        raise ValidationError("the feed body is empty")

    batch = read_feed(body)
    source = request.query_params.get("source", "http")
    dry_run = request.query_params.get("dry_run", "").lower() in {"1", "true", "yes"}

    if dry_run:
        return JSONResponse(
            {
                "dry_run": True,
                "source": source,
                "parsed": batch.describe(),
                "records": [record.describe() for record in batch.records],
            }
        )

    settings = request.app.state.settings
    work = work_for(request)
    try:
        result = import_feed(
            work,
            batch,
            Sequence(settings.location, crop_year_of(datetime.now().date())),
            source=source,
            book=default_book(),
            bin_for=_bins(request),
            at=datetime.now(),
            priority=Priority.EARLIEST_DEADLINE,
        )
        payload = result.describe()
        payload["parsed"] = batch.describe()
        return JSONResponse(payload, status_code=201 if result.taken else 200)
    finally:
        work.close()


async def history(request: Request) -> JSONResponse:
    source = request.path_params["source"]
    work = work_for(request)
    try:
        entries = work.feed.for_source(source)
        return JSONResponse(
            {
                "source": source,
                "count": len(entries),
                "entries": [entry.describe() for entry in entries],
            }
        )
    finally:
        work.close()
