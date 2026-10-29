import asyncio
import uuid
import jwt
import logging
from typing import Optional
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect

from app.services.auth_service import (
    get_current_user_from_cookie,
    get_current_channel,
    get_optional_current_user_from_cookie,
    create_access_token
)
from app.api.deps import get_db
from app.models.video import Video
from app.models.channel import Channel
from app.models.user import User
from app.schemas.video import (
    UploadUrlResponse,
    VideoCompleteRequest,
    VideoCompleteResponse
)
from app.services.storage import storage_service
from app.services.public_video_id import create_public_id
from app.services.video_service import get_video_by_public_id
from app.core.config import settings
from app.tasks.video_tasks import transcode_video_task
from app.ws.manager import manager


router = APIRouter(prefix="/videos", tags=["video operations"])
logger = logging.getLogger(__name__)


# Helper: generate a unique S3 key for the video
def generate_video_key(original_filename: str) -> str:
    """Generate a unique S3 key for the original uploaded video."""
    ext = original_filename.split('.')[-1] if '.' in original_filename else 'mp4'
    unique_id = uuid.uuid4().hex
    return f"originals/{unique_id}.{ext}"


@router.get(
    "/upload-url",
    response_model=UploadUrlResponse,
    status_code=status.HTTP_201_CREATED
)
async def request_video_upload(
    filename: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    current_channel: Channel = Depends(get_current_channel)
):
    """
    Step 1: Create video record and return a pre‑signed POST URL.
    The client will upload directly to S3 using this URL.
    """

    # 1. Verify the user owns the channel
    result = await db.execute(
        select(Channel).filter(
            Channel.id == current_channel.id,
            Channel.user_id == current_user.id,
            Channel.is_suspended == False
        )
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(
            status_code=403,
            detail="Channel not found or you do not own it"
        )

    object_key = generate_video_key(filename)

    # 4. Generate pre‑signed POST for the final key
    try:
        presigned_url = await storage_service.generate_presigned_put_url(
            object_key=object_key,
            expiration= 3600,
            content_type= "video/mp4"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not create upload URL: {str(e)}")

    return UploadUrlResponse(
        upload_url=presigned_url,
        object_key=object_key,
        expires_in=3600
    )

@router.get("/thumbnail-upload-url", response_model=UploadUrlResponse)
async def get_thumbnail_upload_url(
    filename: str = "thumbnail.jpg",
    db: AsyncSession = Depends(get_db),
    content_type: str = "image/jpeg",
    current_user: User = Depends(get_current_user_from_cookie),
    current_channel: Channel = Depends(get_current_channel)
):
    """Return presigned PUT URL for a custom thumbnail."""
    # Verify channel ownership (same as video upload)
    result = await db.execute(
        select(Channel).filter(
            Channel.id == current_channel.id,
            Channel.user_id == current_user.id,
            Channel.is_suspended == False
        )
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=403, detail="Channel not owned by user")

    # Generate unique key for thumbnail
    ext = filename.split('.')[-1]
    thumbnail_key = f"thumbnails/{current_user.id}/{uuid.uuid4().hex}.{ext}"
    try:
        presigned_url = await storage_service.generate_presigned_put_url(
            object_key=thumbnail_key,
            expiration=3600,
            content_type=content_type
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not generate thumbnail upload URL: {str(e)}")

    return UploadUrlResponse(
        upload_url=presigned_url,
        object_key=thumbnail_key,
        expires_in=3600
    )

@router.post("/complete", status_code=status.HTTP_202_ACCEPTED, response_model=VideoCompleteResponse)
async def complete_upload(
    payload: VideoCompleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user_from_cookie),
    current_channel: Channel = Depends(get_current_channel)
):
    """
    Step 2: Client calls this after a successful direct upload to S3.
    Creates Video record, triggers transcoding job.
    """
    # Verify channel ownership
    result = await db.execute(
        select(Channel).filter(
            Channel.id == current_channel.id,
            Channel.user_id == current_user.id,
            Channel.is_suspended == False
        )
    )
    channel = result.scalar_one_or_none()
    if not channel:
        raise HTTPException(status_code=403, detail="Channel not found or not owned")

    # Optional: check that the object actually exists in S3
    if not await storage_service.object_exists(payload.object_key):
        raise HTTPException(status_code=400, detail="File not found in storage. Upload may have failed.")

    # Create video record
    db_video = Video(
        channel_id=channel.id,
        title=payload.title,
        description=payload.description,
        original_filename=payload.original_filename,
        file_size=payload.file_size,
        mime_type=payload.mime_type,
        original_key=payload.object_key,
        file_url=payload.object_key,      # keep for backward compatibility
        thumbnail_url=payload.thumbnail_key,
        duration_seconds=None,            # will be set by worker
        status="processing",
        visibility=payload.visibility or "public",
        category_id=payload.category_id,
        language=payload.language or "en",
        allow_comments=payload.allow_comments if payload.allow_comments is not None else True,
        is_made_for_kids=payload.is_made_for_kids or False,
        is_short=payload.is_short or False
    )

    if payload.thumbnail_key:
        # Optionally verify the thumbnail exists
        if await storage_service.object_exists(payload.thumbnail_key):
            db_video.thumbnail_key = payload.thumbnail_key
            db_video.thumbnail_url = storage_service.get_public_url(payload.thumbnail_key)

    db.add(db_video)
    await db.flush()  # ← get the auto-generated internal_id before we need it below

    await create_public_id(db, internal_id=db_video.id)

    await db.commit()
    # `db.refresh()` with no `attribute_names` only reloads column attributes,
    # not relationships. `db_video.public_id` (loaded via the `public_id_str`
    # property below) was created via create_public_id() as a separate object
    # after `db_video` was flushed, so the relationship is still unloaded.
    # Accessing it would trigger a lazy-load on the async session, raising
    # sqlalchemy.exc.MissingGreenlet. Explicitly include it here instead.
    await db.refresh(db_video, attribute_names=["public_id"])

    # Trigger Celery task
    transcode_video_task.delay(
        video_id=db_video.id,
        original_key=payload.object_key,
        bucket=settings.S3_BUCKET_NAME
    )

    return VideoCompleteResponse(
        public_id=db_video.public_id_str,
        status="processing",
        message="Video accepted for transcoding"
    )

@router.get("/{public_id}/signed-url")
async def get_video_signed_manifest_url(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_current_user_from_cookie),
):
    """
    Returns a short-lived signed URL for the HLS manifest (.m3u8).
    Only accessible if the video is ready and the user has viewing rights.
    """
    video = await get_video_by_public_id(db, public_id, current_user.id if current_user else None)

    # 1. Video must be in 'ready' state
    if video.status != "ready":
        raise HTTPException(status_code=400, detail="Video is not ready for playback")

    # 2. Access control
    #    - Owner (channel owner) can always access
    #    - Public videos are accessible to any authenticated user
    #    - Unlisted / scheduled videos are treated as private for simplicity
    #
    # `video.channel` is a lazily-loaded relationship. Accessing it directly
    # here (after the await above has returned) triggers a synchronous
    # lazy-load on what is actually an AsyncSession, which raises
    # sqlalchemy.exc.MissingGreenlet ("greenlet_spawn has not been called").
    # Explicitly awaiting db.refresh(..., attribute_names=["channel"]) loads
    # the relationship safely within an async context before we touch it.
    if "channel" not in video.__dict__:
        await db.refresh(video, attribute_names=["channel"])

    is_owner = current_user is not None and video.channel.user_id == current_user.id
    if not is_owner and video.visibility != "public":
        raise HTTPException(status_code=403, detail="You do not have permission to view this video")


    # 3. Determine the S3 key of the HLS manifest.
    #    Based on the transcoding task, the manifest is stored at:
    #       processed/{video_id}/hls/master.m3u8
    manifest_key = f"processed/{video.id}/hls/master.m3u8"

    # 4. Generate a presigned URL valid for 60 seconds (short-lived)
    try:
        signed_url = storage_service.generate_presigned_get_url(
            object_key=manifest_key,
            expiration=7200
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not generate signed URL: {str(e)}")

    return {"signed_url": signed_url}

async def _reject(ws: WebSocket, code: int, message: str, msg_type: str = "auth_error", **extra):
    """Send an error and close, tolerating a client that's already gone."""
    try:
        await ws.send_json({"type": msg_type, "message": message, **extra})
        await ws.close(code=code)
    except (WebSocketDisconnect, RuntimeError):
        pass


@router.websocket("/ws/video/{public_id}")
async def websocket_video_status(
    websocket: WebSocket,
    public_id: str,
    db: AsyncSession = Depends(get_db),
):
    await websocket.accept()
    subscribed = False

    try:
        # --- 1. Auth message ---
        try:
            auth_message = await asyncio.wait_for(websocket.receive_json(), timeout=5.0)
        except asyncio.TimeoutError:
            return await _reject(websocket, 4001, "Authentication timeout")
        except ValueError:  # invalid JSON
            return await _reject(websocket, 4001, "Invalid auth message")

        if (
            not isinstance(auth_message, dict)
            or auth_message.get("type") != "auth"
            or not auth_message.get("token")
        ):
            return await _reject(websocket, 4001, "Invalid auth message")

        # --- 2. Verify JWT ---
        try:
            payload = jwt.decode(
                auth_message["token"], settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
        except jwt.ExpiredSignatureError:
            return await _reject(websocket, 4001, "Token expired")
        except jwt.InvalidTokenError:
            return await _reject(websocket, 4001, "Invalid token")

        try:
            user_id = int(payload.get("sub") or payload.get("user_id"))
        except (TypeError, ValueError):
            return await _reject(websocket, 4001, "Token malformed")

        # --- 3. DB lookups ---
        user = (await db.execute(select(User).filter(User.id == user_id))).scalar_one_or_none()
        if not user or not user.is_active:
            return await _reject(websocket, 4001, "User not found")

        try:
            video = await get_video_by_public_id(db, public_id, user.id)
        except HTTPException as e:
            return await _reject(
                websocket,
                4004 if e.status_code == 404 else 4003,
                e.detail,
                msg_type="video.error",
                videoId=public_id,
                error=e.detail,
            )

        channel = (
            await db.execute(
                select(Channel).filter(
                    Channel.id == video.channel_id, Channel.user_id == user.id
                )
            )
        ).scalar_one_or_none()
        if not channel:
            return await _reject(websocket, 4003, "Access denied")

        # Copy what we need, then free the DB connection before the long wait
        video_status = video.status
        processing_error = video.processing_error
        await db.close()

        # --- 4. Success ---
        await websocket.send_json({"type": "auth_ok"})

        if video_status == "ready":
            await websocket.send_json({"type": "video.ready", "videoId": public_id})
            return
        if video_status == "failed":
            await websocket.send_json({
                "type": "video.error",
                "videoId": public_id,
                "error": processing_error or "Transcoding failed",
            })
            return

        # --- 5. Subscribe for updates ---
        await manager.connect(public_id, websocket)
        subscribed = True
        await manager.subscribe(public_id, websocket)

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for video {public_id}")
    except Exception:
        logger.exception(f"WebSocket error for video {public_id}")
        try:
            await websocket.send_json({"type": "error", "message": "Internal error"})
        except Exception:
            pass
    finally:
        if subscribed:
            manager.disconnect(public_id, websocket)
        try:
            await websocket.close()
        except Exception:
            pass


# Generate short-lived token for websocket
@router.get("/ws-token")
async def get_websocket_token(
    current_user: User = Depends(get_current_user_from_cookie)
):
    """
    Returns a short-lived token for WebSocket authentication.
    The HttpOnly cookie is sent automatically with this request.
    """
    # Generate a short-lived WebSocket-specific token (5 minutes)
    ws_token = create_access_token(
        subject=current_user.id,
        expires_delta=timedelta(minutes=5)
    )
    return {"token": ws_token, "expires_in": 300}