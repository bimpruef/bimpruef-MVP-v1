"""
document_storage.py – BIMPruef permanent project document storage

Documents are stored in Cloudflare R2 under:
  projects/{project_id}/documents/{document_id}/{safe_filename}

A ProjectDocument DB row is the authoritative record; R2 holds the bytes.
"""

import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.db import SessionLocal, init_db
from app.exceptions import NotFoundError, StorageError, ValidationError
from app.models import Project, ProjectDocument, ProjectFolder
from app.r2_storage import (
    delete_file_from_r2,
    delete_prefix_from_r2,
    download_file_from_r2,
    object_exists_in_r2,
    r2_enabled,
    upload_file_to_r2,
)

init_db()

MAX_DOCUMENT_SIZE_MB = 500
MAX_DOCUMENT_SIZE_BYTES = MAX_DOCUMENT_SIZE_MB * 1024 * 1024

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value or "")


def _validate_safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(f"Ungültige {label}.")
    return value


def _sanitize_filename(name: str) -> str:
    name = os.path.basename(name or "file")
    name = name.replace(" ", "_")
    name = SAFE_NAME_RE.sub("_", name)
    return name[:180] or "uploaded_file"


def _r2_document_key(project_id: str, document_id: str, safe_filename: str) -> str:
    return f"projects/{project_id}/documents/{document_id}/{safe_filename}"


def _r2_project_documents_prefix(project_id: str) -> str:
    return f"projects/{project_id}/documents/"


def _require_project(db, account_id: str, project_id: str) -> Project:
    account_id = _validate_safe_id(account_id, "Account-ID")
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    project = (
        db.query(Project)
        .filter(Project.account_id == account_id, Project.project_id == project_id)
        .first()
    )
    if not project:
        raise NotFoundError("Projekt nicht gefunden.")
    return project


def _doc_to_dict(doc: ProjectDocument, folder_path: str = "") -> dict:
    return {
        "document_id": doc.document_id,
        "project_id": doc.project_id,
        "folder_id": doc.folder_id or "",
        "original_filename": doc.original_filename,
        "safe_filename": doc.safe_filename,
        "file_extension": doc.file_extension,
        "content_type": doc.content_type,
        "file_size": doc.file_size,
        "r2_key": doc.r2_key,
        "document_kind": doc.document_kind or "other",
        "folder_path": folder_path,
        "created_at": _dt(doc.created_at),
        "updated_at": _dt(doc.updated_at),
    }


def _folder_to_dict(folder: ProjectFolder) -> dict:
    return {
        "folder_id": folder.folder_id,
        "project_id": folder.project_id,
        "parent_folder_id": folder.parent_folder_id or "",
        "name": folder.name,
        "path": folder.path,
        "created_at": _dt(folder.created_at),
        "updated_at": _dt(folder.updated_at),
    }


def _build_folder_path(db, folder_id: Optional[str]) -> str:
    """Walk up the folder tree and return a slash-separated path string."""
    if not folder_id:
        return ""
    parts = []
    visited = set()
    current_id = folder_id
    while current_id and current_id not in visited:
        visited.add(current_id)
        folder = db.query(ProjectFolder).filter(ProjectFolder.folder_id == current_id).first()
        if not folder:
            break
        parts.append(folder.name)
        current_id = folder.parent_folder_id
    return "/".join(reversed(parts))


# ── Folder CRUD ──────────────────────────────────────────────────────────────

def list_folders(account_id: str, project_id: str, parent_folder_id: str = "") -> list:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        q = db.query(ProjectFolder).filter(ProjectFolder.project_id == project_id)
        if parent_folder_id:
            q = q.filter(ProjectFolder.parent_folder_id == parent_folder_id)
        else:
            q = q.filter(ProjectFolder.parent_folder_id.is_(None))
        return [_folder_to_dict(f) for f in q.order_by(ProjectFolder.name).all()]


def get_folder(account_id: str, project_id: str, folder_id: str) -> Optional[dict]:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        folder = (
            db.query(ProjectFolder)
            .filter(
                ProjectFolder.project_id == project_id,
                ProjectFolder.folder_id == folder_id,
            )
            .first()
        )
        return _folder_to_dict(folder) if folder else None


