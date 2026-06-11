
"""
document_storage.py – Project-based document storage for BIMPruef

Documents are the permanent file source for a project.
R2 stores the binary objects; PostgreSQL stores file metadata and the folder tree.

Viewer, clash, list and checking modules load IFC files directly from
ProjectDocument/R2 by document_id.

No viewer session slot cache is used.
"""

import mimetypes
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, engine, init_db
from app.exceptions import NotFoundError, StorageError, ValidationError, ConflictError
from app.models import Project, ProjectDocument, ProjectFolder


try:
    from app.r2_storage import (
        delete_file_from_r2,
        delete_prefix_from_r2,
        download_file_from_r2,
        r2_enabled,
        upload_file_to_r2,
    )
except Exception:  # local development without R2
    delete_file_from_r2 = None
    delete_prefix_from_r2 = None
    download_file_from_r2 = None
    r2_enabled = lambda: False
    upload_file_to_r2 = None

init_db()

MAX_DOCUMENT_SIZE_MB = 250
MAX_DOCUMENT_SIZE_BYTES = MAX_DOCUMENT_SIZE_MB * 1024 * 1024

ALLOWED_DOCUMENT_EXTENSIONS = {
    ".ifc", ".ifczip", ".bcf", ".bcfzip",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt",
    ".zip", ".jpg", ".jpeg", ".png", ".webp",
    ".dwg", ".dxf", ".xml", ".json", ".rtf",
}

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SAFE_FOLDER_RE = re.compile(r"^[A-Za-z0-9ÄÖÜäöüß _.-]{1,120}$")

def sanitize_filename(filename: str) -> str:
    filename = filename or "uploaded.ifc"
    filename = os.path.basename(filename)
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return filename[:180] or "uploaded.ifc"

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(f"Ungültige {label}.")
    return value


def _dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value or "")


def _r2_available() -> bool:
    try:
        return bool(r2_enabled())
    except Exception:
        return False


def _require_r2() -> None:
    if not _r2_available() or upload_file_to_r2 is None:
        raise StorageError("Cloudflare R2 ist nicht konfiguriert; Dokumente können nicht dauerhaft gespeichert werden.")


