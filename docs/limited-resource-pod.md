# Limited-Resource Pod Deployment Guide

This document describes the changes made to prepare the Orbit backend service for deployment on a Kubernetes pod with **1 vCPU / 1 GB RAM** (typical small cloud instance or free-tier node).

---

## Summary of Changes

| Area | Change | Why |
|------|--------|-----|
| **Storage** | Filebase S3 default; MinIO only via `prod-sim` profile | Eliminates 200+ MB MinIO container in production |
| **Celery Worker** | `worker_concurrency=1`, `worker_max_tasks_per_child=10` | Single process bounds memory; recycling reclaims fragmentation |
| **Task Time Limits** | `task_soft_time_limit=8min`, `task_time_limit=9min` | Prevents stuck tasks from consuming resources indefinitely |
| **Task Acknowledgment** | `task_acks_late=True`, `task_reject_on_worker_lost=True` | Re-queues tasks if worker OOMs/crashes mid-job |
| **Video Upload Limits** | `MAX_VIDEO_SECONDS=60`, `MAX_VIDEO_SIZE_MB=100` | Rejects heavy inputs before ffmpeg runs |
| **Sprite Generation** | `SPRITE_MAX_TILES=50`, `SPRITE_COLUMNS=5` | Limits Pillow memory during sprite sheet assembly |
| **ffmpeg Encoding** | `-threads 1` flag | Binds encode to single thread, caps CPU/RAM |
| **FastAPI** | Single uvicorn worker (`--workers 1`) | Avoids multi-process memory overhead |
| **K8s Resources** | `limits.memory=1Gi`, `requests.memory=512Mi` | Guarantees scheduling headroom; OOMKills cleanly |
| **Celery Beat** | Embedded in worker via `CELERY_EMBED_BEAT=1` (default) | Single-container deployment; disable with `CELERY_EMBED_BEAT=0` for prod-sim |

---

## Configuration Details

### Celery (`app/core/celery_app.py`)

```python
celery_app.conf.update(
    worker_concurrency=1,              # 1 process only
    worker_prefetch_multiplier=1,      # Don't hoard queued tasks
    worker_max_tasks_per_child=10,     # Recycle process every 10 tasks
    task_soft_time_limit=8 * 60,       # 8 min graceful shutdown
    task_time_limit=9 * 60,            # 9 min hard kill
    task_acks_late=True,               # Acknowledge after completion
    task_reject_on_worker_lost=True,   # Re-queue on worker crash
    result_expires=60 * 60,            # 1h result TTL
    broker_connection_retry_on_startup=True,
)

celery_app.conf.beat_schedule = {
    "flush_video_stats": {
        "task": "flush_video_stats",
        "schedule": 45.0,  # every 45s
    },
}
```

### Video Task Guards (`app/tasks/video_tasks.py`)

```python
# Configurable via environment variables
MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "60"))
MAX_VIDEO_SIZE_MB = int(os.getenv("MAX_VIDEO_SIZE_MB", "100"))
SPRITE_MAX_TILES = int(os.getenv("SPRITE_MAX_TILES", "50"))
SPRITE_COLUMNS = int(os.getenv("SPRITE_COLUMNS", "5"))
X264_PRESET = os.getenv("X264_PRESET", "veryfast")

# ffmpeg command includes: -threads 1
```

**Enforcement point**: Limits are checked **inside the Celery transcode task** (`transcode_video_task`) **after the upload has been stored in the bucket**. The `/videos/complete` endpoint does **not** validate size or duration before queuing; it only verifies the object exists in S3. A rejected video:
- Gets `status="failed"` and `processing_error` set in the database
- Does **not** receive a `video.ready` WebSocket event (only `video.error`)
- Leaves the original file in the bucket (not auto-deleted; requires manual cleanup or explicit DELETE)

> **Discrepancy with `docs/upload-size-limit.md`**: That document claims enforcement at the storage layer via presigned POST policy. The current code enforces limits **after** storage, so oversized/overlong files occupy bucket space temporarily. The document's claims are not implemented.

### HLS Renditions

Only **360p** and **720p** are produced. The 1080p rendition is intentionally disabled (commented in `HLS_RENDITIONS`) to save ~60% encoding CPU. The memory budget below assumes 720p as the highest quality.

