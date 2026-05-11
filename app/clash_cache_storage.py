"""clash_cache_storage.py – latest project-scoped Clash result cache."""

import json
import re
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app.db import SessionLocal, engine, init_db
from app.exceptions import NotFoundError, ValidationError
from app.models import Project, ProjectClashCache

init_db()

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_safe_id(value: str, label: str) -> str:
    value = str(value or "").strip()
    if not SAFE_ID_RE.fullmatch(value):
        raise ValidationError(f"Ungültige {label}.")
    return value


def _json_dump(value) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, default=str)


def _json_load(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def ensure_clash_cache_schema() -> None:
    """Create/repair the tiny table that stores the latest clash result per project."""
    try:
        init_db()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "project_clash_cache" not in tables:
            ProjectClashCache.__table__.create(bind=engine, checkfirst=True)
            return
        existing = {c["name"] for c in inspector.get_columns("project_clash_cache")}
        cols = {
            "payload_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        }
        with engine.begin() as conn:
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE project_clash_cache ADD COLUMN {name} {ddl}"))
    except Exception:
        pass


ensure_clash_cache_schema()


def _require_project(db, account_id: str, project_id: str) -> Project:
    account_id = _validate_safe_id(account_id, "Account-ID")
    project_id = _validate_safe_id(project_id, "Projekt-ID")
    project = db.query(Project).filter(Project.account_id == account_id, Project.project_id == project_id).first()
    if not project:
        raise NotFoundError("Projekt nicht gefunden.")
    return project


def get_latest_project_clash_cache(account_id: str, project_id: str) -> dict:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        row = db.query(ProjectClashCache).filter(ProjectClashCache.project_id == project_id).first()
        return _json_load(row.payload_json) if row else {}


def save_latest_project_clash_cache(account_id: str, project_id: str, payload: dict) -> dict:
    now = _utcnow()
    with SessionLocal() as db:
        project = _require_project(db, account_id, project_id)
        row = db.query(ProjectClashCache).filter(ProjectClashCache.project_id == project_id).first()
        if row is None:
            row = ProjectClashCache(project_id=project_id, created_at=now, updated_at=now, payload_json="{}")
            db.add(row)
        row.payload_json = _json_dump(payload)
        row.updated_at = now
        project.updated_at = now
        db.commit()
        return _json_load(row.payload_json)


def clear_project_clash_cache(account_id: str, project_id: str) -> None:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        db.query(ProjectClashCache).filter(ProjectClashCache.project_id == project_id).delete(synchronize_session=False)
        db.commit()
