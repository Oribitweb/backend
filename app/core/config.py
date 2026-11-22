from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache
import sys


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )
    SECRET_KEY: str = ""
    SQLALCHEMY_DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    ALGORITHM: str = "HS256"

    # S3/MinIO
    S3_ENDPOINT: str = "https://s3.filebase.io"
    #S3_PUBLIC_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_BUCKET_NAME: str = "orbit-bucket"
    S3_REGION: str = "auto"
    S3_USE_PATH_STYLE: bool = True
    S3_USE_SSL: bool = False

    # Celery
    CELERY_BROKER_URL: str = "redis://redis:6379/0"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/0"

    # Sprite image
    SPRITE_INTERVAL: int = 5
    SPRITE_TILE_WIDTH: int = 160
    SPRITE_TILE_HEIGHT: int = 90
    SPRITE_COLUMNS: int = 10
    SPRITE_MAX_TILES: int = 100

    # Redis for websocket pupsub
    REDIS_WS_PUBSUB: str = "redis://redis:6379/1"

    # Sentry
    SENTRY_DSN: str = "https://***@sentry.hamravesh.com/11154"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Fail fast on missing secrets at import/startup, unless running under pytest
        if "pytest" not in sys.modules:
            if not self.SECRET_KEY:
                raise RuntimeError("SECRET_KEY is not set")
            if not self.S3_ACCESS_KEY or not self.S3_SECRET_KEY:
                raise RuntimeError("S3_ACCESS_KEY and S3_SECRET_KEY are not set")


@lru_cache
def get_settings():
    return Settings()

settings = get_settings()
