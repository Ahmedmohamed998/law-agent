# AI service — retrieval, conversations, streaming.
#
# Two stages so the compiler toolchain that some wheels need does not ship in
# the running image.

# ── build ─────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Build-only. psycopg[binary] and chromadb ship wheels for this platform, but
# a transitive dependency without one needs a compiler, and failing at image
# build is better than discovering it on a different base later.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

# Its own virtualenv rather than the system site-packages, so the runtime
# stage copies one directory and inherits nothing else.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install -r requirements.txt

# ── runtime ───────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is here for the container healthcheck below, and for debugging a
# stream from inside the network namespace when the proxy is suspect.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 lawagent

COPY --from=build /opt/venv /opt/venv

WORKDIR /app

# Application code only. `data/` is a bind mount, not image content: the
# Chroma index is 79MB, Chroma opens it read-write even to query, and
# re-ingesting documents should not mean rebuilding an image.
COPY --chown=lawagent:lawagent alembic.ini ./
COPY --chown=lawagent:lawagent migrations/ ./migrations/
COPY --chown=lawagent:lawagent app/ ./app/

USER lawagent

EXPOSE 8000

# Ready, not merely alive: the index must be loaded and the pool must hand out
# a connection. start-period is generous because the BM25 index over 4,011
# chunks is built before the port answers.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/readyz || exit 1

# One worker, many threads. The index is held in memory and built once at
# startup; every extra process is another full copy of it. Scale threads
# first, and only add processes after measuring memory.
CMD ["python", "-m", "app.api.main"]
