from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # Use top level .env file (one level above ./backend/)
        env_file="../.env",
        env_ignore_empty=True,
        extra="ignore",
    )
    SECRET_KEY: str = "test-secret-key-for-ci"
    SQLALCHEMY_DATABASE_URL: str = "sqlite+aiosqlite:///:memory:"
    ALGORITHM: str = "HS256"

    # S3/MinIO
    S3_ENDPOINT: str = "https://s3.filebase.io"
    #S3_PUBLIC_ENDPOINT: str = "http://localhost:9000"
    S3_ACCESS_KEY: str = "AAD4B60574E7F9BC3147"
    S3_SECRET_KEY: str = "zs6HB3TcS8axWI431KUatC2QhiwL4nVrMleUGfOl"
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
    SENTRY_DSN: str = "https://a988f88a537f6820a0eb3ef6d7a87265@sentry.hamravesh.com/11154"



@lru_cache
def get_settings():
    return Settings()

settings = get_settings()
