"""Structured logging on the standard library, with no third party in the path.

The requirement is one line per event, machine readable, with the fields that
identify the load on it. That is a formatter and a filter, not a framework, and
a package that a co-op runs on one server should not take a logging dependency
to get it.

Two properties matter more than the format:

**Context travels.** A request handler binds the ticket number once and every
log line inside that call carries it, including lines from code that has never
heard of a request. That is a :class:`contextvars.ContextVar`, which is the one
mechanism that survives both threads and coroutines.

**Nothing secret is ever a field.** The API key is not in the settings dump,
not in the request log and not in an error. There is no redaction step to
forget to call, because the value never reaches a log call in the first place.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

__all__ = [
    "EventLogger",
    "JsonFormatter",
    "PlainFormatter",
    "bind",
    "bound",
    "clear",
    "configure",
    "current",
    "get_logger",
]

_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("binyard_log_context", default={})

_RESERVED = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class ContextFilter(logging.Filter):
    """Copies the bound context onto every record that passes through."""

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in _CONTEXT.get().items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the extras flattened onto it."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "at": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = _plain(value)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PlainFormatter(logging.Formatter):
    """Human readable, for a terminal. Same fields, different shape."""

    def format(self, record: logging.LogRecord) -> str:
        extras = {
            key: _plain(value)
            for key, value in record.__dict__.items()
            if key not in _RESERVED and not key.startswith("_")
        }
        rendered = " ".join(f"{key}={value}" for key, value in sorted(extras.items()))
        stamp = datetime.fromtimestamp(record.created, UTC).strftime("%H:%M:%S")
        line = f"{stamp} {record.levelname.lower():<8} {record.getMessage()}"
        if rendered:
            line = f"{line}  {rendered}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def configure(level: str = "INFO", *, json_output: bool = True, stream=None) -> None:
    """Set up the root logger. Safe to call twice; the second call replaces."""
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter() if json_output else PlainFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())


class EventLogger:
    """A thin adapter so a call site writes fields as keyword arguments.

    ``log.info("load received", bushels=str(net))`` rather than the standard
    library's ``extra={...}``, which is easy to leave off and silently drops
    the fields when it is.
    """

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    @property
    def name(self) -> str:
        return self._logger.name

    def debug(self, event: str, **fields: Any) -> None:
        self._emit(logging.DEBUG, event, fields)

    def info(self, event: str, **fields: Any) -> None:
        self._emit(logging.INFO, event, fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._emit(logging.WARNING, event, fields)

    def error(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields)

    def exception(self, event: str, **fields: Any) -> None:
        self._emit(logging.ERROR, event, fields, exc_info=True)

    def _emit(
        self, level: int, event: str, fields: dict[str, Any], *, exc_info: bool = False
    ) -> None:
        safe = {key: value for key, value in fields.items() if key not in _RESERVED}
        self._logger.log(level, event, extra=safe, exc_info=exc_info)


def get_logger(name: str) -> EventLogger:
    return EventLogger(logging.getLogger(name))


def bind(**fields: Any) -> None:
    """Add fields to the current context, replacing any with the same name."""
    _CONTEXT.set({**_CONTEXT.get(), **fields})


def clear() -> None:
    _CONTEXT.set({})


def current() -> Mapping[str, Any]:
    return dict(_CONTEXT.get())


@contextmanager
def bound(**fields: Any) -> Iterator[None]:
    """Bind fields for the duration of the block, then put back what was there."""
    token = _CONTEXT.set({**_CONTEXT.get(), **fields})
    try:
        yield
    finally:
        _CONTEXT.reset(token)


def _plain(value: Any) -> Any:
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
