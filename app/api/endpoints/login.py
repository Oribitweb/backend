from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, Depends, HTTPException, status, Response, Request
from fastapi.responses import JSONResponse

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


router = APIRouter(tags=["login"])

@router.post("/register")
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
    
    raise HTTPException(
        status_code=status.HTTP_201_CREATED,
        detail="User successfully created"
    )


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

        access_token = create_access_token(subject=user.id)  # Create and store in Cookies
        refresh_token = create_refresh_token(subject=user.id)  # Create and store Refresh token

        cookie_config = {
            "httponly": True,
            "secure": False,  # Set to False for testing with HTTP
            "samesite": "lax",
            "expires": datetime.now(timezone.utc) + timedelta(days=7)
        }

        # Set cookie
        response.set_cookie(
            key="access_token",
            value=access_token,
            **cookie_config
        )

        response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            **cookie_config
        )
        # Update user last login
        user.last_login = datetime.now(timezone.utc)
        return {
            "access_token": access_token,
            "refresh_token": refresh_token
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"login error {str(e)}")
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content="Internal serve error during login"
        )


@router.post("/refresh")
async def get_refreshed_token(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db)
):
    """Get a new access token using old access token(even expired)"""
    try:
        refresh_token = request.cookies.get("refresh_token")
        user_id = decode_refresh_token(refresh_token)

        user = await get_user_by_id(db, user_id)
        if user:
            new_access = create_access_token(user_id)
        else:
            raise HTTPException(404, "User not found")

        response.set_cookie(
            key="access_token",
            value=new_access,
            httponly = True,
            secure = False,  # Set to False for testing with HTTP
            samesite = "lax",
            expires = datetime.now(timezone.utc) + timedelta(days=7)
        )
            
        return {"message": "Access token refreshed"}
    except HTTPException as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Refresh failed, {e}",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.post("/logout")
def logout_user(response: Response):
    response.delete_cookie(
        key="access_token",
        httponly=True,
        secure=False,  # Match your login configuration
        samesite="lax"
    )
    
    response.delete_cookie(
        key="refresh_token",
        httponly=True,
        secure=False,
        samesite="lax"
    )
    return {"Logout was successful"}