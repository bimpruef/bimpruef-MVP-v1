"""
issue_storage.py – Projektbasiertes Issues-Modul für BIMPruef

تغییرات اصلی:
- هر clash ذخیره‌شده یک issue_number منحصربه‌فرد و تجمعی دریافت می‌کند (از 1 شروع)
- issue_counter در جدول Project ذخیره می‌شود و هرگز ریست نمی‌شود
- حذف deduplication: یک clash می‌تواند چندین بار با شماره‌های مختلف ذخیره شود
- issue_number در لیست ایشوها نمایش داده می‌شود
- Legacy slot_1/slot_2 schema is repaired for Direct-Document inserts
- Datenbankfehler werden nicht mehr als "Duplikate" verschluckt
"""

import json
import re
import uuid
from datetime import datetime, timezone
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal, engine, init_db
from app.exceptions import ConflictError, NotFoundError, ValidationError
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
        .with_for_update()  # قفل ردیف برای شمارنده atomic
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
        "issue_number": int(issue.issue_number or 0),
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
    Idempotente Migration für bestehende Datenbanken.

    Neben den aktuellen Direct-Document-Feldern repariert diese Migration auch
    alte Datenbanken, in denen ``slot_1``/``slot_2`` noch als NOT-NULL-Spalten
    ohne serverseitigen Default existieren. Solche Legacy-Spalten bleiben aus
    Kompatibilitätsgründen bestehen, bekommen aber DEFAULT 0, damit neue Issues
    ohne Slot-Felder gespeichert werden können.
    """
    init_db()
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    dialect = engine.dialect.name

    try:
        with engine.begin() as conn:
            if "project_issues" in tables:
                existing = {c["name"] for c in inspector.get_columns("project_issues")}
                issue_cols = {
                    "source":         "VARCHAR(60) NOT NULL DEFAULT 'manual'",
                    "issue_type":     "VARCHAR(60) NOT NULL DEFAULT 'coordination'",
                    "description":    "TEXT NOT NULL DEFAULT ''",
                    "status":         "VARCHAR(40) NOT NULL DEFAULT 'open'",
                    "priority":       "VARCHAR(40) NOT NULL DEFAULT 'normal'",
                    "global_id_1":    "VARCHAR(80) NOT NULL DEFAULT ''",
                    "global_id_2":    "VARCHAR(80) NOT NULL DEFAULT ''",
                    "type_1":         "VARCHAR(120) NOT NULL DEFAULT ''",
                    "type_2":         "VARCHAR(120) NOT NULL DEFAULT ''",
                    "name_1":         "VARCHAR(255) NOT NULL DEFAULT ''",
                    "name_2":         "VARCHAR(255) NOT NULL DEFAULT ''",
                    "file_label_1":   "VARCHAR(255) NOT NULL DEFAULT ''",
                    "file_label_2":   "VARCHAR(255) NOT NULL DEFAULT ''",
                    "document_id_1":  "VARCHAR(64) NOT NULL DEFAULT ''",
                    "document_id_2":  "VARCHAR(64) NOT NULL DEFAULT ''",
                    "payload_json":   "TEXT NOT NULL DEFAULT '{}'",
                    "updated_at":     "TIMESTAMP WITH TIME ZONE",
                    "issue_number":   "INTEGER NOT NULL DEFAULT 0",
                }
                for name, ddl in issue_cols.items():
                    if name not in existing:
                        conn.execute(text(
                            f"ALTER TABLE project_issues ADD COLUMN {name} {ddl}"
                        ))

                # Legacy-Schema reparieren: frühere ORM-Versionen hatten
                # slot_1/slot_2 als NOT NULL, aber ohne Server-Default.
                # Die Direct-Document-Architektur schreibt diese Felder nicht mehr.
                if dialect == "postgresql":
                    for legacy_col in ("slot_1", "slot_2"):
                        if legacy_col in existing:
                            conn.execute(text(
                                f"UPDATE project_issues SET {legacy_col} = 0 "
                                f"WHERE {legacy_col} IS NULL"
                            ))
                            conn.execute(text(
                                f"ALTER TABLE project_issues "
                                f"ALTER COLUMN {legacy_col} SET DEFAULT 0"
                            ))

                    # Alte Datensätze, die beim Hinzufügen von issue_number den
                    # Default 0 erhalten haben, einmalig sinnvoll nummerieren.
                    conn.execute(text("""
                        WITH current_max AS (
                            SELECT
                                project_id,
                                COALESCE(MAX(NULLIF(issue_number, 0)), 0) AS max_no
                            FROM project_issues
                            GROUP BY project_id
                        ),
                        numbered AS (
                            SELECT
                                i.issue_id,
                                COALESCE(m.max_no, 0)
                                + ROW_NUMBER() OVER (
                                    PARTITION BY i.project_id
                                    ORDER BY i.created_at NULLS LAST, i.issue_id
                                ) AS new_no
                            FROM project_issues AS i
                            LEFT JOIN current_max AS m
                              ON m.project_id = i.project_id
                            WHERE COALESCE(i.issue_number, 0) = 0
                        )
                        UPDATE project_issues AS i
                        SET issue_number = n.new_no
                        FROM numbered AS n
                        WHERE i.issue_id = n.issue_id
                    """))

            if "projects" in tables:
                proj_existing = {c["name"] for c in inspector.get_columns("projects")}
                if "issue_counter" not in proj_existing:
                    conn.execute(text(
                        "ALTER TABLE projects "
                        "ADD COLUMN issue_counter INTEGER NOT NULL DEFAULT 0"
                    ))

            # Counter nach Migration/Altbestand mindestens auf die höchste
            # bereits vergebene Issue-Nummer setzen.
            if dialect == "postgresql" and {"projects", "project_issues"}.issubset(tables):
                conn.execute(text("""
                    UPDATE projects AS p
                    SET issue_counter = GREATEST(
                        COALESCE(p.issue_counter, 0),
                        COALESCE((
                            SELECT MAX(i.issue_number)
                            FROM project_issues AS i
                            WHERE i.project_id = p.project_id
                        ), 0)
                    )
                """))
    except Exception as exc:
        raise RuntimeError(
            f"Issue-Datenbankschema konnte nicht aktualisiert werden: {exc}"
        ) from exc


ensure_issue_schema()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def list_project_issues(account_id: str, project_id: str) -> list[dict]:
    with SessionLocal() as db:
        _validate_safe_id(account_id, "Account-ID")
        _validate_safe_id(project_id, "Projekt-ID")
        project = (
            db.query(Project)
            .filter(Project.account_id == account_id, Project.project_id == project_id)
            .first()
        )
        if not project:
            raise NotFoundError("Projekt nicht gefunden.")
        issues = (
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id)
            .order_by(ProjectIssue.issue_number.asc())
            .all()
        )
        return [_issue_to_dict(i) for i in issues]


def count_project_issues(account_id: str, project_id: str) -> int:
    with SessionLocal() as db:
        _validate_safe_id(account_id, "Account-ID")
        _validate_safe_id(project_id, "Projekt-ID")
        return int(
            db.query(ProjectIssue)
            .filter(ProjectIssue.project_id == project_id)
            .count()
        )


def get_issue(account_id: str, project_id: str, issue_id: str) -> dict:
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _validate_safe_id(account_id, "Account-ID")
        _validate_safe_id(project_id, "Projekt-ID")
        project = (
            db.query(Project)
            .filter(Project.account_id == account_id, Project.project_id == project_id)
            .first()
        )
        if not project:
            raise NotFoundError("Projekt nicht gefunden.")
        issue = (
            db.query(ProjectIssue)
            .filter(
                ProjectIssue.project_id == project_id,
                ProjectIssue.issue_id == issue_id,
            )
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        return _issue_to_dict(issue)


def save_clash_issues(
    account_id: str,
    project_id: str,
    clashes: list[dict],
) -> list[dict]:
    """
    Speichert jeden ausgewählten Clash als neues Issue.

    Es gibt bewusst keine Deduplication: derselbe Clash darf mehrfach als
    separates Issue gespeichert werden. Der komplette Request wird atomar
    gespeichert; bei einem Datenbankfehler wird nichts teilweise übernommen.
    """
    if not clashes:
        raise ValidationError("Bitte mindestens eine Clash-Zeile auswählen.")

    valid_clashes = [
        c for c in clashes
        if str(c.get("global_id_1") or "").strip()
        and str(c.get("global_id_2") or "").strip()
    ]
    if not valid_clashes:
        raise ValidationError(
            "Keine gültigen GlobalIds in den ausgewählten Clashes. "
            "Bitte Clash-Analyse neu starten."
        )

    now = _utcnow()
    created: list[dict] = []

    with SessionLocal() as db:
        try:
            # Row-Lock schützt die fortlaufende Projektnummerierung.
            project = _project_for_account(db, account_id, project_id)

            for clash in valid_clashes:
                gid1 = _clean(clash.get("global_id_1", ""), 80)
                gid2 = _clean(clash.get("global_id_2", ""), 80)
                if not gid1 or not gid2:
                    continue

                project.issue_counter = int(project.issue_counter or 0) + 1
                next_number = project.issue_counter

                type1 = _clean(clash.get("type_1", ""), 120)
                type2 = _clean(clash.get("type_2", ""), 120)
                name1 = _clean(clash.get("name_1", ""), 255)
                name2 = _clean(clash.get("name_2", ""), 255)
                doc1 = _clean(clash.get("document_id_1", ""), 64)
                doc2 = _clean(clash.get("document_id_2", ""), 64)

                title = (
                    f"#{next_number} – Clash: "
                    f"{type1 or 'Element A'} ↔ {type2 or 'Element B'}"
                )
                description = (
                    f"Clash-Issue #{next_number}\n\n"
                    f"Element A: {type1} | {name1} | {gid1}\n"
                    f"Element B: {type2} | {name2} | {gid2}"
                )

                issue = ProjectIssue(
                    issue_id=uuid.uuid4().hex,
                    issue_number=next_number,
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
                created.append(_issue_to_dict(issue))

            if len(created) != len(valid_clashes):
                raise ValidationError(
                    "Mindestens ein ausgewählter Clash enthält unvollständige Daten."
                )

            # WICHTIG: Flush nicht mehr pro Issue schlucken. Ein Constraint-Fehler
            # muss sichtbar werden, sonst meldet das Frontend fälschlich "Duplikat".
            db.flush()
            project.updated_at = now
            db.commit()

        except IntegrityError as exc:
            db.rollback()
            detail = str(getattr(exc, "orig", exc))
            raise ConflictError(
                "Issues konnten nicht gespeichert werden. "
                f"Datenbank-Constraint: {detail}"
            ) from exc
        except Exception:
            db.rollback()
            raise

    return created


def delete_issue(account_id: str, project_id: str, issue_id: str) -> None:
    """
    یک issue را حذف می‌کند.
    توجه: issue_counter پروژه تغییر نمی‌کند – شماره‌گذاری ادامه می‌یابد.
    """
    issue_id = _validate_safe_id(issue_id, "Issue-ID")
    with SessionLocal() as db:
        _validate_safe_id(account_id, "Account-ID")
        _validate_safe_id(project_id, "Projekt-ID")
        project = (
            db.query(Project)
            .filter(Project.account_id == account_id, Project.project_id == project_id)
            .first()
        )
        if not project:
            raise NotFoundError("Projekt nicht gefunden.")
        issue = (
            db.query(ProjectIssue)
            .filter(
                ProjectIssue.project_id == project_id,
                ProjectIssue.issue_id == issue_id,
            )
            .first()
        )
        if not issue:
            raise NotFoundError("Issue nicht gefunden.")
        db.delete(issue)
        db.commit()


def issue_to_bcf_clash(issue: dict) -> dict:
    """Issue dict را به فرمت BCF export تبدیل می‌کند."""
    payload = issue.get("payload") or {}
    if isinstance(payload, dict) and payload.get("global_id_1") and payload.get("global_id_2"):
        cleaned = dict(payload)
        cleaned.pop("slot_1", None)
        cleaned.pop("slot_2", None)
        cleaned.setdefault("document_id_1", issue.get("document_id_1", ""))
        cleaned.setdefault("document_id_2", issue.get("document_id_2", ""))
        return cleaned

    return {
        "global_id_1":   issue.get("global_id_1", ""),
        "global_id_2":   issue.get("global_id_2", ""),
        "type_1":        issue.get("type_1", ""),
        "type_2":        issue.get("type_2", ""),
        "name_1":        issue.get("name_1", ""),
        "name_2":        issue.get("name_2", ""),
        "file_label_1":  issue.get("file_label_1", ""),
        "file_label_2":  issue.get("file_label_2", ""),
        "document_id_1": issue.get("document_id_1", ""),
        "document_id_2": issue.get("document_id_2", ""),
    }
