"""
models.py – SQLAlchemy models for BIMPruef
"""

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db import Base


def _utcnow() -> datetime:
    """Return the current UTC time as a timezone-aware datetime.

    ``datetime.utcnow()`` is deprecated since Python 3.12 because it returns a
    naive datetime that is easily confused with local time. This helper
    centralises the replacement so every model uses the same idiom.
    """
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    user_id = Column(String(64), primary_key=True, index=True)
    email = Column(String(254), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)

    # Optional persönliche Account-Daten. Diese Felder gehören direkt zum
    # User/Account und werden zusammen mit dem Account gelöscht.
    full_name = Column(String(255), default="", nullable=False)
    company = Column(String(255), default="", nullable=False)
    role_title = Column(String(255), default="", nullable=False)
    phone = Column(String(80), default="", nullable=False)
    account_notes = Column(Text, default="", nullable=False)

    created_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    projects = relationship(
        "Project",
        back_populates="user",
        cascade="all, delete-orphan",
    )


class Project(Base):
    __tablename__ = "projects"

    project_id = Column(String(64), primary_key=True, index=True)
    account_id = Column(
        String(64),
        ForeignKey("users.user_id"),
        nullable=False,
        index=True,
    )

    project_code = Column(String(120), nullable=False)
    project_name = Column(String(255), nullable=False)
    description = Column(Text, default="", nullable=False)
    status = Column(String(40), default="active", nullable=False)

    # Optional viewer/session cache reference.
    # Permanent project files are stored through ProjectDocument / R2.
    session_id = Column(String(64), nullable=True)

    created_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )

    user = relationship("User", back_populates="projects")

    folders = relationship(
        "ProjectFolder",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    documents = relationship(
        "ProjectDocument",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    issues = relationship(
        "ProjectIssue",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class ProjectFolder(Base):
    __tablename__ = "project_folders"
    __table_args__ = (
        UniqueConstraint("project_id", "path", name="uq_project_folders_project_path"),
    )

    folder_id = Column(String(64), primary_key=True, index=True)
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_folder_id = Column(
        String(64),
        ForeignKey("project_folders.folder_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name = Column(String(180), nullable=False)
    path = Column(String(1000), nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    project = relationship("Project", back_populates="folders")
    parent = relationship(
        "ProjectFolder",
        remote_side=[folder_id],
        backref="children",
    )


class ProjectDocument(Base):
    __tablename__ = "project_documents"

    document_id = Column(String(64), primary_key=True, index=True)
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    folder_id = Column(
        String(64),
        ForeignKey("project_folders.folder_id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    original_filename = Column(String(255), nullable=False)
    safe_filename = Column(String(255), nullable=False)
    file_extension = Column(String(40), nullable=False, index=True)
    content_type = Column(String(255), default="application/octet-stream", nullable=False)
    file_size = Column(Integer, default=0, nullable=False)
    r2_key = Column(String(1500), nullable=False, unique=True, index=True)
    document_kind = Column(String(60), default="other", nullable=False, index=True)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    project = relationship("Project", back_populates="documents")
    folder = relationship("ProjectFolder")


class ProjectIssue(Base):
    __tablename__ = "project_issues"

    issue_id = Column(String(64), primary_key=True, index=True)
    project_id = Column(
        String(64),
        ForeignKey("projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    source_type = Column(String(60), default="manual", nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, default="", nullable=False)
    status = Column(String(60), default="open", nullable=False, index=True)
    priority = Column(String(60), default="normal", nullable=False, index=True)

    # JSON strings keep the schema compact and migration-friendly for the MVP.
    # They store document references, involved elements and optional BCF/viewpoint data.
    documents_json = Column(Text, default="[]", nullable=False)
    elements_json = Column(Text, default="{}", nullable=False)
    viewpoint_json = Column(Text, default="{}", nullable=False)

    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    project = relationship("Project", back_populates="issues")
