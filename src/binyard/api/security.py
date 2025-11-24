"""The three things in front of every request.

**A request identity.** Generated here if the caller did not send one, bound
into the logging context, and echoed back on the response and in any error
body. It is the only thing that makes a support call about "an error this
morning" answerable.

**A body ceiling.** Enforced on ``content-length`` before the body is read, so
a caller announcing forty megabytes is refused rather than streamed. A request
with no length header and a body is refused too: a chunked upload cannot be
checked in advance and this API has no endpoint that needs one.

**An API key.** Optional, because a house running this on a machine nobody else
can reach should not have to invent one, and constant time compared when it is
set. The health endpoints are always open, because a load balancer cannot hold
a key.
"""

from __future__ import annotations

import secrets
import uuid

from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from binyard.observability import metrics
from binyard.observability.logging import bound

__all__ = [
    "API_KEY_HEADER",
    "OPEN_PATHS",
    "REQUEST_ID_HEADER",
    "ApiKeyMiddleware",
    "BodyCeilingMiddleware",
    "RequestContextMiddleware",
]

REQUEST_ID_HEADER = "x-request-id"
API_KEY_HEADER = "x-api-key"

#: Paths a load balancer and a scraper reach without a key.
OPEN_PATHS = frozenset({"/health", "/ready", "/metrics"})


class RequestContextMiddleware:
    """Gives every request an id, logs it, counts it and times it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        status_holder = {"status": 500}

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append(REQUEST_ID_HEADER, request_id)
            await send(message)

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        with (
            bound(request_id=request_id, path=path, method=method),
            metrics.timed("http_request_seconds", method=method),
        ):
            await self.app(scope, receive, wrapped)
        metrics.counter(
            "http_requests_total",
            method=method,
            status=str(status_holder["status"]),
        )


class BodyCeilingMiddleware:
    """Refuses a body bigger than the ceiling, before reading any of it."""

    def __init__(self, app: ASGIApp, ceiling: int) -> None:
        self.app = app
        self.ceiling = ceiling

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in {"POST", "PUT", "PATCH"}:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        declared = request.headers.get("content-length")
        if declared is None:
            if request.headers.get("transfer-encoding", "").lower() == "chunked":
                await _refuse(
                    send,
                    413,
                    "chunked_body",
                    "this API needs a content-length it can check first",
                )
                return
        else:
            try:
                length = int(declared)
            except ValueError:
                await _refuse(send, 400, "bad_content_length", "content-length is not a number")
                return
            if length > self.ceiling:
                await _refuse(
                    send,
                    413,
                    "body_too_large",
                    f"bodies are capped at {self.ceiling} bytes",
                )
                return
        await self.app(scope, receive, send)


class ApiKeyMiddleware:
    """Checks the key when one is configured, and lets the probes through."""

    def __init__(self, app: ASGIApp, api_key: str | None) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.api_key is None:
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return
        offered = Request(scope).headers.get(API_KEY_HEADER)
        if offered is None or not secrets.compare_digest(offered, self.api_key):
            metrics.counter("http_unauthorised_total")
            await _refuse(send, 401, "unauthorised", "that key is not one of ours")
            return
        await self.app(scope, receive, send)


async def _refuse(send: Send, status: int, code: str, message: str) -> None:
    response = JSONResponse(
        {"code": code, "message": message, "detail": {}}, status_code=status
    )
    await response(  # type: ignore[call-arg]
        {"type": "http"}, _no_body, send
    )


async def _no_body() -> Message:  # pragma: no cover - responses never read a body
    return {"type": "http.request", "body": b"", "more_body": False}
