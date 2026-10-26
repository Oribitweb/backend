import os
import subprocess
import tempfile
import threading
import json
import shutil
import math
from collections import deque
from PIL import Image
import logging
import redis

from celery import shared_task
from sqlalchemy.orm import Session

from app.core.database import SyncSessionLocal
from app.services.storage import storage_service
from app.core.config import settings
from app.core.celery_app import celery_app
from app.models.channel import Channel
from app.models.video import Video
from app.models.user import User
from app.models.category import Category

logger = logging.getLogger(__name__)

# app/tasks/video_tasks.py – update the main task

# Renditions for adaptive HLS (bitrate, scale, name). Renditions taller than
# the source are skipped at plan time to avoid upscaling.
HLS_RENDITIONS = [
    {"bitrate": "800k", "scale": "640:360", "name": "360p"},
    {"bitrate": "2500k", "scale": "1280:720", "name": "720p"},
    {"bitrate": "5000k", "scale": "1920:1080", "name": "1080p"},
]


def publish_ws_event(public_id: str, payload: dict):
    """Publish an event on the video's WebSocket status Redis channel. Never raises."""
    try:
        r = redis.Redis.from_url(settings.REDIS_WS_PUBSUB)
        r.publish(f"video:{public_id}:status", json.dumps(payload))
        r.close()
    except Exception as e:
        logger.warning(f"Failed to publish WebSocket event {payload.get('type')}: {e}")



