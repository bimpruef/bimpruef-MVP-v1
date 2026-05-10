"""
issue_storage.py – Project Issues storage for BIMPruef

Issues are persistent, project-scoped coordination records. Clash results can
be converted into issues; BCF export belongs to this module, not to the clash
calculation UI.
"""

import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, text

from app.db import SessionLocal, engine, init_db
from app.exceptions import NotFoundError, ValidationError
from app.models import Project, ProjectIssue

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


def _json_load(value: str, fallback):
    try:
        return json.loads(value or "")
    except Exception:
        return fallback


def _dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value or "")


def ensure_issue_schema() -> None:
    """Idempotent schema migration for existing PostgreSQL databases."""
    try:
        init_db()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "project_issues" not in tables:
            ProjectIssue.__table__.create(bind=engine, checkfirst=True)
            return
        existing = {c["name"] for c in inspector.get_columns("project_issues")}
        cols = {
            "source_type": "VARCHAR(60) NOT NULL DEFAULT 'manual'",
            "title": "VARCHAR(255) NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "status": "VARCHAR(60) NOT NULL DEFAULT 'open'",
            "priority": "VARCHAR(60) NOT NULL DEFAULT 'normal'",
            "documents_json": "TEXT NOT NULL DEFAULT '[]'",
            "elements_json": "TEXT NOT NULL DEFAULT '{}'",
            "viewpoint_json": "TEXT NOT NULL DEFAULT '{}'",
            "created_at": "TIMESTAMP WITH TIME ZONE",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        }
        with engine.begin() as conn:
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE project_issues ADD COLUMN {name} {ddl}"))
    except Exception:
        pass


ensure_issue_schema()


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


def _issue_to_dict(issue: ProjectIssue) -> dict:
    return {
        "issue_id": issue.issue_id,
        "project_id": issue.project_id,
        "source_type": issue.source_type,
        "title": issue.title,
        "description": issue.description or "",
        "status": issue.status or "open",
        "priority": issue.priority or "normal",
        "documents": _json_load(issue.documents_json, []),
        "elements": _json_load(issue.elements_json, {}),
        "viewpoint": _json_load(issue.viewpoint_json, {}),
        "created_at": _dt(issue.created_at),
        "updated_at": _dt(issue.updated_at),
    }


def list_project_issues(account_id: str, project_id: str) -> list[dict]:
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        rows = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id)
            .order_by(ProjectIssue.created_at.desc())
            .all()
        )
        return [_issue_to_dict(row) for row in rows]


def get_project_issue(account_id: str, project_id: str, issue_id: str) -> dict:
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        issue = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id, ProjectIssue.issue_id == issue_id)
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        return _issue_to_dict(issue)


def _clash_to_issue_payload(clash: dict, index: int) -> dict:
    gid1 = clash.get("global_id_1", "")
    gid2 = clash.get("global_id_2", "")
    type1 = clash.get("type_1", "")
    type2 = clash.get("type_2", "")
    doc1 = clash.get("document_id_1", "")
    doc2 = clash.get("document_id_2", "")
    title = f"Clash {index:04d}: {type1 or 'Element A'} ↔ {type2 or 'Element B'}"
    description = (
        f"Automatisch aus der Clash-Analyse erzeugt.\n"
        f"Element A: {type1} | {clash.get('name_1','')} | {gid1}\n"
        f"Element B: {type2} | {clash.get('name_2','')} | {gid2}"
    )
    documents = []
    if doc1:
        documents.append({"document_id": doc1, "name": clash.get("document_name_1") or clash.get("file_label_1", "")})
    if doc2 and doc2 != doc1:
        documents.append({"document_id": doc2, "name": clash.get("document_name_2") or clash.get("file_label_2", "")})
    elements = {
        "element_1": {
            "global_id": gid1,
            "ifc_type": type1,
            "name": clash.get("name_1", ""),
            "document_id": doc1,
            "document_name": clash.get("document_name_1") or clash.get("file_label_1", ""),
            "express_id": clash.get("express_id_1", ""),
        },
        "element_2": {
            "global_id": gid2,
            "ifc_type": type2,
            "name": clash.get("name_2", ""),
            "document_id": doc2,
            "document_name": clash.get("document_name_2") or clash.get("file_label_2", ""),
            "express_id": clash.get("express_id_2", ""),
        },
    }
    viewpoint = {
        "selection": [gid for gid in (gid1, gid2) if gid],
        "camera": clash.get("viewpoint") or {},
    }
    return {
        "source_type": "clash",
        "title": title,
        "description": description,
        "status": "open",
        "priority": "normal",
        "documents": documents,
        "elements": elements,
        "viewpoint": viewpoint,
    }


def create_issue(account_id: str, project_id: str, payload: dict) -> dict:
    now = _utcnow()
    with SessionLocal() as db:
        project = _require_project(db, account_id, project_id)
        issue = ProjectIssue(
            issue_id=uuid.uuid4().hex,
            project_id=project.project_id,
            source_type=str(payload.get("source_type") or "manual")[:60],
            title=str(payload.get("title") or "Issue")[:255],
            description=str(payload.get("description") or ""),
            status=str(payload.get("status") or "open")[:60],
            priority=str(payload.get("priority") or "normal")[:60],
            documents_json=_json_dump(payload.get("documents") or []),
            elements_json=_json_dump(payload.get("elements") or {}),
            viewpoint_json=_json_dump(payload.get("viewpoint") or {}),
            created_at=now,
            updated_at=now,
        )
        db.add(issue)
        project.updated_at = now
        db.commit()
        db.refresh(issue)
        return _issue_to_dict(issue)


def create_issues_from_clashes(account_id: str, project_id: str, clashes: list[dict]) -> list[dict]:
    created: list[dict] = []
    for idx, clash in enumerate(clashes or [], start=1):
        created.append(create_issue(account_id, project_id, _clash_to_issue_payload(clash, idx)))
    return created


def update_issue_status(account_id: str, project_id: str, issue_id: str, status: str, priority: str = "") -> dict:
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        issue = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id, ProjectIssue.issue_id == issue_id)
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        issue.status = str(status or issue.status or "open")[:60]
        if priority:
            issue.priority = str(priority)[:60]
        issue.updated_at = _utcnow()
        db.commit()
        db.refresh(issue)
        return _issue_to_dict(issue)


def delete_issue(account_id: str, project_id: str, issue_id: str) -> None:
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _require_project(db, account_id, project_id)
        issue = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id, ProjectIssue.issue_id == issue_id)
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        db.delete(issue)
        db.commit()
