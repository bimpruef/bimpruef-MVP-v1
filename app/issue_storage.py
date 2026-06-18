"""
issue_storage.py – Projektbasiertes Issues-Modul für BIMPruef

Issues sind die zentrale fachliche Sammelstelle für Clash-Ergebnisse und
spätere Prüf-/Koordinationspunkte. BCF-Export gehört hierhin, nicht ins
Clash-Modul.

Aktive Architektur:
  - Clash-Issues werden pro ProjectDocument-Kombination gespeichert.
  - Keine viewer session slots, kein slot_1/slot_2 mehr.
"""

import json
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, engine, init_db
from app.exceptions import NotFoundError, ValidationError, ConflictError
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


def _dt(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    return str(value or "")


def _clean(value: str, max_len: int = 255) -> str:
    return str(value or "").strip()[:max_len]


def _project_for_account(db, account_id: str, project_id: str) -> Project:
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
    payload = {}
    if issue.payload_json:
        try:
            payload = json.loads(issue.payload_json)
        except Exception:
            payload = {}

    return {
        "issue_id": issue.issue_id,
        "project_id": issue.project_id,
        "source": issue.source,
        "issue_type": issue.issue_type,
        "title": issue.title,
        "description": issue.description or "",
        "status": issue.status or "open",
        "priority": issue.priority or "normal",
        "global_id_1": issue.global_id_1 or "",
        "global_id_2": issue.global_id_2 or "",
        "type_1": issue.type_1 or "",
        "type_2": issue.type_2 or "",
        "name_1": issue.name_1 or "",
        "name_2": issue.name_2 or "",
        "file_label_1": issue.file_label_1 or "",
        "file_label_2": issue.file_label_2 or "",
        "document_id_1": getattr(issue, "document_id_1", "") or "",
        "document_id_2": getattr(issue, "document_id_2", "") or "",
        "payload": payload,
        "created_at": _dt(issue.created_at),
        "updated_at": _dt(issue.updated_at),
    }


def ensure_issue_schema() -> None:
    """
    Idempotente Migration für bereits bestehende PostgreSQL-Datenbanken.

    Fügt die neuen document_id-Spalten hinzu, falls sie fehlen.
    Alte slot_1/slot_2-Spalten werden hier bewusst nicht automatisch gelöscht,
    weil DROP COLUMN in Produktion separat und kontrolliert passieren sollte.
    """
    try:
        init_db()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        if "project_issues" not in tables:
            return

        existing = {c["name"] for c in inspector.get_columns("project_issues")}
        cols = {
            "source": "VARCHAR(60) NOT NULL DEFAULT 'manual'",
            "issue_type": "VARCHAR(60) NOT NULL DEFAULT 'coordination'",
            "description": "TEXT NOT NULL DEFAULT ''",
            "status": "VARCHAR(40) NOT NULL DEFAULT 'open'",
            "priority": "VARCHAR(40) NOT NULL DEFAULT 'normal'",
            "global_id_1": "VARCHAR(80) NOT NULL DEFAULT ''",
            "global_id_2": "VARCHAR(80) NOT NULL DEFAULT ''",
            "type_1": "VARCHAR(120) NOT NULL DEFAULT ''",
            "type_2": "VARCHAR(120) NOT NULL DEFAULT ''",
            "name_1": "VARCHAR(255) NOT NULL DEFAULT ''",
            "name_2": "VARCHAR(255) NOT NULL DEFAULT ''",
            "file_label_1": "VARCHAR(255) NOT NULL DEFAULT ''",
            "file_label_2": "VARCHAR(255) NOT NULL DEFAULT ''",
            "document_id_1": "VARCHAR(64) NOT NULL DEFAULT ''",
            "document_id_2": "VARCHAR(64) NOT NULL DEFAULT ''",
            "payload_json": "TEXT NOT NULL DEFAULT '{}'",
            "updated_at": "TIMESTAMP WITH TIME ZONE",
        }

        with engine.begin() as conn:
            for name, ddl in cols.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE project_issues ADD COLUMN {name} {ddl}"))
    except Exception:
        pass


ensure_issue_schema()


def list_project_issues(account_id: str, project_id: str) -> list[dict]:
    with SessionLocal() as db:
        _project_for_account(db, account_id, project_id)
        issues = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id)
            .order_by(ProjectIssue.created_at.desc())
            .all()
        )
        return [_issue_to_dict(i) for i in issues]


def count_project_issues(account_id: str, project_id: str) -> int:
    with SessionLocal() as db:
        _project_for_account(db, account_id, project_id)
        return int(db.query(ProjectIssue).filter(ProjectIssue.project_id == project_id).count())


def get_issue(account_id: str, project_id: str, issue_id: str) -> dict:
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _project_for_account(db, account_id, project_id)
        issue = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id, ProjectIssue.issue_id == issue_id)
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        return _issue_to_dict(issue)