def create_folder(
    account_id: str,
    project_id: str,
    name: str,
    parent_folder_id: str = "",
) -> dict:
    name = str(name or "").strip()[:180]
    if not name:
        raise ValidationError("Ordnername darf nicht leer sein.")
    with SessionLocal() as db:
        project = _require_project(db, account_id, project_id)
        parent_path = ""
        if parent_folder_id:
            parent = db.query(ProjectFolder).filter(
                ProjectFolder.project_id == project_id,
                ProjectFolder.folder_id == parent_folder_id,
            ).first()
            if not parent:
                raise NotFoundError("Übergeordneter Ordner nicht gefunden.")
            parent_path = parent.path
        path = f"{parent_path}/{name}".lstrip("/")
        now = _utcnow()
        folder = ProjectFolder(
            folder_id=uuid.uuid4().hex,
            project_id=project_id,
            parent_folder_id=parent_folder_id or None,
            name=name,
            path=path,
            created_at=now,
            updated_at=now,
        )
        db.add(folder)
        project.updated_at = now
        db.commit()
        db.refresh(folder)
        return _folder_to_dict(folder)


def delete_folder(account_id: str, project_id: str, folder_id: str) -> None:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        folder = (
            db.query(ProjectFolder)
            .filter(
                ProjectFolder.project_id == project_id,
                ProjectFolder.folder_id == folder_id,
            )
            .first()
        )
        if not folder:
            raise NotFoundError("Ordner nicht gefunden.")
        # Move documents in this folder to root
        db.query(ProjectDocument).filter(
            ProjectDocument.project_id == project_id,
            ProjectDocument.folder_id == folder_id,
        ).update({"folder_id": None}, synchronize_session=False)
        db.delete(folder)
        db.commit()


# ── Document CRUD ─────────────────────────────────────────────────────────────

def list_documents(
    account_id: str,
    project_id: str,
    folder_id: str = "",
) -> list:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        q = db.query(ProjectDocument).filter(ProjectDocument.project_id == project_id)
        if folder_id:
            q = q.filter(ProjectDocument.folder_id == folder_id)
        else:
            q = q.filter(ProjectDocument.folder_id.is_(None))
        docs = q.order_by(ProjectDocument.original_filename).all()
        result = []
        for doc in docs:
            fp = _build_folder_path(db, doc.folder_id)
            result.append(_doc_to_dict(doc, folder_path=fp))
        return result


def list_project_ifc_documents(account_id: str, project_id: str) -> list:
    """Return all IFC/IFCZIP documents in the project regardless of folder."""
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        docs = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.file_extension.in_([".ifc", ".ifczip"]),
            )
            .order_by(ProjectDocument.original_filename)
            .all()
        )
        result = []
        for doc in docs:
            fp = _build_folder_path(db, doc.folder_id)
            result.append(_doc_to_dict(doc, folder_path=fp))
        return result


def get_document(account_id: str, project_id: str, document_id: str) -> dict:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        doc = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.document_id == document_id,
            )
            .first()
        )
        if not doc:
            raise NotFoundError("Dokument nicht gefunden.")
        fp = _build_folder_path(db, doc.folder_id)
        return _doc_to_dict(doc, folder_path=fp)


