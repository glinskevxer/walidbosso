"""A description of the API, served by the API.

Not an OpenAPI document. This package's request bodies are read by the domain's
own parsers (see :mod:`binyard.api.parsing`), so there is no schema object to
render and generating one would mean writing the rules down a second time in a
form that could drift from the first.

What a caller actually needs on arrival is smaller than a schema: which routes
exist, what each one is for, and whether this build wants a key. That is what
this returns, and it is built by walking the router rather than by keeping a
list somebody has to remember to update.
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from binyard import __version__
from binyard.api.security import API_KEY_HEADER, OPEN_PATHS
from binyard.commodity import codes

__all__ = ["routes"]

#: One line per route, keyed by name. A route with no line here still appears.
_PURPOSE: dict[str, str] = {
    "health": "liveness, answers without the database",
    "ready": "readiness, fails while migrations are outstanding",
    "metrics": "counters in the prometheus text format",
    "create_ticket": "weigh, grade, shrink, bin and apply one load",
    "list_tickets": "tickets by day or by account, open ones by default",
    "show_ticket": "one ticket by its printed number",
    "list_bins": "every bin, optionally as of a moment",
    "create_bin": "register a bin",
    "show_bin": "one bin, optionally as of a moment",
    "transfer": "move grain between two bins",
    "adjust": "post a measure-up difference",
    "plan_blend": "a draw list for a spec, writing nothing",
    "list_obligations": "what the house owes, open only",
    "show_obligation": "one obligation",
    "price_obligation": "name the price on a deferred contract",
    "cancel_obligation": "cancel a receipt so a new one can be issued",
    "accrue_storage": "storage owed on one obligation through a date",
    "list_contracts": "a grower's open contracts for a crop on a day",
    "create_contract": "write a purchase contract",
    "show_contract": "one contract",
    "fix_futures": "fix the futures leg of a basis contract",
    "list_runs": "what is on the dryer now",
    "start_run": "draw wet grain and light the dryer",
    "show_run": "one run, with its variance against the formula",
    "finish_run": "weigh out and put the dry grain away",
    "abandon_run": "stop a run early",
    "import_feed": "post the indicator file as text/plain",
    "feed_history": "what one feed file did",
    "position": "inventory against obligations",
    "examiner": "the two column statement an examiner asks for",
    "day_sheet": "the close of business sheet",
    "review_queue": "tickets somebody has to look at",
}


def routes() -> list[Route]:
    return [Route("/", index, methods=["GET"], name="index")]


async def index(request: Request) -> JSONResponse:
    settings = request.app.state.settings
    described = []
    for route in request.app.routes:
        if not isinstance(route, Route) or route.name == "index":
            continue
        for method in sorted(route.methods or set()):
            if method in {"HEAD", "OPTIONS"}:
                continue
            described.append(
                {
                    "method": method,
                    "path": route.path,
                    "name": route.name,
                    "purpose": _PURPOSE.get(route.name or "", ""),
                    "open": route.path in OPEN_PATHS,
                }
            )
    described.sort(key=lambda item: (item["path"], item["method"]))

    return JSONResponse(
        {
            "service": "binyard",
            "version": __version__,
            "house": settings.house_name,
            "location": str(settings.location),
            "crop_year": settings.crop_year,
            "commodities": list(codes()),
            "authentication": {
                "required": settings.requires_api_key,
                "header": API_KEY_HEADER,
                "open_paths": sorted(OPEN_PATHS),
            },
            "routes": described,
            "count": len(described),
        }
    )
