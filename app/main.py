from contextlib import asynccontextmanager
from app.core.redis import close_redis

from fastapi import FastAPI
from fastapi_swagger import patch_fastapi
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
from app.ws.manager import manager


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
    title="Todo Application",
    version="0.1.0",
    docs_url=None,
    swagger_ui_oauth2_redirect_url=None,
    lifespan=lifespan
)
patch_fastapi(app)


app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        # add your production domain later
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(login_router)
app.include_router(users_router)
app.include_router(videos_router)
app.include_router(channels_router)
app.include_router(categories_router)
app.include_router(video_public_router)
app.include_router(comments_router)
app.include_router(video_stats_router)
app.include_router(dashboard_router)