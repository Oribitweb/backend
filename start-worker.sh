#!/bin/sh
rm -f /tmp/celerybeat-schedule*

exec celery -A app.core.celery_app worker \
  --concurrency=1 \
  --loglevel=info \
  --max-tasks-per-child=10 \
  -Q celery