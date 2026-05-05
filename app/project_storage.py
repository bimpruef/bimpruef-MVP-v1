"""
project_storage.py – BIMPruef project management

Stores projects in PostgreSQL.
Each project receives an upload session for the existing viewer/storage logic.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.db import SessionLocal, init_db
from app.exceptions import NotFoundError, ValidationError
from app.models import Project
from app.storage import create_upload_session, session_exists

init_db()

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(f"Invalid {label}.")
    return value


def _dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value or "")


def _project_to_dict(project: Project) -> dict:
    return {
        "project_id": project.project_id,
        "account_id": project.account_id,
        "project_code": project.project_code,
        "project_name": project.project_name,
        "description": project.description or "",
        "status": project.status or "active",
        "created_at": _dt(project.created_at),
        "updated_at": _dt(project.updated_at),
    }


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------


def get_account(account_id: str) -> dict:
    """
    Return a minimal account descriptor for *account_id*.

    The platform currently models accounts as users; this helper produces a
    lightweight dict suitable for display without loading the full User record.
    If a richer account model is introduced later this function is the single
    place to update.
    """
    account_id = str(account_id or "").strip()
    return {
        "account_id": account_id,
        "account_name": account_id,
        "workspace": "Default",
    }


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def list_projects(account_id: str) -> list:
    account_id = _validate_safe_id(account_id, "account_id")
    with SessionLocal() as db:
        projects = (
            db.query(Project)
            .filter(Project.account_id == account_id)
            .order_by(Project.created_at.desc())
            .all()
        )
        return [_project_to_dict(p) for p in projects]


def create_project(
    account_id: str,
    project_code: str,
    project_name: str,
    description: str = "",
) -> dict:
    account_id = _validate_safe_id(account_id, "account_id")
    now = _utcnow()

    project = Project(
        project_id=str(uuid.uuid4()),
        account_id=account_id,
        project_code=str(project_code or "").strip(),
        project_name=str(project_name or "").strip(),
        description=str(description or "").strip(),
        status="active",
        created_at=now,
        updated_at=now,
    )

    with SessionLocal() as db:
        db.add(project)
        db.commit()
        db.refresh(project)
        return _project_to_dict(project)


def get_project(account_id: str, project_id: str) -> Optional[dict]:
    account_id = _validate_safe_id(account_id, "account_id")
    project_id = _validate_safe_id(project_id, "project_id")

    with SessionLocal() as db:
        project = (
            db.query(Project)
            .filter(
                Project.account_id == account_id,
                Project.project_id == project_id,
            )
            .first()
        )
        return _project_to_dict(project) if project else None


def update_project(
    account_id: str,
    project_id: str,
    project_code: Optional[str] = None,
    project_name: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
) -> dict:
    """
    Update mutable fields of a project.

    Raises:
        NotFoundError: when the project does not exist.
    """
    account_id = _validate_safe_id(account_id, "account_id")
    project_id = _validate_safe_id(project_id, "project_id")

    with SessionLocal() as db:
        project = (
            db.query(Project)
            .filter(
                Project.account_id == account_id,
                Project.project_id == project_id,
            )
            .first()
        )

        if not project:
            raise NotFoundError(f"Project '{project_id}' not found.")

        if project_code is not None:
            project.project_code = project_code.strip()
        if project_name is not None:
            project.project_name = project_name.strip()
        if description is not None:
            project.description = description.strip()
        if status is not None:
            project.status = status

        project.updated_at = _utcnow()

        db.commit()
        db.refresh(project)
        return _project_to_dict(project)


def delete_project(account_id: str, project_id: str) -> bool:
    """Return True if the project was found and deleted, False otherwise."""
    account_id = _validate_safe_id(account_id, "account_id")
    project_id = _validate_safe_id(project_id, "project_id")

    with SessionLocal() as db:
        project = (
            db.query(Project)
            .filter(
                Project.account_id == account_id,
                Project.project_id == project_id,
            )
            .first()
        )
        if not project:
            return False
        db.delete(project)
        db.commit()
        return True


# ---------------------------------------------------------------------------
# Session management for projects
# ---------------------------------------------------------------------------


def get_project_session(account_id: str, project_id: str) -> Optional[str]:
    """Return the upload session ID attached to the project, or None."""
    account_id = _validate_safe_id(account_id, "account_id")
    project_id = _validate_safe_id(project_id, "project_id")

    with SessionLocal() as db:
        project = (
            db.query(Project)
            .filter(
                Project.account_id == account_id,
                Project.project_id == project_id,
            )
            .first()
        )
        if not project:
            return None
        return project.session_id or None


def get_or_create_project_session(account_id: str, project_id: str) -> str:
    """
    Return the existing upload session for the project, or create one.

    Raises:
        NotFoundError: when the project does not exist.
    """
    account_id = _validate_safe_id(account_id, "account_id")
    project_id = _validate_safe_id(project_id, "project_id")

    with SessionLocal() as db:
        project = (
            db.query(Project)
            .filter(
                Project.account_id == account_id,
                Project.project_id == project_id,
            )
            .first()
        )

        if not project:
            raise NotFoundError(f"Project '{project_id}' not found.")

        if project.session_id and session_exists(project.session_id):
            return project.session_id

        session_id = create_upload_session()
        project.session_id = session_id
        project.updated_at = _utcnow()
        db.commit()
        return session_id


def get_project_model_count(account_id: str, project_id: str) -> int:
    """Return the number of IFC models uploaded to the project's session."""
    from app.storage import get_session_slots  # local import avoids circular

    session_id = get_project_session(account_id, project_id)
    if not session_id or not session_exists(session_id):
        return 0
    return len(get_session_slots(session_id))
