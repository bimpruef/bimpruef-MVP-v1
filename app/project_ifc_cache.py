"""
project_ifc_cache.py – projektbezogener IFC-Cache und IFC-Index für BIMPruef

Documents/R2 bleibt die dauerhafte Quelle. Dieses Modul erzeugt daraus bei
Bedarf temporäre, lokal nutzbare .ifc-Dateien und JSON-Indizes pro Projekt.

Cache-Struktur:
  uploads/project_cache/{project_id}/
    documents/{document_id}.ifc
    index/{document_id}.json
    index/project_index.json
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ifcopenshell

from app.db import SessionLocal
from app.exceptions import NotFoundError, StorageError, ValidationError
from app.extractors import extract_element_data, get_candidate_products
from app.models import Project, ProjectDocument
from app.storage import UPLOADS_DIR

try:
    from app.r2_storage import download_file_from_r2, r2_enabled
except Exception:  # local development without R2
    download_file_from_r2 = None
    r2_enabled = lambda: False

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
CACHE_ROOT = os.path.join(UPLOADS_DIR, "project_cache")
CACHE_MAX_AGE_HOURS = int(os.environ.get("PROJECT_IFC_CACHE_MAX_AGE_HOURS", "24"))
_INDEX_VERSION = 1
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utcnow().replace(microsecond=0).isoformat()


def _validate_safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(f"Ungültige {label}.")
    return value


def _safe_join(base_dir: str, *parts: str) -> str:
    base_abs = os.path.abspath(base_dir)
    path_abs = os.path.abspath(os.path.join(base_abs, *parts))
    if path_abs != base_abs and not path_abs.startswith(base_abs + os.sep):
        raise ValidationError("Unsicherer Cache-Pfad erkannt.")
    return path_abs


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


def _cache_dir(project_id: str) -> str:
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    return _safe_join(CACHE_ROOT, project_id)


def _documents_dir(project_id: str) -> str:
    return _safe_join(_cache_dir(project_id), "documents")


def _index_dir(project_id: str) -> str:
    return _safe_join(_cache_dir(project_id), "index")


def _document_ifc_path(project_id: str, document_id: str) -> str:
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    return _safe_join(_documents_dir(project_id), f"{document_id}.ifc")


def _document_index_path(project_id: str, document_id: str) -> str:
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    return _safe_join(_index_dir(project_id), f"{document_id}.json")


def _project_index_path(project_id: str) -> str:
    return _safe_join(_index_dir(project_id), "project_index.json")


def _ensure_dirs(project_id: str) -> None:
    os.makedirs(_documents_dir(project_id), exist_ok=True)
    os.makedirs(_index_dir(project_id), exist_ok=True)


def _read_json(path: str) -> dict[str, Any] | None:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_atomic(path: str, data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _dt(value) -> str:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat()
    return str(value or "")


def _document_marker(doc: dict[str, Any]) -> str:
    return "|".join([
        str(doc.get("r2_key", "")),
        str(doc.get("file_size", 0)),
        str(doc.get("updated_at", "")),
        str(doc.get("file_extension", "")),
    ])


def _doc_to_dict(doc: ProjectDocument) -> dict[str, Any]:
    return {
        "document_id": doc.document_id,
        "project_id": doc.project_id,
        "original_filename": doc.original_filename,
        "safe_filename": doc.safe_filename,
        "file_extension": (doc.file_extension or "").lower(),
        "content_type": doc.content_type,
        "file_size": int(doc.file_size or 0),
        "r2_key": doc.r2_key,
        "document_kind": doc.document_kind,
        "created_at": _dt(doc.created_at),
        "updated_at": _dt(doc.updated_at),
    }


def _get_project_for_account(db, account_id: str, project_id: str) -> Project:
    account_id = _validate_safe_id(account_id, "Account-ID")
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    project = db.query(Project).filter(Project.account_id == account_id, Project.project_id == project_id).first()
    if not project:
        raise NotFoundError("Projekt nicht gefunden.")
    return project


def _get_ifc_documents(account_id: str, project_id: str) -> list[dict[str, Any]]:
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
        docs = (
            db.query(ProjectDocument)
            .filter(ProjectDocument.project_id == project_id, ProjectDocument.file_extension.in_([".ifc", ".ifczip"]))
            .order_by(ProjectDocument.created_at.asc())
            .all()
        )
        return [_doc_to_dict(d) for d in docs]


def _get_ifc_document(account_id: str, project_id: str, document_id: str) -> dict[str, Any]:
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
        if (doc.file_extension or "").lower() not in {".ifc", ".ifczip"}:
            raise ValidationError("Das Dokument ist keine IFC/IFCZIP-Datei.")
        return _doc_to_dict(doc)


def _r2_available() -> bool:
    try:
        return bool(r2_enabled())
    except Exception:
        return False


def _download_original_to_temp(doc: dict[str, Any]) -> str:
    if not _r2_available() or download_file_from_r2 is None:
        raise StorageError("Cloudflare R2 ist nicht konfiguriert; IFC-Dokument kann nicht geladen werden.")
    suffix = doc.get("file_extension") or ".bin"
    fd, tmp_path = tempfile.mkstemp(prefix="bimpruef-project-ifc-", suffix=suffix)
    os.close(fd)
    try:
        download_file_from_r2(doc["r2_key"], tmp_path)
        return tmp_path
    except Exception as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise StorageError(f"IFC-Dokument konnte nicht aus R2 geladen werden: {exc}") from exc


def _extract_or_copy_to_ifc(original_path: str, doc: dict[str, Any], target_ifc_path: str) -> None:
    os.makedirs(os.path.dirname(target_ifc_path), exist_ok=True)
    ext = (doc.get("file_extension") or "").lower()
    if ext == ".ifc":
        shutil.copyfile(original_path, target_ifc_path)
        return
    if ext != ".ifczip":
        raise ValidationError("Nur .ifc und .ifczip können in den Project IFC Cache übernommen werden.")
    with zipfile.ZipFile(original_path, "r") as zf:
        ifc_names = [n for n in zf.namelist() if n.lower().endswith(".ifc") and not n.endswith("/")]
        if not ifc_names:
            raise ValidationError("IFCZIP enthält keine .ifc-Datei.")
        # Das größte IFC im ZIP ist in der Praxis fast immer das Hauptmodell.
        info = max((zf.getinfo(n) for n in ifc_names), key=lambda i: i.file_size)
        with zf.open(info, "r") as src, open(target_ifc_path, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _build_document_index(doc: dict[str, Any], local_ifc_path: str) -> dict[str, Any]:
    try:
        model = ifcopenshell.open(local_ifc_path)
    except Exception as exc:
        raise StorageError(f"IFC-Datei konnte nicht geöffnet werden: {exc}") from exc

    elements: list[dict[str, Any]] = []
    type_counts: dict[str, int] = {}
    for elem in get_candidate_products(model):
        try:
            data = extract_element_data(elem, file_label=doc.get("original_filename", ""))
            basic = {
                "document_id": doc["document_id"],
                "file_label": doc.get("original_filename", ""),
                "express_id": data.get("express_id", ""),
                "global_id": data.get("global_id", ""),
                "type": data.get("type", ""),
                "name": data.get("name", ""),
                "object_type": data.get("object_type", ""),
                "predefined_type": data.get("predefined_type", ""),
            }
            elements.append(basic)
            if basic["type"]:
                type_counts[basic["type"]] = type_counts.get(basic["type"], 0) + 1
        except Exception:
            continue

    return {
        "index_version": _INDEX_VERSION,
        "document_id": doc["document_id"],
        "project_id": doc["project_id"],
        "original_filename": doc.get("original_filename", ""),
        "file_extension": doc.get("file_extension", ""),
        "r2_key": doc.get("r2_key", ""),
        "file_size": int(doc.get("file_size") or 0),
        "updated_at": doc.get("updated_at", ""),
        "document_marker": _document_marker(doc),
        "local_ifc_path": local_ifc_path,
        "indexed_at": _iso_now(),
        "element_count": len(elements),
        "ifc_types": sorted(type_counts.keys()),
        "ifc_type_counts": type_counts,
        "elements": elements,
        "optional_fields": {
            "psets": False,
            "quantities": False,
            "bounding_boxes": False,
            "geometry_metadata": False,
        },
    }


def _document_cache_valid(doc: dict[str, Any], ifc_path: str, index_path: str) -> bool:
    if not os.path.isfile(ifc_path) or os.path.getsize(ifc_path) <= 0:
        return False
    idx = _read_json(index_path)
    if not idx:
        return False
    return (
        idx.get("index_version") == _INDEX_VERSION
        and idx.get("document_marker") == _document_marker(doc)
        and idx.get("local_ifc_path") == ifc_path
    )


def ensure_document_ifc_cache(account_id: str, project_id: str, document_id: str) -> dict[str, Any]:
    """Ensure one ProjectDocument exists as local .ifc and has a valid JSON index."""
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    lock = _lock_for(f"{project_id}:{document_id}")
    with lock:
        _ensure_dirs(project_id)
        doc = _get_ifc_document(account_id, project_id, document_id)
        ifc_path = _document_ifc_path(project_id, document_id)
        index_path = _document_index_path(project_id, document_id)

        if _document_cache_valid(doc, ifc_path, index_path):
            idx = _read_json(index_path) or {}
            idx["last_accessed_at"] = _iso_now()
            _write_json_atomic(index_path, idx)
            return idx

        original_path = ""
        try:
            original_path = _download_original_to_temp(doc)
            _extract_or_copy_to_ifc(original_path, doc, ifc_path)
            idx = _build_document_index(doc, ifc_path)
            idx["last_accessed_at"] = _iso_now()
            _write_json_atomic(index_path, idx)
            return idx
        finally:
            if original_path:
                try:
                    os.remove(original_path)
                except OSError:
                    pass


def ensure_project_ifc_cache(account_id: str, project_id: str) -> dict[str, Any]:
    """Ensure all IFC/IFCZIP documents of a project have cache + index."""
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    lock = _lock_for(f"{project_id}:project")
    with lock:
        _ensure_dirs(project_id)
        docs = _get_ifc_documents(account_id, project_id)
        active_ids = {d["document_id"] for d in docs}

        document_indexes = []
        for doc in docs:
            document_indexes.append(ensure_document_ifc_cache(account_id, project_id, doc["document_id"]))

        # Remove stale cache/index files for documents no longer present.
        for path in Path(_documents_dir(project_id)).glob("*.ifc"):
            if path.stem not in active_ids:
                try:
                    path.unlink()
                except OSError:
                    pass
        for path in Path(_index_dir(project_id)).glob("*.json"):
            if path.name == "project_index.json":
                continue
            if path.stem not in active_ids:
                try:
                    path.unlink()
                except OSError:
                    pass

        available_types = sorted({t for idx in document_indexes for t in idx.get("ifc_types", [])})
        project_index = {
            "index_version": _INDEX_VERSION,
            "project_id": project_id,
            "indexed_at": _iso_now(),
            "last_accessed_at": _iso_now(),
            "document_ids": [idx["document_id"] for idx in document_indexes],
            "ifc_document_count": len(document_indexes),
            "total_element_count": sum(int(idx.get("element_count") or 0) for idx in document_indexes),
            "available_ifc_types": available_types,
            "documents": document_indexes,
        }
        _write_json_atomic(_project_index_path(project_id), project_index)
        return project_index


def get_cached_ifc_path(account_id: str, project_id: str, document_id: str) -> str:
    idx = ensure_document_ifc_cache(account_id, project_id, document_id)
    path = idx.get("local_ifc_path") or _document_ifc_path(project_id, document_id)
    if not os.path.isfile(path):
        raise NotFoundError("Lokale IFC-Cache-Datei nicht gefunden.")
    return path


def get_document_ifc_index(account_id: str, project_id: str, document_id: str) -> dict[str, Any]:
    return ensure_document_ifc_cache(account_id, project_id, document_id)


def get_project_ifc_index(account_id: str, project_id: str) -> dict[str, Any]:
    return ensure_project_ifc_cache(account_id, project_id)


def invalidate_document_ifc_cache(account_id: str, project_id: str, document_id: str) -> None:
    _validate_safe_id(account_id, "Account-ID")
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    document_id = _validate_safe_id(document_id, "Dokument-ID")
    # Verify ownership before deleting local cache.
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
    for path in (_document_ifc_path(project_id, document_id), _document_index_path(project_id, document_id)):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    try:
        pidx = _project_index_path(project_id)
        if os.path.exists(pidx):
            os.remove(pidx)
    except OSError:
        pass


def invalidate_project_ifc_cache(account_id: str, project_id: str) -> None:
    _validate_safe_id(account_id, "Account-ID")
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    with SessionLocal() as db:
        _get_project_for_account(db, account_id, project_id)
    cache = _cache_dir(project_id)
    try:
        if os.path.isdir(cache):
            shutil.rmtree(cache)
    except OSError:
        pass


def cleanup_old_project_ifc_caches(max_age_hours: int | None = None) -> None:
    max_age = int(max_age_hours or CACHE_MAX_AGE_HOURS) * 3600
    root = Path(CACHE_ROOT)
    if not root.exists():
        return
    now = time.time()
    for project_dir in root.iterdir():
        if not project_dir.is_dir():
            continue
        idx = _read_json(str(project_dir / "index" / "project_index.json")) or {}
        last = idx.get("last_accessed_at") or idx.get("indexed_at") or ""
        try:
            if last:
                last_ts = datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
            else:
                last_ts = project_dir.stat().st_mtime
        except Exception:
            last_ts = project_dir.stat().st_mtime
        if now - last_ts > max_age:
            try:
                shutil.rmtree(project_dir)
            except OSError:
                pass


def document_index_by_slot(project_index: dict[str, Any], slot: int) -> dict[str, Any]:
    try:
        slot = int(slot)
    except Exception as exc:
        raise ValidationError("Ungültiger Dokument-Slot.") from exc
    docs = project_index.get("documents", []) or []
    if slot < 1 or slot > len(docs):
        raise ValidationError("Ausgewähltes Dokument existiert nicht im Project IFC Index.")
    return docs[slot - 1]
