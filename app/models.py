"""
models.py – SQLAlchemy models for BIMPruef
"""

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import relationship

from app.db import Base


class User(Base):
    __tablename__ = "users"

    user_id = Column(String(64), primary_key=True, index=True)
    email = Column(String(254), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    projects = relationship(
        "Project",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Project(Base):
    __tablename__ = "projects"

    project_id = Column(String(64), primary_key=True, index=True)
    account_id = Column(String(64), ForeignKey("users.user_id"), nullable=False, index=True)

    project_code = Column(String(120), nullable=False)
    project_name = Column(String(255), nullable=False)
    description = Column(Text, default="", nullable=False)
    status = Column(String(40), default="active", nullable=False)

    session_id = Column(String(64), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    user = relationship("User", back_populates="projects")
