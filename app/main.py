from contextlib import asynccontextmanager
from app.core.redis import close_redis

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.api.endpoints.users import router as users_router 
from app.api.endpoints.login import router as login_router
from app.api.endpoints.videos import router as videos_router
from app.api.endpoints.channels import router as channels_router
from app.api.endpoints.categories import router as categories_router
from app.api.endpoints.public import router as video_public_router
from app.api.endpoints.comments import router as comments_router
from app.api.endpoints.video_stats import router as video_stats_router
from app.api.endpoints.dashboard import router as dashboard_router
from app.core.database import Base, engine
from app.core.config import settings
from app.ws.manager import manager
import sentry_sdk

from celery.result import AsyncResult
from app.core.celery_app import celery_app
from app.tasks.video_stats_tasks import add, slow_task

sentry_sdk.init(
    dsn=settings.SENTRY_DSN,
    # Add data like request headers and IP for users,
    # see https://docs.sentry.io/platforms/python/data-management/data-collected/ for more info
)


@asynccontextmanager
async def lifespan(app:FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    print("✅ Database tables created successfully")
    await manager.initialize()
    yield
    print("Shuting down..")
    await manager.close()
    await close_redis()


app = FastAPI(
    title="Orbit",
    description="Sharing video service",
    version="0.1.0",
    lifespan=lifespan
)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://oribitweb.github.io",
        "http://localhost:5500",
        "http://127.0.0.1:5500",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],  
    allow_headers=["*"],
)

@app.get("/is-ready", status_code=200)
async def readiness():
    return JSONResponse(content="ok")

# Verify sentry
# @app.get("/sentry-debug")
# async def trigger_error():
#     division_by_zero = 1 / 0

@app.post("/add")
def enqueue_add(x: int, y: int):
    task = add.delay(x, y)
    return {"task_id": task.id}

@app.post("/slow")
def enqueue_slow(seconds: int = 5):
    task = slow_task.delay(seconds)
    return {"task_id": task.id}

@app.get("/tasks/{task_id}")
def get_task_status(task_id: str):
    result = AsyncResult(task_id, app=celery_app)
    return {
        "task_id": task_id,
        "status": result.status,          # PENDING / STARTED / SUCCESS / FAILURE
        "result": result.result if result.ready() else None,
    }

app.include_router(login_router)
app.include_router(users_router)
app.include_router(videos_router)
app.include_router(channels_router)
app.include_router(categories_router)
app.include_router(video_public_router)
app.include_router(comments_router)
app.include_router(video_stats_router)
app.include_router(dashboard_router)