def _document_to_dict(doc: ProjectDocument) -> dict:
    folder_path = doc.folder.path if getattr(doc, "folder", None) else ""
    return {
        "document_id": doc.document_id,
        "project_id": doc.project_id,
        "folder_id": doc.folder_id or "",
        "folder_path": folder_path,
        "original_filename": doc.original_filename,
        "safe_filename": doc.safe_filename,
        "file_extension": doc.file_extension,
        "content_type": doc.content_type,
        "file_size": int(doc.file_size or 0),
        "r2_key": doc.r2_key,
        "document_kind": doc.document_kind,
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


def _clean_folder_name(name: str) -> str:
    name = str(name or "").strip().strip("/\\")
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise ValidationError("Bitte einen Ordnernamen eingeben.")
    if "/" in name or "\\" in name or ".." in name:
        raise ValidationError("Ordnername darf keine Pfadzeichen enthalten.")
    if not SAFE_FOLDER_RE.fullmatch(name):
        raise ValidationError("Ordnername enthält nicht erlaubte Zeichen.")
    return name[:120]


def _normalise_folder_path(path: str) -> str:
    path = str(path or "").strip().replace("\\", "/").strip("/")
    if not path:
        return ""
    parts = [_clean_folder_name(part) for part in path.split("/") if part.strip()]
    return "/".join(parts)


def _extension(filename: str) -> str:
    safe = sanitize_filename(filename)
    ext = Path(safe).suffix.lower()
    return ext


def document_kind_for_extension(ext: str) -> str:
    ext = str(ext or "").lower()
    if ext == ".ifc":
        return "ifc_model"
    if ext == ".ifczip":
        return "ifc_zip"
    if ext in {".bcf", ".bcfzip"}:
        return "bcf"
    if ext == ".pdf":
        return "pdf"
    if ext in {".xls", ".xlsx", ".csv"}:
        return "spreadsheet"
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        return "image"
    if ext == ".zip":
        return "archive"
    if ext in {".doc", ".docx", ".txt", ".rtf"}:
        return "text"
    return "other"


def _project_document_prefix(project_id: str) -> str:
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    return f"projects/{project_id}/documents/"


def build_document_r2_key(project_id: str, folder_path: str, document_id: str, safe_filename: str) -> str:
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    safe_filename = sanitize_filename(safe_filename)
    folder_path = _normalise_folder_path(folder_path)
    if folder_path:
        return f"projects/{project_id}/documents/{folder_path}/{document_id}_{safe_filename}"
    return f"projects/{project_id}/documents/{document_id}_{safe_filename}"


def _get_project_for_account(db, account_id: str, project_id: str) -> Project:
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


def ensure_document_schema() -> None:
    """Idempotent migration for existing Render/PostgreSQL databases."""
    try:
        init_db()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        with engine.begin() as conn:
            if "project_folders" in tables:
                existing = {c["name"] for c in inspector.get_columns("project_folders")}
                cols = {
                    "parent_folder_id": "VARCHAR(64)",
                    "updated_at": "TIMESTAMP WITH TIME ZONE",
                }
                for name, ddl in cols.items():
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE project_folders ADD COLUMN {name} {ddl}"))
            if "project_documents" in tables:
                existing = {c["name"] for c in inspector.get_columns("project_documents")}
                cols = {
                    "content_type": "VARCHAR(255) NOT NULL DEFAULT 'application/octet-stream'",
                    "document_kind": "VARCHAR(60) NOT NULL DEFAULT 'other'",
                    "updated_at": "TIMESTAMP WITH TIME ZONE",
                }
                for name, ddl in cols.items():
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE project_documents ADD COLUMN {name} {ddl}"))
    except Exception:
        # Startup should not die during a transient DB issue. Actual DB use will
        # still raise clear exceptions.
        pass


ensure_document_schema()


def list_folders(account_id: str, project_id: str, parent_folder_id: str = "") -> list[dict]:
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        q = db.query(ProjectFolder).filter(ProjectFolder.project_id == project_id)
        if parent_folder_id:
            q = q.filter(ProjectFolder.parent_folder_id == parent_folder_id)
        else:
            q = q.filter(ProjectFolder.parent_folder_id.is_(None))
        return [_folder_to_dict(f) for f in q.order_by(ProjectFolder.name.asc()).all()]


def get_folder(account_id: str, project_id: str, folder_id: str) -> Optional[dict]:
    folder_id = str(folder_id or "").strip()
    if not folder_id:
        return None
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        folder = (
            db.query(ProjectFolder)
            .filter(ProjectFolder.project_id == project_id, ProjectFolder.folder_id == folder_id)
            .first()
        )
        return _folder_to_dict(folder) if folder else None


def create_folder(account_id: str, project_id: str, name: str, parent_folder_id: str = "") -> dict:
    name = _clean_folder_name(name)
    parent_folder_id = str(parent_folder_id or "").strip() or None
    now = _utcnow()
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        parent = None
        if parent_folder_id:
            parent = (
                db.query(ProjectFolder)
                .filter(ProjectFolder.project_id == project_id, ProjectFolder.folder_id == parent_folder_id)
                .first()
            )
            if not parent:
                raise NotFoundError("Übergeordneter Ordner nicht gefunden.")
        path = f"{parent.path}/{name}" if parent else name
        path = _normalise_folder_path(path)
        duplicate = (
            db.query(ProjectFolder)
            .filter(ProjectFolder.project_id == project_id, ProjectFolder.path == path)
            .first()
        )
        if duplicate:
            raise ConflictError("Ein Ordner mit diesem Namen existiert hier bereits.")
        folder = ProjectFolder(
            folder_id=uuid.uuid4().hex,
            project_id=project_id,
            parent_folder_id=parent_folder_id,
            name=name,
            path=path,
            created_at=now,
            updated_at=now,
        )
        db.add(folder)
        db.commit()
        db.refresh(folder)
        return _folder_to_dict(folder)


def delete_folder(account_id: str, project_id: str, folder_id: str) -> None:
    folder_id = _validate_safe_id(folder_id, "Ordner-ID")
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        folder = (
            db.query(ProjectFolder)
            .filter(ProjectFolder.project_id == project_id, ProjectFolder.folder_id == folder_id)
            .first()
        )
        if not folder:
            raise NotFoundError("Ordner nicht gefunden.")
        has_docs = db.query(ProjectDocument).filter(ProjectDocument.folder_id == folder_id).first()
        has_children = db.query(ProjectFolder).filter(ProjectFolder.parent_folder_id == folder_id).first()
        if has_docs or has_children:
            raise ConflictError("Ordner kann nur gelöscht werden, wenn er leer ist.")
        db.delete(folder)
        db.commit()


def list_documents(account_id: str, project_id: str, folder_id: str = "") -> list[dict]:
    folder_id = str(folder_id or "").strip()
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        q = db.query(ProjectDocument).filter(ProjectDocument.project_id == project_id)
        if folder_id:
            q = q.filter(ProjectDocument.folder_id == folder_id)
        else:
            q = q.filter(ProjectDocument.folder_id.is_(None))
        return [_document_to_dict(d) for d in q.order_by(ProjectDocument.created_at.desc()).all()]


def list_project_ifc_documents(account_id: str, project_id: str) -> list[dict]:
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        docs = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.file_extension.in_([".ifc", ".ifczip"]),
            )
            .order_by(ProjectDocument.created_at.asc())
            .all()
        )
        return [_document_to_dict(d) for d in docs]


def count_project_documents(account_id: str, project_id: str) -> int:
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        return int(db.query(ProjectDocument).filter(ProjectDocument.project_id == project_id).count())


def count_project_ifc_documents(account_id: str, project_id: str) -> int:
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        return int(
            db.query(ProjectDocument)
            .filter(ProjectDocument.project_id == project_id, ProjectDocument.file_extension.in_([".ifc", ".ifczip"]))
            .count()
        )


