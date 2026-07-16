# syntax=docker/dockerfile:1
# ── Stage 1: dependency installation ─────────────────────────────────────────
FROM python:3.13-slim AS deps

# psycopg[binary] bundles libpq so we don't need the system package.
# build-essential is needed only if you switch to psycopg[c]; not needed here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt


# ── Stage 2: runtime image ───────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# HF Spaces convention: non-root user with UID 1000.
RUN useradd --uid 1000 --create-home user

WORKDIR /app

# Copy installed packages from deps stage (no build tools in the final image).
COPY --from=deps /usr/local/lib/python3.13 /usr/local/lib/python3.13
COPY --from=deps /usr/local/bin /usr/local/bin

# Copy application source. .dockerignore keeps secrets and dev artifacts out.
COPY --chown=user:user . .

# Switch to non-root before anything runs.
USER user

# HF Spaces Docker SDK: the container MUST listen on 7860.
EXPOSE 7860

# entrypoint.sh runs alembic upgrade head then starts uvicorn.
ENTRYPOINT ["/app/entrypoint.sh"]
