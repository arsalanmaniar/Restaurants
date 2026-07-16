#!/bin/sh
# Container entrypoint for HF Spaces.
# Runs Alembic migrations (idempotent — safe to re-run against an already-current DB)
# then starts uvicorn on the port HF Spaces expects.
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting uvicorn on 0.0.0.0:7860..."
exec uvicorn app.main:app --host 0.0.0.0 --port 7860 --workers 1