def save_project_document(
    account_id: str,
    project_id: str,
    file_bytes: bytes,
    original_filename: str,
    content_type: str = "application/octet-stream",
    folder_id: str = "",
) -> dict:
    if len(file_bytes) > MAX_DOCUMENT_SIZE_BYTES:
        raise ValidationError(
            f"Datei überschreitet das Maximum von {MAX_DOCUMENT_SIZE_MB} MB."
        )
    safe_name = _sanitize_filename(original_filename)
    ext = os.path.splitext(safe_name)[1].lower() or ""
    doc_kind = "ifc" if ext in {".ifc", ".ifczip"} else "other"

    with SessionLocal() as db:
        project = _require_project(db, account_id, project_id)
        if folder_id:
            folder = db.query(ProjectFolder).filter(
                ProjectFolder.project_id == project_id,
                ProjectFolder.folder_id == folder_id,
            ).first()
            if not folder:
                folder_id = ""

        document_id = uuid.uuid4().hex
        r2_key = _r2_document_key(project_id, document_id, safe_name)
        now = _utcnow()

        doc = ProjectDocument(
            document_id=document_id,
            project_id=project_id,
            folder_id=folder_id or None,
            original_filename=original_filename[:255],
            safe_filename=safe_name,
            file_extension=ext,
            content_type=content_type[:255],
            file_size=len(file_bytes),
            r2_key=r2_key,
            document_kind=doc_kind,
            created_at=now,
            updated_at=now,
        )
        db.add(doc)
        project.updated_at = now
        db.commit()
        db.refresh(doc)
        doc_dict = _doc_to_dict(doc)

    # Upload to R2 after DB commit
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            tmp.write(file_bytes)
            tmp_path = tmp.name
        upload_file_to_r2(tmp_path, r2_key, content_type=content_type)
    except Exception as exc:
        raise StorageError(f"R2-Upload fehlgeschlagen: {exc}") from exc
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return doc_dict


def download_document_to_temp(
    account_id: str,
    project_id: str,
    document_id: str,
) -> tuple[dict, str]:
    """Download document from R2 to a temp file. Returns (doc_dict, tmp_path)."""
    doc = get_document(account_id, project_id, document_id)
    ext = doc.get("file_extension") or ""
    fd, tmp_path = tempfile.mkstemp(prefix="bimpruef-doc-", suffix=ext)
    os.close(fd)
    try:
        download_file_from_r2(doc["r2_key"], tmp_path)
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise StorageError(
            f"Dokument '{doc['original_filename']}' konnte nicht aus R2 geladen werden: {exc}"
        ) from exc
    return doc, tmp_path


def delete_document(account_id: str, project_id: str, document_id: str) -> None:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        doc = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.document_id == document_id,
            )
            .first()
        )
        if not doc:
            raise NotFoundError("Dokument nicht gefunden.")
        r2_key = doc.r2_key
        db.delete(doc)
        db.commit()
    try:
        delete_file_from_r2(r2_key)
    except Exception:
        pass


def delete_project_documents_prefix(project_id: str, strict: bool = False) -> None:
    """Delete all R2 objects under projects/{project_id}/documents/."""
    prefix = _r2_project_documents_prefix(project_id)
    try:
        delete_prefix_from_r2(prefix)
    except Exception as exc:
        if strict:
            raise StorageError(
                f"R2-Dokumente konnten nicht vollständig gelöscht werden: {exc}"
            ) from exc


def count_project_documents(account_id: str, project_id: str) -> int:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        return int(
            db.query(ProjectDocument)
            .filter(ProjectDocument.project_id == project_id)
            .count()
        )


def count_project_ifc_documents(account_id: str, project_id: str) -> int:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        return int(
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.file_extension.in_([".ifc", ".ifczip"]),
            )
            .count()
        )


def prepare_viewer_session_from_project_documents(
    account_id: str,
    project_id: str,
    document_ids: list,
    session_id: str = "",
) -> str:
    """
    Download selected IFC documents from R2 into a viewer session.

    Returns the session_id (existing or newly created).
    """
    from app.storage import create_upload_session, save_ifc_file
    from app.project_storage import get_or_create_project_session

    if not session_id:
        session_id = get_or_create_project_session(account_id, project_id)

    if not document_ids:
        raise ValidationError("Bitte mindestens ein Dokument auswählen.")

    clean_ids = []
    seen: set = set()
    for did in document_ids:
        did = str(did or "").strip()
        if did and did not in seen:
            clean_ids.append(did)
            seen.add(did)

    for slot, document_id in enumerate(clean_ids, start=1):
        doc, tmp_path = download_document_to_temp(account_id, project_id, document_id)
        try:
            with open(tmp_path, "rb") as f:
                file_bytes = f.read()
            save_ifc_file(session_id, slot, file_bytes, doc["original_filename"])
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return session_id
