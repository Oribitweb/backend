 #!/bin/sh
# Remove stale SQLite files left from a previous run
rm -f /tmp/celerybeat-schedule*

exec celery -A core.celery_app worker -B \
  --concurrency=1 \
  --loglevel=info \
  --max-tasks-per-child=10 \
  -Q celery \
  --schedule=/tmp/celerybeat-schedule