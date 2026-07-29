"""Turning a domain error into an HTTP response, in one table.

Every error the package raises already carries a machine code and a detail
mapping, so the response body is the error's own ``as_dict`` plus nothing. The
only thing this module decides is the status, and it decides it by looking the
code up rather than by catching each class at each call site.

A code with no entry gets a 500 on purpose. A new error class that nobody has
thought about the status for should be loud, not quietly a 400.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from binyard.errors import BinyardError
from binyard.observability.logging import get_logger

__all__ = ["error_response", "install_handlers", "status_for"]

_log = get_logger("binyard.api")

_STATUS: dict[str, int] = {
    "validation_error": 400,
    "unit_error": 400,
    "incompatible_units": 400,
    "unknown_commodity": 400,
    "grading_error": 400,
    "factor_out_of_range": 400,
    "schedule_error": 400,
    "no_schedule_in_effect": 409,
    "ticket_error": 400,
    "ticket_state_error": 409,
    "inventory_error": 409,
    "bin_capacity_exceeded": 409,
    "commodity_mismatch": 409,
    "insufficient_inventory": 409,
    "obligation_error": 409,
    "blend_error": 422,
    "infeasible_blend": 422,
    "loadout_error": 409,
    "not_found": 404,
    "conflict": 409,
    "configuration_error": 500,
    "too_many_requests": 429,
}


def status_for(error: BinyardError) -> int:
    """The status this error deserves. Unknown codes are server errors."""
    return _STATUS.get(error.code, 500)


def error_response(error: BinyardError, *, request_id: str | None = None) -> JSONResponse:
    """Render an error as the JSON body and status this package gives it."""
    payload: dict[str, Any] = error.as_dict()
    if request_id:
        payload["request_id"] = request_id
    return JSONResponse(payload, status_code=status_for(error))


async def _domain_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, BinyardError)
    request_id = getattr(request.state, "request_id", None)
    status = status_for(error)
    if status >= 500:
        _log.error(
            "request failed", code=error.code, path=request.url.path, request_id=request_id
        )
    else:
        _log.info(
            "request refused", code=error.code, path=request.url.path, request_id=request_id
        )
    return error_response(error, request_id=request_id)


async def _unexpected(request: Request, error: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    _log.exception("unhandled error", path=request.url.path, request_id=request_id)
    payload = {
        "code": "internal_error",
        "message": "something went wrong and it was not your request",
        "detail": {},
    }
    if request_id:
        payload["request_id"] = request_id
    return JSONResponse(payload, status_code=500)


def install_handlers() -> dict[Any, Any]:
    """The exception handler map an application is built with."""
    return {BinyardError: _domain_error, Exception: _unexpected}
