# Orbit Backend

A video processing and streaming API built with FastAPI, Celery, and PostgreSQL.

## Documentation

- [Limited-Resource Pod Deployment Guide](docs/limited-resource-pod.md) — Configuration for 1 vCPU / 1 GB RAM deployments
- [End-to-End Video Pipeline](docs/end_to_end_video_pipeline.md) — Upload, transcode, HLS streaming flow
- [Upload Size Limit Enforcement](docs/upload-size-limit.md) — 100 MB hard limit at storage layer

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start API server
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 1

# Start Celery worker (separate terminal)
celery -A app.core.celery_app worker --loglevel=info --concurrency=1
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `S3_ENDPOINT` | `https://s3.filebase.io` | S3-compatible storage endpoint |
| `S3_BUCKET_NAME` | `orbit-bucket` | Bucket for videos, thumbnails, sprites |
| `MAX_VIDEO_SECONDS` | `60` | Max video duration (seconds) |
| `MAX_VIDEO_SIZE_MB` | `100` | Max upload size (MB) |
| `SPRITE_MAX_TILES` | `50` | Max sprite thumbnails per video |
| `SPRITE_COLUMNS` | `5` | Sprite sheet columns |
| `X264_PRESET` | `veryfast` | ffmpeg encoding speed preset |

## Kubernetes Deployment

See [Limited-Resource Pod Deployment Guide](docs/limited-resource-pod.md#kubernetes-deployment-example) for resource limits, probes, and memory budget.

## API Endpoints

- `POST /videos/upload-url` — Get presigned upload URL
- `POST /videos/complete` — Confirm upload, trigger transcoding
- `GET /videos/{public_id}/signed-url` — Get HLS manifest proxy URL
- `GET /videos/hls/{public_id}/{path}` — Proxy for HLS segments & sprites
- `GET /videos` — Public video feed
- `GET /videos/{public_id}` — Video detail

## Architecture

```
Client → FastAPI (API) → PostgreSQL (metadata)
                ↓
           Celery Worker → ffmpeg → Filebase S3 (processed/)
                ↓
           Redis (WebSocket pub/sub, Celery broker)
```