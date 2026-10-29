#!/bin/sh

# Health server in the background so Render detects an open port
python health.py &

exec celery -A app.core.celery_app worker \
  --concurrency=1 \
  --loglevel=info \
  --max-tasks-per-child=10 \
  -Q celery