@celery_app.task(bind=True, max_retries=2)
def transcode_video_task(self, video_id: int, original_key: str, bucket: str):
    """
    Celery task:
      1. Download original video from S3 to a temp file.
      2. Probe duration/resolution.
      3. Transcode to multi-quality HLS (one ffmpeg pass per rendition).
      4. Generate sprite images for timeline preview.
      5. Generate poster thumbnail (if the user didn't upload one).
      6. Upload HLS playlist & segments + sprites + poster to S3 (processed/ prefix).
      7. Update video record with HLS URL, poster URL, duration, status.

    Every stage publishes a {"type": "video.progress", ...} event on the Redis
    channel video:{public_id}:status; the FastAPI WebSocket endpoint forwards
    these to the browser so the frontend can render a step-by-step report.
    """
    db: Session = SyncSessionLocal()
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        logger.error(f"Video {video_id} not found")
        return
    try:
        pub_id = video.public_id_str
    except Exception:
        pub_id = None

    # Step plan published with every progress event so a client that connects
    # (or reconnects) late can rebuild the whole report from any single event.
    steps_plan = [{"key": "download", "label": "Downloading original video"}]

    try:

        video.status = "processing"
        db.commit()

        def publish_progress(step_key: str, label: str, step_index=None, step_percent=None):
            if not pub_id:
                return
            payload = {
                "type": "video.progress",
                "videoId": pub_id,
                "stepKey": step_key,
                "label": label,
                "steps": steps_plan,
            }
            if step_index is not None:
                payload["stepIndex"] = step_index
            if step_percent is not None:
                payload["stepPercent"] = step_percent
            publish_ws_event(pub_id, payload)

        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "input.mp4")
            logger.info(f"Downloading {original_key} from bucket {bucket}")
            publish_progress("download", "Downloading original video", step_index=1)
            storage_service.download_file(original_key, input_path)

            # Probe duration / resolution
            publish_progress("analyze", "Analyzing video", step_index=2)
            video_info = get_video_stream_info(input_path)
            duration = video_info["duration"]
            orig_height = video_info["height"]
            video.duration_seconds = int(duration)
            db.commit()

            # The full step plan is only known now that we have the source resolution
            renditions = [r for r in HLS_RENDITIONS if int(r["scale"].split(":")[1]) <= orig_height]
            if not renditions:
                # Source is smaller than the lowest rendition — transcode to 360p anyway
                renditions = [HLS_RENDITIONS[0]]

            steps_plan = [
                {"key": "download", "label": "Downloading original video"},
                {"key": "analyze", "label": "Analyzing video"},
            ]
            steps_plan += [
                {"key": f"transcode_{r['name']}", "label": f"Transcoding {r['name']}"}
                for r in renditions
            ]
            steps_plan.append({"key": "sprites", "label": "Generating timeline previews"})
            if not video.thumbnail_url:
                steps_plan.append({"key": "thumbnail", "label": "Generating thumbnail"})
            steps_plan.append({"key": "upload", "label": "Uploading processed files"})

            def step_index(key: str):
                return next((i + 1 for i, s in enumerate(steps_plan) if s["key"] == key), None)

            # Transcode to multi-quality HLS with per-rendition progress
            output_hls_dir = os.path.join(tmpdir, "hls")
            os.makedirs(output_hls_dir, exist_ok=True)

            def on_rendition_progress(name: str, percent):
                key = f"transcode_{name}"
                publish_progress(key, f"Transcoding {name}",
                                 step_index=step_index(key), step_percent=percent)

            master_playlist = transcode_to_hls_multi_quality(
                input_path, output_hls_dir, duration, renditions,
                on_rendition_progress=on_rendition_progress
            )

            # Generate sprites for timeline preview
            publish_progress("sprites", "Generating timeline previews",
                             step_index=step_index("sprites"))
            sprite_output_dir = os.path.join(tmpdir, "sprites")
            os.makedirs(sprite_output_dir, exist_ok=True)
            sprite_data = generate_video_sprites(input_path, sprite_output_dir, duration)

            # Generate or use user-uploaded thumbnail
            if not video.thumbnail_url:
                publish_progress("thumbnail", "Generating thumbnail",
                                 step_index=step_index("thumbnail"))
                # Generate poster from first frame
                poster_path = os.path.join(tmpdir, "poster.jpg")
                generate_thumbnail(input_path, poster_path, time_seconds=5)
                poster_key = f"processed/{video_id}/poster.jpg"
                storage_service.upload_file(poster_path, poster_key, content_type="image/jpeg")
                video.thumbnail_url = storage_service.get_public_url(poster_key)
                video.thumbnail_key = poster_key

            # Collect every file that needs to go to S3, then upload with progress
            publish_progress("upload", "Uploading processed files",
                             step_index=step_index("upload"))
            hls_prefix = f"processed/{video_id}/hls"
            uploads = []
            for root, dirs, files in os.walk(output_hls_dir):
                for file in files:
                    local_path = os.path.join(root, file)
                    remote_key = f"{hls_prefix}/{os.path.relpath(local_path, output_hls_dir)}"
                    uploads.append((local_path, remote_key, get_mime_type(file)))

            sprite_prefix = f"processed/{video_id}/sprites"
            sprite_remote_key = f"{sprite_prefix}/sprites.jpg"
            vtt_remote_key = f"{sprite_prefix}/sprites.vtt"
            uploads.append((
                os.path.join(sprite_output_dir, "sprites.jpg"),
                sprite_remote_key, "image/jpeg"
            ))
            uploads.append((
                os.path.join(sprite_output_dir, "sprites.vtt"),
                vtt_remote_key, "text/vtt"
            ))

            for done, (local_path, remote_key, content_type) in enumerate(uploads, start=1):
                storage_service.upload_file(local_path, remote_key, content_type=content_type)
                publish_progress("upload", "Uploading processed files",
                                 step_index=step_index("upload"),
                                 step_percent=int(done / len(uploads) * 100))

            # Build public URLs
            hls_manifest_url = storage_service.get_public_url(f"{hls_prefix}/master.m3u8")
            sprite_url = storage_service.get_public_url(sprite_remote_key)
            sprite_vtt_url = storage_service.get_public_url(vtt_remote_key)

            # Update database with all metadata
            video.hls_manifest_url = hls_manifest_url
            video.sprite_url = sprite_url
            video.sprite_vtt_url = sprite_vtt_url
            video.sprite_tile_width = sprite_data["tile_width"]
            video.sprite_tile_height = sprite_data["tile_height"]
            video.sprite_columns = sprite_data["columns"]
            video.sprite_rows = sprite_data["rows"]
            video.status = "ready"
            db.commit()

            logger.info(f"Video {video.public_id_str} processed successfully with sprites")
            channel_key = f"video:{pub_id}:status" if pub_id else f"video:{video_id}:status"
            try:
                r = redis.Redis.from_url(settings.REDIS_WS_PUBSUB)
                r.publish(
                    channel_key,
                    json.dumps({"type": "video.ready", "videoId": video.public_id_str})
                )
                r.close()
                logger.info(f"Published video.ready event on {channel_key}")
            except Exception as e:
                logger.error(f"Failed to publish WebSocket notification: {e}")

    except Exception as e:
            logger.exception(f"Transcoding failed for video {video_id}")
            if db:
                try:
                    video = db.query(Video).filter(Video.id == video_id).first()
                    if video:
                        video.status = "failed"
                        video.processing_error = str(e)
                        db.commit()
                except Exception:
                    db.rollback()
            # Tell any connected client what happened: a retry is coming, or this
            # was the final attempt (self.retry raises MaxRetriesExceeded once the
            # budget is spent, so we must decide which event to send *before* it).
            if pub_id:
                retries_done = self.request.retries
                if retries_done < (self.max_retries or 0):
                    publish_ws_event(pub_id, {
                        "type": "video.progress",
                        "videoId": pub_id,
                        "stepKey": "retry",
                        "label": f"Attempt {retries_done + 1} failed — retrying in 60s",
                        "steps": steps_plan,
                    })
                else:
                    publish_ws_event(pub_id, {
                        "type": "video.error",
                        "videoId": pub_id,
                        "error": str(e)
                    })
            raise self.retry(exc=e, countdown=60)
    finally:
        db.close()


