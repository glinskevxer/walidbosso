"""Gunicorn settings for the container.

Two workers per core plus one is the usual advice and it is wrong here. This
service spends its time in SQLite, which serialises writes, so more workers buy
nothing but lock contention on a busy scale house. Two is enough to keep a slow
report from blocking a truck, and the number is an environment variable so a
house on Postgres can raise it without a rebuild.
"""

from __future__ import annotations

import multiprocessing
import os

bind = os.environ.get("BINYARD_BIND", "0.0.0.0:8000")
workers = int(os.environ.get("BINYARD_WORKERS", "2"))
worker_class = "uvicorn.workers.UvicornWorker"

#: A blend over a full yard is the slowest thing here and it is not slow.
timeout = int(os.environ.get("BINYARD_TIMEOUT", "30"))
graceful_timeout = 20
keepalive = 5

#: Recycle workers so a slow leak cannot run a season.
max_requests = 4000
max_requests_jitter = 400

accesslog = "-"
errorlog = "-"
loglevel = os.environ.get("BINYARD_LOG_LEVEL", "info").lower()
access_log_format = '%(h)s %(m)s %(U)s %(s)s %(M)sms %({x-request-id}o)s'

preload_app = True


def on_starting(server) -> None:  # pragma: no cover - runs inside gunicorn
    server.log.info(
        "binyard starting with %s workers on %s (%s cores available)",
        workers,
        bind,
        multiprocessing.cpu_count(),
    )
