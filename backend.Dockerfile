# MiniMe backend — FastAPI app (api/server.py)
#
# python:3.12-slim — NOT 3.11. pyproject.toml's ruff target-version="py311"
# is only the linter's syntax target, not the actual runtime floor.
# requirements.txt pins numpy==2.5.1, which requires Python >=3.12 (confirmed
# against PyPI); building on 3.11 fails at `pip install` with no matching
# distribution found. If you deliberately need 3.11 for another reason,
# pin numpy back to a 2.x release that still supports it (e.g. 2.4.6)
# instead of bumping the base image — but 3.12 is the simpler fix here.
FROM python:3.12-slim

# libpq5 — psycopg2-binary's wheel is self-contained, but a couple of
# distros still need the shared lib present at runtime; cheap to include.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first so Docker's layer cache skips the (long) pip
# install step on rebuilds where only application code changed.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

# data/exports is where NOTES_EXPORTS_DIR (api/server.py) writes generated
# reports/decks/scripts — needs to exist and be writable in the container,
# not just on your local disk.
RUN mkdir -p data/exports

# Run as a non-root user (Semgrep dockerfile.security.missing-user) —
# python:3.12-slim has no unprivileged user baked in like node:*-slim
# does, so create one explicitly and hand it ownership of /app (it
# needs to write into data/exports at runtime, not just read code).
RUN groupadd --system app && useradd --system --gid app --no-create-home app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Matches how you've been running it locally: uvicorn api.server:app --port 8000.
# No --reload in production — that's a dev-only flag; docker-compose.override
# can add it back for local container development if you want hot-reload
# inside Docker too.
CMD ["uvicorn", "api.server:app", "--host", "0.0.0.0", "--port", "8000"]
