import enum
from sqlalchemy import (
     Column,
     String,
     Boolean,
     DateTime,
     func,
     Integer,
     Enum
)
from sqlalchemy.orm import relationship
from app.core.database import Base


class UserRole(str, enum.Enum):
    SUPERUSER = "superuser"
    ADMIN = "admin"
    MODERATOR = "moderator"
    USER = "user"

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(30), unique=True, index=True, nullable=False)
    email = Column(String(250), unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False) 
    avatar_url = Column(String(500))
    last_login = Column(DateTime)
    is_verified = Column(Boolean, default=False)
    handle = Column(String, default=None)
    is_active = Column(Boolean, default=True)                                                 # TODO: Add duration time for this
    two_factor_enables = Column(Boolean, default=False)
    role = Column(Enum(UserRole), default=UserRole.USER)
    created_by_admin_id = Column(Integer, nullable=True)                                      # Track who created this admin
    language = Column(String(10), default="en")
    theme = Column(String(10), default="dark")
    created_date = Column(DateTime, server_default=func.now())
    updated_date = Column(DateTime, server_default=func.now(),server_onupdate=func.now())

    channel = relationship("Channel", back_populates="user", uselist=False)
    comments = relationship("Comment", back_populates="user")
    reactions = relationship("VideoReaction", back_populates="user")