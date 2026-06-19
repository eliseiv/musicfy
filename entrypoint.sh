#!/bin/sh
set -eu

echo "Running Alembic migrations..."
alembic upgrade head

echo "Starting uvicorn..."
exec uvicorn app.main:app --host "${HTTP_HOST:-0.0.0.0}" --port "${HTTP_PORT:-8000}" --workers "${UVICORN_WORKERS:-1}"
