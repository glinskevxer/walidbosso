"""Liveness, readiness and metrics.

Three endpoints rather than one, because they answer different questions and a
load balancer that cannot tell them apart takes the whole house out when the
database goes slow.
"""

from __future__ import annotations

from starlette.routing import Route

from binyard.api.app import healthy, metrics, ready

__all__ = ["routes"]


def routes() -> list[Route]:
    """The routes this module serves."""
    return [
        Route("/health", healthy, methods=["GET"], name="health"),
        Route("/ready", ready, methods=["GET"], name="ready"),
        Route("/metrics", metrics, methods=["GET"], name="metrics"),
    ]
