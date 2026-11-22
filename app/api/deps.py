from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User, UserRole
from app.core.database import AsyncSessionLocal


async def get_db():
    async with AsyncSessionLocal() as db:
        yield db


def check_admin_or_author(
    object_id: int,
    current_user: User,
    resource_type: str = "resource"  # For better error messages
) -> tuple[bool, bool]:
    """
    Check if user is admin or the author of a resource.

    Args:
        object_id: ID of the resource being accessed
        current_user: Currently authenticated user
        resource_type: Type of resource for error messages

    Returns:
        A tuple of (is_admin, is_self). The caller decides how to apply the
        permission result, while this helper raises 403 when access is denied.

    Raises:
        HTTPException: 403 if user is not authorized
    """
    is_admin = current_user.role == UserRole.ADMIN
    is_self = current_user.id == object_id

    if not (is_admin or is_self):
        raise HTTPException(
            status_code=403,
            detail=f"You have not access to this {resource_type}",
        )

    return is_admin, is_self