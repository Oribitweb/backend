import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, Depends, HTTPException, status, Response, Request

from app.schemas.user import UserCreate, UserLogin
from app.api.deps import get_db
from app.crud.users import (
    get_user_by_id,
    get_user_by_username,
    get_user_by_email,
    get_user_by_phone,
    create_user,
    authenticate
)
from app.services.auth_service import create_refresh_token, create_access_token, decode_refresh_token
from app.core.security import get_password_hash


logger = logging.getLogger(__name__)

router = APIRouter(tags=["login"])

# ---------------------------------------------------------------------------
# Shared cookie settings: used by login, refresh AND logout so the attributes
# always match (otherwise the browser may not delete / overwrite the cookie).
#
# - Frontend and API on the SAME site (e.g. app.example.com + api.example.com):
#       samesite="lax" (or "strict")
# - Frontend on a DIFFERENT site: samesite="none" (requires secure=True)
#   and you should add CSRF protection.
# - For local testing over plain HTTP: set secure=False and samesite="lax".
# ---------------------------------------------------------------------------
COOKIE_KWARGS = {
    "httponly": True,
    "secure": True,
    "samesite": "none",
    "path": "/",
}

ACCESS_COOKIE_LIFETIME = timedelta(days=7)    # can be shorter than the refresh cookie
REFRESH_COOKIE_LIFETIME = timedelta(days=7)


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_user(
    request: UserCreate,
    db: AsyncSession = Depends(get_db)
):
    """
    Register new user using payload data

    **request**: User basic data
    **db**: database Session local
    Returns:
    - Json massage if everything ok
    """
    existing_user = await get_user_by_username(db, request.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Username already registered"
        )

    existing_email = await get_user_by_email(db, request.email)
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Email already registered"
        )

    # Hash password
    password_hash = get_password_hash(request.password)

    user_data = request.model_dump()
    user_data.pop("password")
    user_data.pop("confirm_password")
    user_data["password_hash"] = password_hash

    await create_user(db, user_data)

    return {"message": "User successfully created"}


@router.post("/login")
async def login_user(
    response: Response,
    request: UserLogin,
    db: AsyncSession = Depends(get_db)
):
    try:
        user = await authenticate(db=db, username=request.username, password=request.password)
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid password or username"
            )

        access_token = create_access_token(subject=user.id)
        refresh_token = create_refresh_token(subject=user.id)

        now = datetime.now(timezone.utc)

        response.set_cookie(
            key="access_token",
            value=access_token,
            expires=now + ACCESS_COOKIE_LIFETIME,
            **COOKIE_KWARGS
        )
        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            expires=now + REFRESH_COOKIE_LIFETIME,
            **COOKIE_KWARGS
        )

        # Update user last login
        # NOTE: remove the commit below if get_db / authenticate already commits
        user.last_login = now.replace(tzinfo=None)
        await db.commit()

        # Tokens are only in HttpOnly cookies now, not in the body, so JS
        # (and any XSS) can't read or store them. If your frontend still
        # reads them from the body, return them here again.
        return {"message": "Login successful"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("Unexpected error during login")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Internal server error during login"
        )


@router.post("/refresh")
async def get_refreshed_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    """Get a new access token using the refresh token cookie"""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Refresh failed",
        headers={"WWW-Authenticate": "Bearer"},
    )

    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise unauthorized

    try:
        user_id = decode_refresh_token(refresh_token)
    except Exception:
        # invalid / expired / malformed token
        raise unauthorized

    user = await get_user_by_id(db, user_id)
    if not user:
        raise unauthorized

    new_access = create_access_token(user_id)

    response.set_cookie(
        key="access_token",
        value=new_access,
        expires=datetime.now(timezone.utc) + ACCESS_COOKIE_LIFETIME,
        **COOKIE_KWARGS
    )

    return {"message": "Access token refreshed"}


@router.post("/logout")
def logout_user(response: Response):
    # Attributes must match the ones used in set_cookie
    response.delete_cookie(key="access_token", **COOKIE_KWARGS)
    response.delete_cookie(key="refresh_token", **COOKIE_KWARGS)
    return {"message": "Logout was successful"}