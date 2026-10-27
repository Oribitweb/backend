from pydantic import BaseModel, Field, field_validator
from datetime import datetime, timezone
from typing import Optional, List, Dict, Literal
from enum import Enum

class VisibilityEnum(str, Enum):
    PUBLIC = "public"
    UNLISTED = "unlisted"
    PRIVATE = "private"
    SCHEDULED = "scheduled"

class LicenseEnum(str, Enum):
    STANDARD = "standard"
    CC = "cc"

# Base schema
class VideoBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    visibility: VisibilityEnum = VisibilityEnum.PUBLIC
    scheduled_at: Optional[datetime] = None
    category_id: Optional[int] = None
    language: str = Field("en", max_length=50)
    license: LicenseEnum = LicenseEnum.STANDARD
    allow_comments: bool = True
    is_made_for_kids: bool = False
    is_short: bool = False

    @field_validator("scheduled_at")
    def validate_scheduled_at(cls, v, values):
        if v and values.get("visibility") != VisibilityEnum.SCHEDULED:
            raise ValueError("scheduled_at only allowed for scheduled videos")
        if v and v <= datetime.utcnow():
            raise ValueError("scheduled_at must be in the future")
        return v

# Create video request
class VideoCreate(VideoBase):
    duration_seconds: Optional[int] = Field(None, gt=0, le=43200)  # Max 12 hours

    @field_validator("duration_seconds")
    def validate_duration(cls, v):
        if v and v <= 0:
            raise ValueError("duration_seconds must be positive")
        return v

# Update video request
class VideoUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = Field(None, max_length=5000)
    visibility: Optional[VisibilityEnum] = None
    scheduled_at: Optional[datetime] = None
    category_id: Optional[int] = None
    language: Optional[str] = Field(None, max_length=50)
    license: Optional[LicenseEnum] = None
    allow_comments: Optional[bool] = None
    is_made_for_kids: Optional[bool] = None

    @field_validator("scheduled_at")
    def validate_scheduled_at(cls, v, values):
        if v and v <= datetime.now(timezone.utc):
            raise ValueError("scheduled_at must be in the future")
        return v

# Video response schema
class VideoResponse(BaseModel):
    id: int
    public_id: Optional[str] = None
    status: str
    visibility: str
    title: str
    description: Optional[str]
    channel_name: str
    duration_seconds: Optional[int]
    thumbnail_url: Optional[str]
    hls_manifest_url: Optional[str]
    sprite_url: Optional[str]
    sprite_vtt_url: Optional[str]
    sprite_tile_width: Optional[int]
    sprite_tile_height: Optional[int]
    sprite_columns: Optional[int]
    sprite_rows: Optional[int]
    created_at: datetime
    channel_id: int
    file_url: str
    like_count: int = 0
    view_count: int = 0  # NEW: needed by the list feed cards
    published_at: Optional[datetime] = None
    updated_at: datetime

    class Config:
        from_attributes = True

# Detailed video response (for watch page)
class VideoDetailResponse(VideoResponse):
    channel_name: Optional[str] = None
    channel_avatar_url: Optional[str] = None
    view_count: int = 0
    like_count: int = 0
    comment_count: int = 0
    current_user_reaction: Optional[Literal["like", "dislike"]] = None

    # Video availability
    is_published: bool
    can_edit: bool = False  # For current user

    class Config:
        from_attributes = True

# Video list response
class VideoListResponse(BaseModel):
    items: List[VideoResponse]
    total: int
    page: int
    page_size: int
    has_next: bool

class UploadUrlResponse(BaseModel):
    upload_url: str
    object_key: str
    expires_in: int

class VideoCompleteRequest(BaseModel):
    object_key: str
    original_filename: str
    file_size: int
    mime_type: str
    title: str
    description: Optional[str] = None
    visibility: Optional[str] = "public"
    category_id: Optional[int] = None
    language: Optional[str] = "en"
    allow_comments: Optional[bool] = True
    is_made_for_kids: Optional[bool] = False
    is_short: Optional[bool] = False
    thumbnail_key: Optional[str] = None

class VideoCompleteResponse(BaseModel):
    public_id: str
    status: str
    message: str