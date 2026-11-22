#!/bin/bash

# ---------------------------------------------------------------------------
# Defensive re-exec: if this script was invoked by sh/dash (which does not
# support bash-only features like `wait -n`), re-exec ourselves under bash.
# This makes the script safe even if the shebang is bypassed.
# ---------------------------------------------------------------------------
if [ -z "$BASH_VERSION" ]; then
  exec /bin/bash "$0" "$@"
fi

# Exit on unset variables and pipeline failures, but not on plain non-zero
# commands (we want to capture exit codes from wait).
set -o pipefail

# ---------------------------------------------------------------------------
# Celery Beat embedding: run beat inside the worker when CELERY_EMBED_BEAT!=0
# ---------------------------------------------------------------------------
# Default to embedding beat (single-container deployments).
# Set CELERY_EMBED_BEAT=0 to run beat as a separate process (prod-sim).
if [ -z "$CELERY_EMBED_BEAT" ] || [ "$CELERY_EMBED_BEAT" != "0" ]; then
  EMBED_BEAT_ARGS="-B -s /tmp/celerybeat-schedule"
else
  EMBED_BEAT_ARGS=""
fi

# ---------------------------------------------------------------------------
# Start Celery worker in the background (with optional embedded beat)
# ---------------------------------------------------------------------------
# worker_max_tasks_per_child is set in celery_app.py (10)
celery -A app.core.celery_app worker \
  --concurrency=1 \
  --loglevel=info \
  $EMBED_BEAT_ARGS \
  -Q celery &
CELERY_PID=$!
echo "Celery worker started with PID $CELERY_PID (embed_beat=$EMBED_BEAT_ARGS)"

# ---------------------------------------------------------------------------
# Start the API in the background (Render provides $PORT)
# ---------------------------------------------------------------------------
fastapi run --host 0.0.0.0 --port "${PORT:-8000}" &
API_PID=$!
echo "FastAPI started with PID $API_PID"

# ---------------------------------------------------------------------------
# Graceful shutdown helper
# ---------------------------------------------------------------------------
shutdown() {
  echo "Shutting down..."
  kill -TERM "$CELERY_PID" "$API_PID" 2>/dev/null || true
  wait "$CELERY_PID" "$API_PID" 2>/dev/null || true
}

# Forward Render's SIGTERM / Ctrl+C to both processes
trap shutdown TERM INT

# ---------------------------------------------------------------------------
# Block until either process exits (bash 4.3+ supports `wait -n`)
# ---------------------------------------------------------------------------
wait -n
EXIT_CODE=$?

echo "A process exited (code $EXIT_CODE), stopping the container so Render restarts it"
shutdown
exit "$EXIT_CODE"
