"""A ceiling on how fast one caller can write.

Not a defence against an attacker. This service sits on a private network and
the thing it is actually protecting against is a scale house terminal stuck in
a retry loop, which will happily post the same load four hundred times a minute
and fill a bin with grain that never existed.

A token bucket, in memory, per caller. In memory because there is one process
per house and a shared counter would mean a second piece of infrastructure to
protect against a stuck terminal, which is the wrong trade. Per caller because
one lane looping should not stop the other lane weighing.

Reads are never throttled. A dashboard polling the bin board is not the
problem, and throttling reads is how a load balancer's health check starts
failing under load.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from binyard.api.security import API_KEY_HEADER, OPEN_PATHS
from binyard.observability import metrics
from binyard.observability.logging import get_logger

__all__ = ["WRITE_METHODS", "Bucket", "ThrottleMiddleware", "TokenBuckets"]

_log = get_logger("binyard.api.throttle")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@dataclass(slots=True)
class Bucket:
    """One caller's allowance."""

    tokens: float
    last_filled: float

    def take(self, *, rate: float, burst: float, now: float) -> bool:
        """Refill for the time that has passed, then take one if there is one."""
        elapsed = max(now - self.last_filled, 0.0)
        self.tokens = min(burst, self.tokens + elapsed * rate)
        self.last_filled = now
        if self.tokens < 1.0:
            return False
        self.tokens -= 1.0
        return True

    def seconds_until_next(self, *, rate: float) -> float:
        if self.tokens >= 1.0 or rate <= 0:
            return 0.0
        return (1.0 - self.tokens) / rate


class TokenBuckets:
    """Buckets by caller, with the stale ones dropped as they are noticed."""

    #: A caller that has not been seen for this long is forgotten.
    IDLE_SECONDS = 900.0

    def __init__(self, *, per_minute: int, burst: int | None = None) -> None:
        if per_minute <= 0:
            raise ValueError("a rate has to be positive")
        self.rate = per_minute / 60.0
        self.burst = float(burst if burst is not None else per_minute)
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, caller: str, *, now: float | None = None) -> tuple[bool, float]:
        """Whether this caller may write, and how long until they may again."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._forget_idle(moment)
            bucket = self._buckets.get(caller)
            if bucket is None:
                bucket = Bucket(tokens=self.burst, last_filled=moment)
                self._buckets[caller] = bucket
            allowed = bucket.take(rate=self.rate, burst=self.burst, now=moment)
            return allowed, bucket.seconds_until_next(rate=self.rate)

    def _forget_idle(self, now: float) -> None:
        if len(self._buckets) < 64:
            return
        stale = [
            caller
            for caller, bucket in self._buckets.items()
            if now - bucket.last_filled > self.IDLE_SECONDS
        ]
        for caller in stale:
            del self._buckets[caller]

    @property
    def tracked(self) -> int:
        with self._lock:
            return len(self._buckets)


class ThrottleMiddleware:
    """Refuses a write that is coming too fast, with a Retry-After."""

    def __init__(self, app: ASGIApp, *, per_minute: int, burst: int | None = None) -> None:
        self.app = app
        self.buckets = TokenBuckets(per_minute=per_minute, burst=burst)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in WRITE_METHODS:
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in OPEN_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        allowed, wait = self.buckets.allow(_caller(request))
        if allowed:
            await self.app(scope, receive, send)
            return

        metrics.counter("http_throttled_total", method=scope.get("method", "POST"))
        _log.warning("caller throttled", path=scope.get("path", ""), retry_after=round(wait, 2))
        response = JSONResponse(
            {
                "code": "too_many_requests",
                "message": "writes are coming faster than this house accepts them",
                "detail": {"retry_after_seconds": round(wait, 2)},
            },
            status_code=429,
            headers={"retry-after": str(max(1, int(wait + 0.999)))},
        )
        await response(scope, _no_body, send)


def _caller(request: Request) -> str:
    """Who is asking. The key if there is one, otherwise the peer address."""
    key = request.headers.get(API_KEY_HEADER)
    if key:
        # Never the key itself: it would end up in a dict that gets logged.
        return f"key:{hash(key) & 0xFFFFFFFF:08x}"
    client = request.client
    return f"peer:{client.host}" if client else "peer:unknown"


async def _no_body() -> Message:  # pragma: no cover - responses never read a body
    return {"type": "http.request", "body": b"", "more_body": False}
