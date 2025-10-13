"""The application factory.

Starlette rather than a framework with a schema layer, because the domain
already owns its own parsing and validation (see :mod:`binyard.api.parsing`)
and a second one would only restate it. What is left is routing, middleware and
JSON, which Starlette does without opinions.

The factory takes its dependencies rather than reaching for them. A test builds
an app on an in-memory database and a settings object it made up, with no
environment and no file, which is why the API tests run in milliseconds and
leave nothing behind.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from binyard import __version__
from binyard.api.errors import install_handlers
from binyard.api.security import ApiKeyMiddleware, BodyCeilingMiddleware, RequestContextMiddleware
from binyard.config import Settings, load_settings
from binyard.observability.metrics import REGISTRY
from binyard.store.migrate import pending
from binyard.store.sqlite import SqliteUnitOfWork, connect

__all__ = ["create_app", "ConnectionFactory"]

ConnectionFactory = Callable[[], sqlite3.Connection]


def create_app(
    settings: Settings | None = None,
    connections: ConnectionFactory | None = None,
) -> Starlette:
    """Build the application. Everything it needs is an argument."""
    resolved = settings or load_settings()
    factory = connections or _factory_for(resolved)

    from binyard.api.routes import health

    routes: list[Route] = []
    for module in (health,):
        routes.extend(module.routes())

    middleware = [
        Middleware(RequestContextMiddleware),
        Middleware(BodyCeilingMiddleware, ceiling=resolved.max_request_bytes),
        Middleware(ApiKeyMiddleware, api_key=resolved.api_key),
    ]
    if resolved.allowed_origins:
        middleware.append(
            Middleware(
                CORSMiddleware,
                allow_origins=list(resolved.allowed_origins),
                allow_methods=["GET", "POST"],
                allow_headers=["content-type", "x-api-key"],
            )
        )

    application = Starlette(
        debug=resolved.debug,
        routes=routes,
        middleware=middleware,
        exception_handlers=install_handlers(),
    )
    application.state.settings = resolved
    application.state.connections = factory
    return application


def _factory_for(settings: Settings) -> ConnectionFactory:
    from binyard.cli.context import database_path

    where = database_path(settings.database_url)

    def factory() -> sqlite3.Connection:
        return connect(where)

    return factory


def work_for(request: Request) -> SqliteUnitOfWork:
    """A unit of work for this request. Closed by the caller."""
    return SqliteUnitOfWork(request.app.state.connections())


async def healthy(request: Request) -> JSONResponse:
    """Liveness. Answers without touching the database."""
    return JSONResponse({"status": "ok", "version": __version__})


async def ready(request: Request) -> JSONResponse:
    """Readiness. Fails when the schema is behind, because it is not ready."""
    work = work_for(request)
    try:
        outstanding = pending(work._connection)
        settings: Settings = request.app.state.settings
        payload = {
            "status": "ok" if not outstanding else "migrations_pending",
            "version": __version__,
            "location": str(settings.location),
            "pending_migrations": [item.filename for item in outstanding],
        }
        return JSONResponse(payload, status_code=200 if not outstanding else 503)
    finally:
        work.close()


async def metrics(request: Request) -> PlainTextResponse:
    """The counters, in the text format a scraper reads."""
    return PlainTextResponse(REGISTRY.render(), media_type="text/plain; version=0.0.4")
