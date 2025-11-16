# Two stages. The build stage has a compiler and the wheel cache; the run stage
# has neither, and runs as a user that cannot write to the code it is running.

FROM python:3.12-slim-bookworm AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip setuptools wheel \
 && pip install ".[api,serve]"


FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    BINYARD_DATABASE_URL=sqlite:////var/lib/binyard/binyard.sqlite3 \
    BINYARD_LOG_LEVEL=INFO

# A real user, a real home, and a data directory it owns. Running as root inside
# a container is not a hypothetical problem when the process writes a database
# file that a volume mount will keep long after the container is gone.
RUN groupadd --system --gid 1101 binyard \
 && useradd --system --uid 1101 --gid binyard --create-home --home-dir /home/binyard binyard \
 && mkdir -p /var/lib/binyard \
 && chown -R binyard:binyard /var/lib/binyard

COPY --from=build /opt/venv /opt/venv
COPY --chown=root:root scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
COPY --chown=root:root gunicorn.conf.py /opt/venv/gunicorn.conf.py
RUN chmod 0755 /usr/local/bin/entrypoint.sh

USER binyard
WORKDIR /var/lib/binyard
VOLUME ["/var/lib/binyard"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["serve"]