### Kubernetes Deployment (Example)

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: orbit-backend
spec:
  replicas: 1
  template:
    spec:
      containers:
      - name: backend
        env:
        - name: CELERY_EMBED_BEAT
          value: "1"  # Embed beat in worker for single-container deploy
        - name: SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: orbit-secrets
              key: secret-key
        - name: FILEBASE_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: orbit-secrets
              key: filebase-access-key
        - name: FILEBASE_SECRET_KEY
          valueFrom:
            secretKeyRef:
              name: orbit-secrets
              key: filebase-secret-key
        - name: FILEBASE_BUCKET
          valueFrom:
            secretKeyRef:
              name: orbit-secrets
              key: filebase-bucket
        resources:
          requests:
            cpu: "250m"
            memory: "512Mi"
          limits:
            cpu: "1000m"
            memory: "1Gi"
        livenessProbe:
          httpGet:
            path: /healthz
            port: 8000
          initialDelaySeconds: 60
          periodSeconds: 30
          timeoutSeconds: 10
          failureThreshold: 6
        readinessProbe:
          httpGet:
            path: /ready
            port: 8000
          initialDelaySeconds: 15
          periodSeconds: 15
          timeoutSeconds: 5
          failureThreshold: 3
```

---

## Deployment Assumptions

| Assumption | Impact |
|------------|--------|
| **Low traffic** (portfolio, not product) | Single replica sufficient; no horizontal scaling |
| **Video uploads controlled** (you or trusted users) | 60s/100 MB limits acceptable; no public abuse risk |
| **External Redis & PostgreSQL** | Not counted in pod memory; run as managed services or separate pods |
| **Filebase S3 for storage** | No local MinIO; eliminates 200+ MB MinIO container |
| **Single Celery worker** | Throughput: ~1 video at a time; queue builds under burst |
| **Celery Beat embedded** | Single container runs API + worker + beat; disable via `CELERY_EMBED_BEAT=0` for prod-sim |

---

## Caveats & Follow-Up Items

| Item | Status | Notes |
|------|--------|-------|
| **K8s resource limits** | ⚠️ Not committed | Must be added to your deployment manifests (not in repo) |
| **Health endpoints** | ✅ Exist | `/healthz` (cheap), `/ready` (checks DB+Redis) |
| **Prometheus metrics** | ❌ Missing | Add `prometheus-fastapi-instrumentator` for `container_memory_usage_bytes` alerts |
| **Log aggregation** | ❌ Missing | Ensure stdout/stderr captured by node logging agent |
| **Graceful shutdown** | ⚠️ Partial | Celery `worker_max_tasks_per_child` helps; add SIGTERM handler for in-progress ffmpeg |
| **Upload size enforcement** | ⚠️ After storage | Oversized/overlong files stored then rejected; `docs/upload-size-limit.md` claims not implemented |
| **HLS private caching** | ✅ Fixed | `Cache-Control: private` for non-public videos; S3 reads offloaded to thread pool |

---

## Memory Budget (Estimated)

| Component | RSS (Idle) | RSS (Under Load) |
|-----------|------------|------------------|
| OS / Container overhead | ~100 MB | ~100 MB |
| FastAPI (uvicorn ×1) | ~150 MB | ~180 MB |
| Celery worker (×1, with embedded beat) | ~200 MB | ~240 MB |
| ffmpeg (1 encode, 720p, 60s) | — | **300–500 MB** |
| Pillow (sprite sheet) | — | **50–100 MB** |
| **Total** | **~450 MB** | **~690–940 MB** |

**Headroom**: ~60–310 MB under load before OOMKill. The 1 Gi limit provides safety margin.

---

## Tuning Knobs (Environment Variables)

| Variable | Default | Increase If | Decrease If |
|----------|---------|-------------|-------------|
| `MAX_VIDEO_SECONDS` | 60 | Users need longer videos | OOM under load |
| `MAX_VIDEO_SIZE_MB` | 100 | Higher quality uploads | Frequent 413 errors |
| `SPRITE_MAX_TILES` | 50 | Richer timeline previews | Sprite OOM |
| `SPRITE_COLUMNS` | 5 | Wider sprite sheets | Sprite OOM |
| `X264_PRESET` | veryfast | Better compression | CPU time acceptable |
| `worker_max_tasks_per_child` | 10 | Memory fragmentation low | Frequent OOM after many tasks |
| `CELERY_EMBED_BEAT` | 1 | Single-container deploy | Prod-sim (set to 0) |

---

## Related Documentation

- `docs/upload-size-limit.md` — Claims hard 100 MB enforcement at storage layer (**not implemented**; current code enforces after storage)
- `docs/end_to_end_video_pipeline.md` — Video processing flow overview