# ---- Helper functions (use ffmpeg/ffprobe from system) ----
def get_video_duration(file_path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", file_path],
        capture_output=True, text=True
    )
    return float(result.stdout.strip())

def get_video_stream_info(file_path: str) -> dict:
    """Return video width, height, and duration using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    video_stream = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if not video_stream:
        raise ValueError("No video stream found")
    return {
        "width": int(video_stream.get("width", 0)),
        "height": int(video_stream.get("height", 0)),
        "duration": float(data["format"]["duration"])
    }

def run_ffmpeg_with_progress(cmd: list, duration_seconds: float, on_percent) -> None:
    """
    Run an ffmpeg command and parse its `-progress pipe:1` output, invoking
    on_percent(0-100) as the encode advances. Raises CalledProcessError on a
    non-zero exit, with the last stderr lines attached.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stderr_tail = deque(maxlen=30)

    def drain_stderr():
        for line in proc.stderr:
            stderr_tail.append(line.rstrip())

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    last_percent = -1
    try:
        for line in proc.stdout:
            # ffmpeg emits both out_time_us and out_time_ms (the latter is also
            # microseconds, despite the name) as plain `key=value` lines.
            if not (line.startswith("out_time_us=") or line.startswith("out_time_ms=")):
                continue
            try:
                out_time_us = int(line.split("=", 1)[1])
            except ValueError:
                continue
            if duration_seconds <= 0:
                continue
            percent = max(0, min(99, int(out_time_us / (duration_seconds * 1_000_000) * 100)))
            if percent != last_percent:
                last_percent = percent
                if on_percent:
                    on_percent(percent)
        returncode = proc.wait()
    finally:
        stderr_thread.join(timeout=5)

    if returncode != 0:
        raise subprocess.CalledProcessError(returncode, cmd, stderr="\n".join(stderr_tail))

def transcode_to_hls_multi_quality(input_path: str, output_dir: str, duration: float,
                                   renditions: list, on_rendition_progress=None) -> str:
    """
    Generate adaptive HLS with multiple qualities.
    `renditions` is the pre-filtered rendition list (already excludes
    resolutions taller than the source, to avoid upscaling).
    `on_rendition_progress(name, percent)` is called while each rendition encodes.
    Returns path to master.m3u8.
    """
    variant_playlists = []
    for r in renditions:
        variant_dir = os.path.join(output_dir, r["name"])
        os.makedirs(variant_dir, exist_ok=True)
        playlist_path = os.path.join(variant_dir, "playlist.m3u8")
        cmd = [
            "ffmpeg", "-nostats", "-loglevel", "error", "-progress", "pipe:1",
            "-i", input_path,
            "-c:v", "libx264", "-b:v", r["bitrate"],
            "-vf", f"scale={r['scale']}", "-preset", "medium",
            "-c:a", "aac", "-b:a", "128k",
            "-hls_time", "6",
            "-hls_playlist_type", "vod",
            "-hls_segment_filename", os.path.join(variant_dir, "segment_%03d.ts"),
            playlist_path
        ]
        if on_rendition_progress:
            on_rendition_progress(r["name"], 0)

        def report(pct, name=r["name"]):
            if on_rendition_progress:
                on_rendition_progress(name, pct)

        run_ffmpeg_with_progress(cmd, duration, report)
        # Create variant entry for master playlist
        bandwidth = int(r["bitrate"].replace("k", "000"))
        variant_playlists.append({
            "path": f"{r['name']}/playlist.m3u8",
            "bandwidth": bandwidth,
            "resolution": r["scale"]
        })

    # Generate master playlist (variant.m3u8)
    master_path = os.path.join(output_dir, "master.m3u8")
    with open(master_path, "w") as f:
        f.write("#EXTM3U\n")
        for v in variant_playlists:
            f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={v["bandwidth"]},RESOLUTION={v["resolution"]}\n')
            f.write(f'{v["path"]}\n')
    return master_path

def generate_thumbnail(input_path: str, output_path: str, time_seconds: int = 5):
    subprocess.run(
        ["ffmpeg", "-i", input_path, "-ss", str(time_seconds),
         "-vframes", "1", "-q:v", "2", output_path],
        check=True, capture_output=True
    )

