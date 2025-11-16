#!/bin/sh
# Migrate, then do what was asked. Migrating on start is safe because the
# runner refuses anything it has already applied and refuses anything that has
# changed since, so a container that starts twice does not run a migration
# twice and a container running an older image stops rather than corrupting the
# schema.
set -eu

binyard migrate

case "${1:-serve}" in
  serve)
    exec gunicorn \
      --config /opt/venv/gunicorn.conf.py \
      --worker-class uvicorn.workers.UvicornWorker \
      "binyard.api:create_app()"
    ;;
  cli)
    shift
    exec binyard "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