def save_clash_issues(account_id: str, project_id: str, clashes: list[dict]) -> list[dict]:
    """
    Speichert ausgewählte Clash-Zeilen als Issues.

    Deduplizierung (zweistufig):
      1. seen_in_batch: filtert Duplikate innerhalb derselben Anfrage heraus,
         bevor sie die Datenbank erreichen.
      2. DB-Abfrage: prüft ob das Paar bereits in einem früheren Request
         gespeichert wurde.

    Bei einem unerwarteten IntegrityError (z. B. Race Condition zwischen zwei
    gleichzeitigen Requests) wird nur das betroffene Issue übersprungen;
    der Rest des Batches wird weiter verarbeitet.
    """
    if not clashes:
        raise ValidationError("Bitte mindestens eine Clash-Zeile auswählen.")

    now = _utcnow()
    created: list[dict] = []
    # Deduplizierung innerhalb desselben Batch-Requests
    seen_in_batch: set[tuple] = set()

    with SessionLocal() as db:
        project = _project_for_account(db, account_id, project_id)

        for clash in clashes:
            gid1 = _clean(clash.get("global_id_1", ""), 80)
            gid2 = _clean(clash.get("global_id_2", ""), 80)
            doc1 = _clean(clash.get("document_id_1", ""), 64)
            doc2 = _clean(clash.get("document_id_2", ""), 64)

            if not gid1 or not gid2:
                continue

            # Stufe 1: In-Memory-Deduplizierung für diesen Request
            dedupe_key = (project_id, doc1, gid1, doc2, gid2)
            if dedupe_key in seen_in_batch:
                continue
            seen_in_batch.add(dedupe_key)

            # Stufe 2: Datenbankabfrage auf bereits vorhandene Issues
            duplicate = (
                db.query(ProjectIssue)
                .filter(
                    ProjectIssue.project_id == project_id,
                    ProjectIssue.issue_type == "clash",
                    ProjectIssue.global_id_1 == gid1,
                    ProjectIssue.global_id_2 == gid2,
                    ProjectIssue.document_id_1 == doc1,
                    ProjectIssue.document_id_2 == doc2,
                )
                .first()
            )
            if duplicate:
                created.append(_issue_to_dict(duplicate))
                continue

            type1 = _clean(clash.get("type_1", ""), 120)
            type2 = _clean(clash.get("type_2", ""), 120)
            name1 = _clean(clash.get("name_1", ""), 255)
            name2 = _clean(clash.get("name_2", ""), 255)

            title = f"Clash: {type1 or 'Element A'} ↔ {type2 or 'Element B'}"
            description = (
                f"Aus Clash-Analyse gespeichert.\n\n"
                f"Element A: {type1} | {name1} | {gid1}\n"
                f"Element B: {type2} | {name2} | {gid2}"
            )

            issue = ProjectIssue(
                issue_id=uuid.uuid4().hex,
                project_id=project_id,
                source="clash",
                issue_type="clash",
                title=title[:255],
                description=description,
                status="open",
                priority="normal",
                global_id_1=gid1,
                global_id_2=gid2,
                type_1=type1,
                type_2=type2,
                name_1=name1,
                name_2=name2,
                file_label_1=_clean(clash.get("file_label_1", ""), 255),
                file_label_2=_clean(clash.get("file_label_2", ""), 255),
                document_id_1=doc1,
                document_id_2=doc2,
                payload_json=json.dumps(clash, ensure_ascii=False, default=str),
                created_at=now,
                updated_at=now,
            )

            db.add(issue)
            try:
                db.flush()
            except IntegrityError:
                # Race Condition oder unbekannter DB-Constraint:
                # dieses Issue überspringen, Session wiederherstellen,
                # restlichen Batch weiter verarbeiten.
                db.rollback()
                project = _project_for_account(db, account_id, project_id)
                continue

            created.append(_issue_to_dict(issue))

        project.updated_at = now
        db.commit()

    return created


def delete_issue(account_id: str, project_id: str, issue_id: str) -> None:
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _project_for_account(db, account_id, project_id)
        issue = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id, ProjectIssue.issue_id == issue_id)
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        db.delete(issue)
        db.commit()


def issue_to_bcf_clash(issue: dict) -> dict:
    """Konvertiert ein Issue-Dict in das Clash-Dict-Format des BCF-Exports."""
    payload = issue.get("payload") or {}
    if isinstance(payload, dict) and payload.get("global_id_1") and payload.get("global_id_2"):
        cleaned_payload = dict(payload)
        cleaned_payload.pop("slot_1", None)
        cleaned_payload.pop("slot_2", None)
        cleaned_payload.setdefault("document_id_1", issue.get("document_id_1", ""))
        cleaned_payload.setdefault("document_id_2", issue.get("document_id_2", ""))
        return cleaned_payload

    return {
        "global_id_1": issue.get("global_id_1", ""),
        "global_id_2": issue.get("global_id_2", ""),
        "type_1": issue.get("type_1", ""),
        "type_2": issue.get("type_2", ""),
        "name_1": issue.get("name_1", ""),
        "name_2": issue.get("name_2", ""),
        "file_label_1": issue.get("file_label_1", ""),
        "file_label_2": issue.get("file_label_2", ""),
        "document_id_1": issue.get("document_id_1", ""),
        "document_id_2": issue.get("document_id_2", ""),
    }
