from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from app.core.config import settings

# Example async URLs:
# SQLALCHEMY_DATABASE_URL = "sqlite+aiosqlite:///./test.db"
# SQLALCHEMY_DATABASE_URL = "postgresql+asyncpg://user:password@postgresserver:5432/db"
# SQLALCHEMY_DATABASE_URL = "mysql+aiomysql://username:password@localhost/db_name"

SQLALCHEMY_DATABASE_URL = settings.SQLALCHEMY_DATABASE_URL

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    pool_pre_ping=True,      # test connection before use, replace if dead
    pool_recycle=300,        # recycle connections every 5 min (below the pooler's idle timeout)
    pool_size=5,
    max_overflow=10,
    pool_timeout=30,
)

# Convert async URL to sync for Celery worker use
SYNC_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace("postgresql+asyncpg", "postgresql+psycopg2")

sync_engine = create_engine(SYNC_DATABASE_URL)
SyncSessionLocal = sessionmaker(bind=sync_engine)


AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session