def get_document(account_id: str, project_id: str, document_id: str) -> dict:
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        doc = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.project_id == project_id, ProjectDocument.document_id == document_id)
            .first()
        )
        if not doc:
            raise NotFoundError("Dokument nicht gefunden.")
        return _document_to_dict(doc)


def save_project_document(
    account_id: str,
    project_id: str,
    file_bytes: bytes,
    original_filename: str,
    content_type: str = "",
    folder_id: str = "",
) -> dict:
    _require_r2()
    if not original_filename:
        raise ValidationError("Dateiname fehlt.")
    file_size = len(file_bytes or b"")
    if file_size <= 0:
        raise ValidationError("Die Datei ist leer.")
    if file_size > MAX_DOCUMENT_SIZE_BYTES:
        raise ValidationError(f"Datei überschreitet {MAX_DOCUMENT_SIZE_MB} MB.")

    safe_filename = sanitize_filename(original_filename)
    ext = _extension(safe_filename)
    if ext not in ALLOWED_DOCUMENT_EXTENSIONS:
        raise ValidationError(f"Dateityp '{ext or 'unbekannt'}' ist nicht erlaubt.")

    folder_id = str(folder_id or "").strip() or None
    document_id = uuid.uuid4().hex
    now = _utcnow()

    with SessionLocal() as db:
        project = _get_project_for_account(db, account_id, project_id)
        folder = None
        if folder_id:
            folder = (
                db.query(ProjectFolder)
                .filter(ProjectFolder.project_id == project.project_id, ProjectFolder.folder_id == folder_id)
                .first()
            )
            if not folder:
                raise NotFoundError("Zielordner nicht gefunden.")
        folder_path = folder.path if folder else ""
        r2_key = build_document_r2_key(project.project_id, folder_path, document_id, safe_filename)
        content_type = content_type or mimetypes.guess_type(safe_filename)[0] or "application/octet-stream"

        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name
            upload_file_to_r2(tmp_path, r2_key, content_type=content_type)
        except Exception as exc:
            raise StorageError(f"Dokument konnte nicht nach R2 hochgeladen werden: {exc}") from exc
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

        doc = ProjectDocument(
            document_id=document_id,
            project_id=project.project_id,
            folder_id=folder_id,
            original_filename=original_filename,
            safe_filename=safe_filename,
            file_extension=ext,
            content_type=content_type,
            file_size=file_size,
            r2_key=r2_key,
            document_kind=document_kind_for_extension(ext),
            created_at=now,
            updated_at=now,
        )
        db.add(doc)
        project.updated_at = now
        try:
            db.commit()
        except Exception:
            db.rollback()
            try:
                delete_file_from_r2(r2_key)
            except Exception:
                pass
            raise
        db.refresh(doc)
        return _document_to_dict(doc)


def download_document_to_temp(account_id: str, project_id: str, document_id: str) -> tuple[dict, str]:
    if not _r2_available() or download_file_from_r2 is None:
        raise StorageError("Cloudflare R2 ist nicht konfiguriert; Dokument kann nicht geladen werden.")
    doc = get_document(account_id, project_id, document_id)
    suffix = doc.get("file_extension") or ""
    fd, tmp_path = tempfile.mkstemp(prefix="bimpruef-doc-", suffix=suffix)
    os.close(fd)
    try:
        download_file_from_r2(doc["r2_key"], tmp_path)
        return doc, tmp_path
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise StorageError(f"Dokument konnte nicht aus R2 geladen werden: {exc}") from exc


def delete_document(account_id: str, project_id: str, document_id: str) -> None:
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    if not _r2_available() or delete_file_from_r2 is None:
        raise StorageError("Cloudflare R2 ist nicht konfiguriert; Dokument kann nicht sicher gelöscht werden.")
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        doc = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.project_id == project_id, ProjectDocument.document_id == document_id)
            .first()
        )
        if not doc:
            raise NotFoundError("Dokument nicht gefunden.")
        try:
            delete_file_from_r2(doc.r2_key)
        except Exception as exc:
            db.rollback()
            raise StorageError(f"R2-Datei konnte nicht gelöscht werden. SQL wurde nicht verändert: {exc}") from exc
        db.delete(doc)
        db.commit()


def delete_project_documents_prefix(project_id: str, strict: bool = True) -> None:
    project_id = _validate_safe_id(project_id, "Projekt-ID")

    if not _r2_available() or delete_prefix_from_r2 is None:
        if strict:
            raise StorageError(
                "Cloudflare R2 ist nicht konfiguriert; "
                "Projektdokumente können nicht sicher gelöscht werden."
            )
        return

    try:
        delete_prefix_from_r2(_project_document_prefix(project_id))
    except Exception as exc:
        if strict:
            raise StorageError(
                f"Dokument-Prefix konnte nicht vollständig gelöscht werden: {exc}"
            ) from exc
