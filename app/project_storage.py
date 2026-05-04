"""
project_storage.py – BIMPruef Projektverwaltung

Speichert Projekte als JSON im Dateisystem unter:
  uploads/accounts/<account_id>/projects.json

Jedes Projekt bekommt eine eigene Upload-Session, die an die
bestehende session-basierte Logik (storage.py) angebunden ist.
"""

import json
import os
import re
import time
import uuid
from typing import Optional

from app.storage import (
    UPLOADS_DIR,
    create_upload_session,
    session_exists,
    get_session_dir,
)

ACCOUNTS_DIR = os.path.join(UPLOADS_DIR, "accounts")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


# ─────────────────────────────────────────────────────────────────────────────
# Demo-Account (Phase 1: lokaler Einzel-Account)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_ACCOUNT = {
    "account_id":   "default",
    "account_name": "foadamini",
    "workspace":    "Default",
    "created_at":   "2026-01-01T00:00:00",
}


def get_account(account_id: str = "default") -> dict:
    return DEFAULT_ACCOUNT.copy()


# ─────────────────────────────────────────────────────────────────────────────
# Interner Dateipfad-Helfer
# ─────────────────────────────────────────────────────────────────────────────

def _validate_safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(value):
        raise ValueError(f"Ungültige {label}.")
    return value


def _safe_join(base_dir: str, *parts: str) -> str:
    base_abs = os.path.abspath(base_dir)
    path_abs = os.path.abspath(os.path.join(base_abs, *parts))
    if path_abs != base_abs and not path_abs.startswith(base_abs + os.sep):
        raise ValueError("Unsicherer Projektpfad erkannt.")
    return path_abs


def _account_dir(account_id: str) -> str:
    account_id = _validate_safe_id(account_id, "Account-ID")
    return _safe_join(ACCOUNTS_DIR, account_id)


def _projects_file(account_id: str) -> str:
    return os.path.join(_account_dir(account_id), "projects.json")


def _project_dir(account_id: str, project_id: str) -> str:
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    return _safe_join(_account_dir(account_id), "projects", project_id)


def _ensure_account_dir(account_id: str):
    os.makedirs(_account_dir(account_id), exist_ok=True)
    os.makedirs(_safe_join(_account_dir(account_id), "projects"), exist_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# Session-Mapping: project_id  ↔  session_id
# ─────────────────────────────────────────────────────────────────────────────
# Die bestehende Upload-/Viewer-Logik erwartet eine session_id.
# Wir speichern die Zuordnung pro Projekt in einer kleinen Datei.

def _session_map_file(account_id: str, project_id: str) -> str:
    return os.path.join(_project_dir(account_id, project_id), "session_id.txt")


def get_or_create_project_session(account_id: str, project_id: str) -> str:
    """
    Gibt die bestehende session_id eines Projekts zurück.
    Falls noch keine existiert (oder die Session-Daten fehlen), wird eine neue
    Session angelegt und gespeichert.
    """
    map_file = _session_map_file(account_id, project_id)

    if os.path.exists(map_file):
        with open(map_file, "r", encoding="utf-8") as f:
            sid = f.read().strip()
        if sid and session_exists(sid):
            return sid

    # Neue Session anlegen
    sid = create_upload_session()
    os.makedirs(os.path.dirname(map_file), exist_ok=True)
    with open(map_file, "w", encoding="utf-8") as f:
        f.write(sid)
    return sid


def get_project_session(account_id: str, project_id: str) -> Optional[str]:
    """Gibt die gespeicherte session_id zurück (ohne neu anzulegen)."""
    map_file = _session_map_file(account_id, project_id)
    if not os.path.exists(map_file):
        return None
    with open(map_file, "r", encoding="utf-8") as f:
        sid = f.read().strip()
    return sid if sid else None


# ─────────────────────────────────────────────────────────────────────────────
# Projektverwaltung
# ─────────────────────────────────────────────────────────────────────────────

def _load_projects(account_id: str) -> list:
    path = _projects_file(account_id)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []


def _save_projects(account_id: str, projects: list):
    _ensure_account_dir(account_id)
    path = _projects_file(account_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def list_projects(account_id: str = "default") -> list:
    """Gibt alle Projekte des Accounts zurück (neueste zuerst)."""
    projects = _load_projects(account_id)
    return sorted(projects, key=lambda p: p.get("created_at", ""), reverse=True)


def create_project(
    account_id:   str,
    project_code: str,
    project_name: str,
    description:  str = "",
) -> dict:
    """Legt ein neues Projekt an und gibt es zurück."""
    _ensure_account_dir(account_id)

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    project = {
        "project_id":   str(uuid.uuid4()),
        "account_id":   account_id,
        "project_code": project_code.strip(),
        "project_name": project_name.strip(),
        "description":  description.strip(),
        "status":       "active",
        "created_at":   now,
        "updated_at":   now,
    }

    projects = _load_projects(account_id)
    projects.append(project)
    _save_projects(account_id, projects)

    # Projekt-Verzeichnis anlegen
    os.makedirs(_project_dir(account_id, project["project_id"]), exist_ok=True)

    return project


def get_project(account_id: str, project_id: str) -> Optional[dict]:
    """Gibt ein einzelnes Projekt zurück oder None."""
    for p in _load_projects(account_id):
        if p["project_id"] == project_id:
            return p
    return None


def update_project(
    account_id:   str,
    project_id:   str,
    project_code: Optional[str] = None,
    project_name: Optional[str] = None,
    description:  Optional[str] = None,
    status:       Optional[str] = None,
) -> Optional[dict]:
    """Aktualisiert ein Projekt und gibt das aktualisierte Objekt zurück."""
    projects = _load_projects(account_id)
    for p in projects:
        if p["project_id"] == project_id:
            if project_code is not None:
                p["project_code"] = project_code.strip()
            if project_name is not None:
                p["project_name"] = project_name.strip()
            if description is not None:
                p["description"] = description.strip()
            if status is not None:
                p["status"] = status
            p["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            _save_projects(account_id, projects)
            return p
    return None


def delete_project(account_id: str, project_id: str) -> bool:
    """Löscht ein Projekt (und gibt True zurück, wenn es gefunden wurde)."""
    import shutil
    projects = _load_projects(account_id)
    new_list = [p for p in projects if p["project_id"] != project_id]
    if len(new_list) == len(projects):
        return False
    _save_projects(account_id, new_list)
    # Projekt-Verzeichnis löschen
    pdir = _project_dir(account_id, project_id)
    if os.path.isdir(pdir):
        shutil.rmtree(pdir, ignore_errors=True)
    return True


def get_project_model_count(account_id: str, project_id: str) -> int:
    """Gibt die Anzahl hochgeladener Modelle in einem Projekt zurück."""
    from app.storage import get_session_slots
    sid = get_project_session(account_id, project_id)
    if not sid or not session_exists(sid):
        return 0
    return len(get_session_slots(sid))
