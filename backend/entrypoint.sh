#!/bin/sh
# Container entrypoint.
# Runs Alembic migrations (idempotent — safe to re-run against an already-current DB)
# then starts uvicorn. Honours $PORT if the platform sets one (e.g. Render);
# falls back to 7860 for Hugging Face Spaces (which expects that port).
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting uvicorn on 0.0.0.0:${PORT:-7860}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-7860}" --workers 1
