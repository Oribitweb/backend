import re
from datetime import datetime
from typing import Optional, List

from pydantic import BaseModel, Field, EmailStr, field_validator
from pydantic_settings import SettingsConfigDict

from app.models.user import UserRole


class PasswordValidationMixin:
    @field_validator('password', check_fields=False)
    @classmethod
    def validate_password_complexity(cls, v: str) -> str:
        pattern = r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)(?=.*[@$!%*?&#])[A-Za-z\d@$!%*?&#]{8,32}$"
        
        if not re.match(pattern, v):
            raise ValueError(
                "Password must contain at least 8 characters, including "
                "uppercase, lowercase, number and special character."
            )
        return v

    @field_validator("confirm_password", check_fields=False)
    @classmethod
    def passwords_match(cls, v, info):
        password = info.data.get("password")
        if password is None:
            return v
        if v != password:
            raise ValueError("Password confirmation must match the password")
        return v


class UserBase(BaseModel):
    username: str
    email: EmailStr


class UserCreate(UserBase, PasswordValidationMixin):
    password: str = Field(...)
    confirm_password: str = Field(description="Confirm password")
    avatar_url: Optional[str] = None
    

class UserLogin(BaseModel):
    username: str
    password: str


class UserUpdate(UserBase, PasswordValidationMixin):
    username: Optional[str] = None
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None

    # Admin admins can update these
    handle: Optional[str] = None
    is_verified: Optional[bool] = None
    is_active: Optional[bool] = None
    role: Optional[str] = None


class UserInDBBase(UserBase):
    id: int
    password_hash: str
    phone: str
    avatar_url: Optional[str] = None
    last_login: datetime
    is_verified: bool
    is_active: bool
    two_factor_enables: bool
    role: str
    language: str
    theme: str 
    created_date: datetime
    updated_date: datetime

    model_config = SettingsConfigDict(from_attributes=True)


class UserPublic(UserBase):
    """Public user information - safe to return to clients"""
    
    # Basic identification
    id: int
    
    # Profile information
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    
    # Status flags (safe to expose)
    is_verified: bool
    is_active: bool
    
    # User preferences
    role: Optional[UserRole] = None
    language: str
    theme: str
    
    # Metadata
    last_login: Optional[datetime] = None
    created_date: datetime
    updated_date: datetime


class PaginatedResponse(BaseModel):
    total: int
    page: int
    per_page: int
    total_pages: int


class UserListResponse(PaginatedResponse):
    users: List[UserPublic]