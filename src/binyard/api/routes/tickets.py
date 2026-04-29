"""Scale tickets over HTTP.

``POST /tickets`` is the whole receiving path: it weighs, grades, shrinks,
bins and applies, in one transaction, and answers with the ticket, the
discounts and where the bushels went. It is the endpoint the scale house
terminal calls and the only write path in the API that touches five aggregates.

A load arrives with either a ``sample``, which is one set of readings, or a
list of ``probes``, which is what the probe actually pulled and where from.
Probes are the better answer: the composite is weighted by what each one
pulled, and anything the sampling rules do not like lands on the ticket as a
review note instead of being lost.

Reads are deliberately thin. Nothing here computes anything the domain has not
already computed, so a listing is a projection of stored tickets and never a
second opinion about what a load graded.
"""

from __future__ import annotations

import json
from datetime import date, datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from binyard.api.app import work_for
from binyard.api.parsing import Reader, reader_for
from binyard.contracts.application import Priority
from binyard.discounts.posted import default_book
from binyard.errors import ValidationError
from binyard.grading.factors import FactorSample, Odor
from binyard.grading.sampling import Position, Probe
from binyard.identity import Sequence, TicketNumber, crop_year_of
from binyard.obligations.obligation import ObligationKind
from binyard.observability import metrics
from binyard.services.receiving import ReceiptRequest, receive_load
from binyard.store.replies import StoredReply, digest
from binyard.tickets.weights import ScaleWeights, TareSource

__all__ = ["probes_from", "routes", "sample_from", "weights_from"]


def routes() -> list[Route]:
    return [
        Route("/tickets", create, methods=["POST"], name="create_ticket"),
        Route("/tickets", listing, methods=["GET"], name="list_tickets"),
        Route("/tickets/{number}", show, methods=["GET"], name="show_ticket"),
    ]


def weights_from(reader: Reader) -> ScaleWeights:
    return ScaleWeights(
        gross=reader.weight("gross"),
        tare=reader.weight("tare"),
        tare_source=reader.choice("tare_source", TareSource, TareSource.WEIGHED),
    )


def sample_from(reader: Reader) -> FactorSample:
    return FactorSample(
        moisture=reader.percent("moisture"),
        test_weight=reader.decimal("test_weight"),
        total_damage=reader.percent("total_damage", "0"),
        heat_damage=reader.percent("heat_damage", "0"),
        foreign_material=reader.percent("foreign_material", "0"),
        broken=reader.percent("broken", "0"),
        dockage=reader.percent("dockage", "0"),
        other_classes=reader.percent("other_classes", "0"),
        shrunken_broken=reader.percent("shrunken_broken", "0"),
        odor=reader.choice("odor", Odor, Odor.NONE),
        infested=reader.flag("infested", False),
    )


def probes_from(reader: Reader) -> tuple[Probe, ...]:
    return tuple(
        Probe(
            position=item.choice("position", Position),
            grams=item.integer("grams"),
            reading=sample_from(item.nested("reading")),
        )
        for item in reader.each("probes")
    )


IDEMPOTENCY_HEADER = "idempotency-key"


async def create(request: Request) -> Response:
    raw = await request.body()
    key = request.headers.get(IDEMPOTENCY_HEADER)
    route = "POST /tickets"
    fingerprint = digest(route, raw)

    if key:
        work = work_for(request)
        try:
            stored = work.replies.replay(key, route, fingerprint)
        finally:
            work.close()
        if stored is not None:
            return Response(
                stored.body,
                status_code=stored.status,
                media_type="application/json",
                headers={"idempotent-replay": "true"},
            )

    reader = reader_for(json.loads(raw))
    standard = reader.commodity("commodity")
    weighed_on = reader.day("weighed_on", date.today())
    # The ledger is dated from the weighing, not from the wall clock. A load
    # posted the morning after harvest with yesterday's date has to fold into
    # yesterday's position, or the bin reads empty on the day it was filled.
    at = reader.timestamp("at", datetime.combine(weighed_on, datetime.now().time()).isoformat())
    settings = request.app.state.settings

    probed = reader.optional_nested("sample") is None
    receipt_request = ReceiptRequest(
        account=reader.text("account"),
        standard=standard,
        weights=weights_from(reader.nested("weights")),
        sample=None if probed else sample_from(reader.nested("sample")),
        probes=probes_from(reader) if probed else (),
        bin_identifier=reader.text("bin"),
        weighed_on=weighed_on,
        at=at,
        by=reader.text("by", "api"),
        hauler=reader.optional_text("hauler"),
        priority=reader.choice("priority", Priority, Priority.EARLIEST_DEADLINE),
        storage_kind=reader.choice("storage_kind", ObligationKind, ObligationKind.OPEN_STORAGE),
        obligation_identifier=reader.optional_text("obligation"),
    )

    work = work_for(request)
    try:
        receipt = receive_load(
            work,
            Sequence(settings.location, crop_year_of(weighed_on)),
            receipt_request,
            book=default_book(),
        )
    finally:
        work.close()

    metrics.counter("loads_received_total", commodity=standard.code)
    body = json.dumps(receipt.describe())
    if key:
        keeper = work_for(request)
        try:
            keeper.replies.remember(
                StoredReply(
                    key=key,
                    route=route,
                    request_hash=fingerprint,
                    status=201,
                    body=body,
                    stored_at=datetime.now(),
                )
            )
            keeper.commit()
        finally:
            keeper.close()
    return Response(body, status_code=201, media_type="application/json")


async def listing(request: Request) -> JSONResponse:
    account = request.query_params.get("account")
    day = request.query_params.get("day")
    if account and day:
        raise ValidationError("ask by account or by day, not both")

    work = work_for(request)
    try:
        if account:
            found = work.tickets.for_account(account)
        elif day:
            try:
                on = date.fromisoformat(day)
            except ValueError as exc:
                raise ValidationError("day is written YYYY-MM-DD", got=day) from exc
            found = work.tickets.weighed_on(on)
        else:
            found = work.tickets.open_tickets()
        return JSONResponse(
            {"tickets": [ticket.describe() for ticket in found], "count": len(found)}
        )
    finally:
        work.close()


async def show(request: Request) -> JSONResponse:
    number = TicketNumber.parse(request.path_params["number"])
    work = work_for(request)
    try:
        return JSONResponse(work.tickets.get(number).describe())
    finally:
        work.close()
