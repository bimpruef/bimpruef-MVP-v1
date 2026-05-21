"""
project_storage.py – BIMPruef project management

Projects are stored in PostgreSQL. Documents are the permanent project file
source. The attached viewer session is only a derived runtime cache that can be
rebuilt from project Documents.
"""

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.db import SessionLocal, init_db
from app.exceptions import NotFoundError, StorageError, ValidationError
from app.models import Project, ProjectDocument, ProjectFolder
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


def delete_project(account_id: str, project_id: str) -> None:
    """
    Delete a project completely.
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

        session_id = project.session_id

        try:
            from app.document_storage import delete_project_documents_prefix
            delete_project_documents_prefix(project_id, strict=True)
        except StorageError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise StorageError(
                f"Projektdokumente konnten nicht vollständig gelöscht werden: {exc}"
            ) from exc

        if session_id:
            try:
                from app.storage import delete_session
                delete_session(session_id, strict=True)
            except StorageError:
                db.rollback()
                raise
            except Exception as exc:
                db.rollback()
                raise StorageError(
                    f"Viewer-Session-Dateien konnten nicht vollständig gelöscht werden: {exc}"
                ) from exc

        try:
            db.query(ProjectDocument).filter(ProjectDocument.project_id == project_id).delete(synchronize_session=False)
            db.query(ProjectFolder).filter(ProjectFolder.project_id == project_id).delete(synchronize_session=False)
            db.delete(project)
            db.commit()
        except Exception:
            db.rollback()
            raise


# ---------------------------------------------------------------------------
# Session management for projects
# ---------------------------------------------------------------------------


def get_project_session(account_id: str, project_id: str) -> Optional[str]:
    """Return the upload/runtime session ID attached to the project, or None."""
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


def _sync_viewer_cache_from_documents(account_id: str, project_id: str, session_id: str) -> str:
    """
    Rebuild the viewer runtime cache from the project's IFC/IFCZIP documents.

    Documents remain the permanent source. The session is only a cache used by
    the existing viewer, list, clash and rule-check code paths.
    """
    from app.document_storage import (
        list_project_ifc_documents,
        prepare_viewer_session_from_project_documents,
    )

    docs = list_project_ifc_documents(account_id, project_id)
    if not docs:
        return session_id

    return prepare_viewer_session_from_project_documents(
        account_id,
        project_id,
        [d["document_id"] for d in docs],
        session_id=session_id,
    )


def get_or_create_project_session(account_id: str, project_id: str) -> str:
    """
    Return the project's runtime viewer session and keep it synchronized with
    Documents when IFC/IFCZIP files exist.

    The session is no longer a separate upload source. It is rebuilt from
    project Documents so /projects/{project_id}/model can open directly without
    a separate Viewer upload or manual file selection.
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
            session_id = project.session_id
        else:
            session_id = create_upload_session()
            project.session_id = session_id
            project.updated_at = _utcnow()
            db.commit()

    try:
        return _sync_viewer_cache_from_documents(account_id, project_id, session_id)
    except Exception:
        # Dashboard and navigation must not fail because of a transient R2/cache
        # issue. The Model route can still show the Documents-based selection or
        # an error message from its own flow.
        return session_id


def get_all_project_session_ids() -> set[str]:
    """Return all upload/runtime session IDs currently attached to projects."""
    with SessionLocal() as db:
        rows = (
            db.query(Project.session_id)
            .filter(Project.session_id.isnot(None))
            .all()
        )
        return {str(row[0]) for row in rows if row and row[0]}


def get_project_model_count(account_id: str, project_id: str) -> int:
    """Return the number of IFC/IFCZIP documents in the project's Documents area."""
    from app.document_storage import count_project_ifc_documents
    return count_project_ifc_documents(account_id, project_id)


def get_project_document_count(account_id: str, project_id: str) -> int:
    """Return the number of all documents stored for the project."""
    from app.document_storage import count_project_documents
    return count_project_documents(account_id, project_id)