def get_mime_type(filename: str) -> str:
    if filename.endswith(".m3u8"):
        return "application/vnd.apple.mpegurl"
    elif filename.endswith(".ts"):
        return "video/MP2T"
    elif filename.endswith(".jpg") or filename.endswith(".jpeg"):
        return "image/jpeg"
    else:
        return "application/octet-stream"

def generate_video_sprites(input_path: str, output_dir: str, video_duration: float) -> dict:
    """
    Generate sprite sheet and VTT file for timeline preview.
    
    Args:
        input_path: Path to input video file
        output_dir: Directory to save generated files
        video_duration: Duration in seconds
    
    Returns:
        Dictionary with sprite metadata:
        {
            "sprite_url": "processed/{video_id}/sprites.jpg",
            "sprite_vtt_url": "processed/{video_id}/sprites.vtt",
            "tile_width": 160,
            "tile_height": 90,
            "columns": 10,
            "rows": 5
        }
    """
    # Configuration
    sprite_interval = settings.SPRITE_INTERVAL     # seconds between thumbnails
    tile_width = settings.SPRITE_TILE_WIDTH        # width of each thumbnail
    tile_height = settings.SPRITE_TILE_HEIGHT      # height of each thumbnail (16:9)
    columns = settings.SPRITE_COLUMNS              # number of tiles per row
    max_tiles = settings.SPRITE_MAX_TILES          # maximum sprites to generate (avoid huge files)
    
    # Calculate number of thumbnails needed
    num_thumbnails = min(math.ceil(video_duration / sprite_interval), max_tiles)
    rows = math.ceil(num_thumbnails / columns)
    
    # Create a temporary directory for individual thumbnails
    thumbnails_dir = os.path.join(output_dir, "thumbnails_temp")
    os.makedirs(thumbnails_dir, exist_ok=True)
    
    # Extract thumbnails at intervals using FFmpeg
    thumbnail_pattern = os.path.join(thumbnails_dir, "thumb_%04d.jpg")
    ffmpeg_cmd = [
        "ffmpeg", "-i", input_path,
        "-vf", f"fps=1/{sprite_interval},scale={tile_width}:{tile_height}",
        "-frames:v", str(num_thumbnails),
        "-q:v", "3",  # Quality (2-5 is good for JPEG)
        thumbnail_pattern
    ]
    subprocess.run(ffmpeg_cmd, check=True, capture_output=True)
    
    # Get list of generated thumbnails
    thumb_files = sorted([f for f in os.listdir(thumbnails_dir) if f.endswith('.jpg')])
    thumb_paths = [os.path.join(thumbnails_dir, f) for f in thumb_files]
    
    # Create sprite sheet using Pillow
    sprite_width = columns * tile_width
    sprite_height = rows * tile_height
    sprite_image = Image.new('RGB', (sprite_width, sprite_height), (0, 0, 0))
    
    for idx, thumb_path in enumerate(thumb_paths):
        if idx >= num_thumbnails:
            break
        col = idx % columns
        row = idx // columns
        x = col * tile_width
        y = row * tile_height
        
        thumb = Image.open(thumb_path)
        sprite_image.paste(thumb, (x, y))
        thumb.close()
    
    # Save sprite sheet
    sprite_path = os.path.join(output_dir, "sprites.jpg")
    sprite_image.save(sprite_path, quality=85, optimize=True)
    sprite_image.close()
    
    # Generate VTT file
    vtt_path = os.path.join(output_dir, "sprites.vtt")
    with open(vtt_path, "w") as vtt_file:
        vtt_file.write("WEBVTT\n\n")
        
        for idx in range(num_thumbnails):
            start_time = idx * sprite_interval
            end_time = min((idx + 1) * sprite_interval, video_duration)
            
            # Format time as HH:MM:SS.mmm
            start_str = format_time(start_time)
            end_str = format_time(end_time)
            
            col = idx % columns
            row = idx // columns
            x = col * tile_width
            y = row * tile_height
            
            vtt_file.write(f"{start_str} --> {end_str}\n")
            vtt_file.write(f"sprites.jpg#xywh={x},{y},{tile_width},{tile_height}\n\n")
    
    # Clean up temporary thumbnails
    shutil.rmtree(thumbnails_dir)
    
    return {
        "sprite_filename": "sprites.jpg",
        "vtt_filename": "sprites.vtt",
        "tile_width": tile_width,
        "tile_height": tile_height,
        "columns": columns,
        "rows": rows
    }

def format_time(seconds: float) -> str:
    """Convert seconds to VTT timestamp format HH:MM:SS.mmm"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return f"{hours:02d}:{minutes:02d}:{secs:06.3f}"