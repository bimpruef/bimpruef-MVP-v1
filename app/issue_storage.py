"""
issue_storage.py – Projektbasiertes Issues-Modul für BIMPruef

تغییرات اصلی:
- هر clash ذخیره‌شده یک issue_number منحصربه‌فرد و تجمعی دریافت می‌کند (از 1 شروع)
- issue_counter در جدول Project ذخیره می‌شود و هرگز ریست نمی‌شود
- حذف deduplication: یک clash می‌تواند چندین بار با شماره‌های مختلف ذخیره شود
- issue_number در لیست ایشوها نمایش داده می‌شود
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

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
    Migration idempotent برای دیتابیس‌های موجود.
    ستون‌های جدید issue_number و document_id را اضافه می‌کند.
    ستون issue_counter را به جدول projects اضافه می‌کند.
    """
    try:
        init_db()
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())

        with engine.begin() as conn:
            # ─── project_issues ───────────────────────────────────────────
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
                    "slot_1":         "INTEGER NOT NULL DEFAULT 0",
                    "slot_2":         "INTEGER NOT NULL DEFAULT 0",
                    "payload_json":   "TEXT NOT NULL DEFAULT '{}'",
                    "updated_at":     "TIMESTAMP WITH TIME ZONE",
                    # ستون جدید شماره sequential
                    "issue_number":   "INTEGER NOT NULL DEFAULT 0",
                }
                for name, ddl in issue_cols.items():
                    if name not in existing:
                        conn.execute(text(
                            f"ALTER TABLE project_issues ADD COLUMN {name} {ddl}"
                        ))

            # ─── projects ────────────────────────────────────────────────
            if "projects" in tables:
                proj_existing = {c["name"] for c in inspector.get_columns("projects")}
                if "issue_counter" not in proj_existing:
                    conn.execute(text(
                        "ALTER TABLE projects ADD COLUMN issue_counter INTEGER NOT NULL DEFAULT 0"
                    ))

    except Exception:
        pass


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
    هر clash انتخاب‌شده را به عنوان یک Issue جدید ذخیره می‌کند.

    قوانین:
    - هر clash → یک Issue جدید با issue_number منحصربه‌فرد
    - issue_number از project.issue_counter گرفته می‌شود و با هر ذخیره +1 می‌شود
    - شمارنده هرگز ریست نمی‌شود حتی با حذف issue
    - یک clash می‌تواند چندین بار با شماره‌های مختلف ذخیره شود
    - GlobalId هر دو طرف باید موجود باشد
    """
    if not clashes:
        raise ValidationError("Bitte mindestens eine Clash-Zeile auswählen.")

    # اعتبارسنجی اولیه
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
        # with_for_update برای جلوگیری از race condition در شمارنده
        project = _project_for_account(db, account_id, project_id)

        for clash in valid_clashes:
            gid1 = _clean(clash.get("global_id_1", ""), 80)
            gid2 = _clean(clash.get("global_id_2", ""), 80)

            if not gid1 or not gid2:
                continue

            # شماره بعدی را از شمارنده پروژه بگیر
            project.issue_counter = (project.issue_counter or 0) + 1
            next_number = project.issue_counter

            type1 = _clean(clash.get("type_1", ""), 120)
            type2 = _clean(clash.get("type_2", ""), 120)
            name1 = _clean(clash.get("name_1", ""), 255)
            name2 = _clean(clash.get("name_2", ""), 255)
            doc1  = _clean(clash.get("document_id_1", ""), 64)
            doc2  = _clean(clash.get("document_id_2", ""), 64)

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
                slot_1=int(clash.get("slot_1", 0) or 0),
                slot_2=int(clash.get("slot_2", 0) or 0),
                payload_json=json.dumps(clash, ensure_ascii=False, default=str),
                created_at=now,
                updated_at=now,
            )

            db.add(issue)

            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                # شمارنده را برگردان چون این issue ذخیره نشد
                project = _project_for_account(db, account_id, project_id)
                # شمارنده دست نخورده بماند – شماره مصرف شده است
                continue

            created.append(_issue_to_dict(issue))

        project.updated_at = now
        db.commit()

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