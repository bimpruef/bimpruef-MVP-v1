"""
project_rulecheck.py – BIMPruef Rule-Check Projektmodul

معماری جدید (بدون slot cache):
- کاربر مستقیم از Documents فایل‌های IFC را انتخاب می‌کند
- هنگام کلیک ▶، فایل‌ها مستقیم از R2 لود می‌شوند
- هیچ session_id یا slot cache ای وجود ندارد
- مثل project_clash.py و list_module.py

sessionStorage cache:
- نتایج، فیلترها، انتخاب فایل‌ها و انتخاب رول‌ها در sessionStorage ذخیره می‌شن
- هنگام بازگشت به صفحه بدون درخواست جدید restore می‌شن
- مثل project_clash.py

Routen:
  GET  /projects/{project_id}/checking          → Rule-Check UI
  POST /projects/{project_id}/checking/run      → Regelprüfung ausführen (JSON-API)
  GET  /projects/{project_id}/checking/export   → Ergebnisse als JSON exportieren

Legacy-Redirects:
  GET  /viewer/rulecheck/        → 302
  POST /viewer/rulecheck/run/    → 410
  GET  /viewer/rulecheck/export/ → 410
"""

import html
import io
import json
import os
import tempfile
import zipfile
from urllib.parse import quote_plus

import ifcopenshell
import ifcopenshell.util.element
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_user
from app.document_storage import (
    get_document,
    list_project_ifc_documents,
)
from app.project_storage import get_project

try:
    from app.r2_storage import download_file_from_r2, r2_enabled
except Exception:
    download_file_from_r2 = None
    r2_enabled = lambda: False

project_rulecheck_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Regel-Definitionen
# ─────────────────────────────────────────────────────────────────────────────

ALL_RULES = [
    {
        "id":          "missing_names",
        "label":       "Fehlende Namen",
        "description": "Prüft alle relevanten IfcProduct-Elemente auf fehlende Name-Attribute.",
        "severity":    "warning",
    },
    {
        "id":          "missing_global_id",
        "label":       "Fehlende GlobalId",
        "description": "Prüft alle IfcProduct-Elemente direkt auf fehlende GlobalId.",
        "severity":    "error",
    },
    {
        "id":          "missing_spaces",
        "label":       "Fehlende Räume (IfcSpace)",
        "description": "Warnt, wenn im Modell keine IfcSpace-Elemente vorhanden sind.",
        "severity":    "warning",
    },
    {
        "id":          "door_without_name",
        "label":       "Türen ohne Name",
        "description": "Prüft alle IfcDoor-Elemente auf fehlende Namen.",
        "severity":    "warning",
    },
    {
        "id":          "window_without_name",
        "label":       "Fenster ohne Name",
        "description": "Prüft alle IfcWindow-Elemente auf fehlende Namen.",
        "severity":    "warning",
    },
    {
        "id":          "wall_without_fire_rating",
        "label":       "Wände ohne Brandschutzklasse",
        "description": "Prüft IfcWall/IfcWallStandardCase auf fehlende FireRating-Eigenschaft "
                       "(sucht nach: FireRating, Feuerwiderstand, Brandschutz).",
        "severity":    "error",
    },
    {
        "id":          "external_wall_check",
        "label":       "Außenwand-Kennzeichnung",
        "description": "Prüft Wände auf IsExternal-Eigenschaft. Fehlende Kennzeichnung wird als Hinweis ausgegeben.",
        "severity":    "info",
    },
]

_RULE_META = {r["id"]: r for r in ALL_RULES}


# ─────────────────────────────────────────────────────────────────────────────
# IFC direkt aus R2 laden (identisch zu project_clash.py)
# ─────────────────────────────────────────────────────────────────────────────

def _open_ifc_from_document(account_id: str, project_id: str, document_id: str):
    """
    IFC/IFCZIP را مستقیم از R2 لود می‌کند — بدون slot cache.
    Returns: (model, label)
    """
    if not (r2_enabled() and download_file_from_r2):
        raise ValueError("Cloudflare R2 ist nicht konfiguriert.")

    doc = get_document(account_id, project_id, document_id)
    label = doc.get("original_filename", document_id)
    ext = (doc.get("file_extension") or ".ifc").lower()

    fd, tmp_path = tempfile.mkstemp(prefix="bp_check_", suffix=ext)
    os.close(fd)

    try:
        download_file_from_r2(doc["r2_key"], tmp_path)

        if ext == ".ifczip":
            with open(tmp_path, "rb") as f:
                raw = f.read()
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                ifc_names = [n for n in zf.namelist() if n.lower().endswith(".ifc")]
                if not ifc_names:
                    raise ValueError(f"Keine .ifc-Datei in '{label}' gefunden.")
                ifc_bytes = zf.read(ifc_names[0])

            fd2, ifc_tmp = tempfile.mkstemp(prefix="bp_check_ifc_", suffix=".ifc")
            os.close(fd2)
            with open(ifc_tmp, "wb") as f:
                f.write(ifc_bytes)
            try:
                model = ifcopenshell.open(ifc_tmp)
            finally:
                try:
                    os.remove(ifc_tmp)
                except OSError:
                    pass
        else:
            model = ifcopenshell.open(tmp_path)

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return model, label


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s or ""))


def _flatten_psets(psets: dict) -> dict:
    flat = {}
    for pset_name, props in (psets or {}).items():
        if isinstance(props, dict):
            for prop_name, value in props.items():
                flat_key = f"{pset_name}.{prop_name}".lower()
                flat[flat_key] = str(value) if value is not None else ""
    return flat


def _psets_contain_key(psets: dict, search_terms: list) -> bool:
    flat = _flatten_psets(psets)
    for key in flat:
        prop_name = key.split(".", 1)[-1] if "." in key else key
        for term in search_terms:
            if term.lower() in prop_name:
                return True
    return False


def _make_result(
    rule_id: str,
    severity: str,
    file_label: str,
    document_id: str,
    ifc_type: str,
    name: str,
    global_id: str,
    express_id,
    message: str,
) -> dict:
    return {
        "rule_id":     rule_id,
        "severity":    severity,
        "file_label":  file_label,
        "document_id": document_id,
        "ifc_type":    ifc_type,
        "name":        name or "",
        "global_id":   global_id or "",
        "express_id":  str(express_id or ""),
        "message":     message,
    }


def _fmt_size(num: int) -> str:
    try:
        n = float(num or 0)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


# ─────────────────────────────────────────────────────────────────────────────
# Regelprüfungen (jetzt mit document_id statt slot)
# ─────────────────────────────────────────────────────────────────────────────

def check_missing_names(model, file_label: str, document_id: str) -> list:
    from app.extractors import get_candidate_products, extract_element_data
    results = []
    for elem in get_candidate_products(model):
        data = extract_element_data(elem, file_label=file_label)
        if not data.get("name", "").strip():
            results.append(_make_result(
                rule_id="missing_names", severity="warning",
                file_label=file_label, document_id=document_id,
                ifc_type=data.get("type", ""), name="",
                global_id=data.get("global_id", ""),
                express_id=data.get("express_id", ""),
                message=f"Element ohne Name gefunden (Typ: {data.get('type', '?')}).",
            ))
    return results


def check_missing_global_id(model, file_label: str, document_id: str) -> list:
    results = []
    try:
        for obj in model.by_type("IfcProduct"):
            try:
                global_id = getattr(obj, "GlobalId", None)
                if not global_id:
                    ifc_type   = obj.is_a() if hasattr(obj, "is_a") else "IfcProduct"
                    name       = getattr(obj, "Name", None) or ""
                    express_id = obj.id() if hasattr(obj, "id") else ""
                    results.append(_make_result(
                        rule_id="missing_global_id", severity="error",
                        file_label=file_label, document_id=document_id,
                        ifc_type=ifc_type, name=name, global_id="",
                        express_id=express_id,
                        message=f"Element ohne GlobalId (Express-ID: {express_id}, Typ: {ifc_type}).",
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return results


def check_missing_spaces(model, file_label: str, document_id: str) -> list:
    results = []
    try:
        if not model.by_type("IfcSpace"):
            results.append(_make_result(
                rule_id="missing_spaces", severity="warning",
                file_label=file_label, document_id=document_id,
                ifc_type="IfcSpace", name="", global_id="", express_id="",
                message="Kein IfcSpace im Modell gefunden. Raumstruktur fehlt möglicherweise.",
            ))
    except Exception:
        pass
    return results


def check_door_without_name(model, file_label: str, document_id: str) -> list:
    results = []
    try:
        for door in model.by_type("IfcDoor"):
            try:
                name = getattr(door, "Name", None) or ""
                if not name.strip():
                    global_id  = getattr(door, "GlobalId", None) or ""
                    express_id = door.id() if hasattr(door, "id") else ""
                    results.append(_make_result(
                        rule_id="door_without_name", severity="warning",
                        file_label=file_label, document_id=document_id,
                        ifc_type="IfcDoor", name="", global_id=global_id,
                        express_id=express_id,
                        message=f"Tür ohne Name (GlobalId: {global_id or 'unbekannt'}).",
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return results


def check_window_without_name(model, file_label: str, document_id: str) -> list:
    results = []
    try:
        for window in model.by_type("IfcWindow"):
            try:
                name = getattr(window, "Name", None) or ""
                if not name.strip():
                    global_id  = getattr(window, "GlobalId", None) or ""
                    express_id = window.id() if hasattr(window, "id") else ""
                    results.append(_make_result(
                        rule_id="window_without_name", severity="warning",
                        file_label=file_label, document_id=document_id,
                        ifc_type="IfcWindow", name="", global_id=global_id,
                        express_id=express_id,
                        message=f"Fenster ohne Name (GlobalId: {global_id or 'unbekannt'}).",
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return results


def check_wall_without_fire_rating(model, file_label: str, document_id: str) -> list:
    results = []
    fire_keys = ["firerating", "feuerwiderstand", "brandschutz"]
    wall_types = []
    for wt in ("IfcWall", "IfcWallStandardCase"):
        try:
            wall_types.extend(model.by_type(wt))
        except Exception:
            pass
    for wall in wall_types:
        try:
            psets      = ifcopenshell.util.element.get_psets(wall) or {}
            name       = getattr(wall, "Name", None) or ""
            global_id  = getattr(wall, "GlobalId", None) or ""
            express_id = wall.id() if hasattr(wall, "id") else ""
            ifc_type   = wall.is_a() if hasattr(wall, "is_a") else "IfcWall"
            if not _psets_contain_key(psets, fire_keys):
                results.append(_make_result(
                    rule_id="wall_without_fire_rating", severity="error",
                    file_label=file_label, document_id=document_id,
                    ifc_type=ifc_type, name=name, global_id=global_id,
                    express_id=express_id,
                    message=f"Wand '{name or global_id}' hat keine FireRating-Eigenschaft.",
                ))
        except Exception:
            continue
    return results


def check_external_wall(model, file_label: str, document_id: str) -> list:
    results = []
    ext_keys = ["isexternal"]
    wall_types = []
    for wt in ("IfcWall", "IfcWallStandardCase"):
        try:
            wall_types.extend(model.by_type(wt))
        except Exception:
            pass
    for wall in wall_types:
        try:
            psets      = ifcopenshell.util.element.get_psets(wall) or {}
            name       = getattr(wall, "Name", None) or ""
            global_id  = getattr(wall, "GlobalId", None) or ""
            express_id = wall.id() if hasattr(wall, "id") else ""
            ifc_type   = wall.is_a() if hasattr(wall, "is_a") else "IfcWall"
            if not _psets_contain_key(psets, ext_keys):
                results.append(_make_result(
                    rule_id="external_wall_check", severity="info",
                    file_label=file_label, document_id=document_id,
                    ifc_type=ifc_type, name=name, global_id=global_id,
                    express_id=express_id,
                    message=f"Wand '{name or global_id}' hat keine IsExternal-Eigenschaft.",
                ))
        except Exception:
            continue
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Regel-Dispatcher
# ─────────────────────────────────────────────────────────────────────────────

_RULE_FUNCTIONS = {
    "missing_names":            check_missing_names,
    "missing_global_id":        check_missing_global_id,
    "missing_spaces":           check_missing_spaces,
    "door_without_name":        check_door_without_name,
    "window_without_name":      check_window_without_name,
    "wall_without_fire_rating": check_wall_without_fire_rating,
    "external_wall_check":      check_external_wall,
}


def run_rules_on_model(model, file_label: str, document_id: str, rules: list) -> list:
    results = []
    for rule_id in rules:
        fn = _RULE_FUNCTIONS.get(rule_id)
        if fn is None:
            continue
        try:
            results.extend(fn(model, file_label, document_id))
        except Exception as exc:
            results.append(_make_result(
                rule_id=rule_id, severity="error",
                file_label=file_label, document_id=document_id,
                ifc_type="", name="", global_id="", express_id="",
                message=f"Interner Fehler beim Ausführen der Regel '{rule_id}': {exc}",
            ))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Auth / Context
# ─────────────────────────────────────────────────────────────────────────────

def _account_from_request(request: Request) -> dict:
    user = require_user(request)
    return {
        "account_id": user["user_id"],
        "account_name": user["email"],
        "workspace": "Personal",
    }


def _load_context(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return None, None
    return account, project


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-UI
# ─────────────────────────────────────────────────────────────────────────────

def _checking_page(project: dict, account: dict) -> HTMLResponse:
    from app.projects import _page, _project_subnav, _topbar_global

    project_id = project["project_id"]
    pid = _e(project_id)
    account_id = account["account_id"]

    docs = list_project_ifc_documents(account_id, project_id)

    # ── Keine Dokumente ───────────────────────────────────────────────────────
    if not docs:
        body = f"""
        {_topbar_global(account)}
        {_project_subnav(project_id, "checking")}
        <div style="padding:28px 32px;max-width:900px;margin:0 auto">
          <h1 style="font-size:22px;font-weight:600;margin-bottom:14px">Rule-Check (Checking)</h1>
          <div class="card" style="border-color:var(--accent2)">
            <h3 style="font-size:15px;margin-bottom:8px">Keine IFC-Dateien im Documents-Modul</h3>
            <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
              Der Rule-Check lädt IFC-Dateien direkt aus dem Documents-Modul.
              Lade zuerst mindestens eine .ifc- oder .ifczip-Datei dort hoch.
            </p>
            <a class="btn btn-primary" href="/projects/{pid}/documents"
               style="text-decoration:none">Zu Documents</a>
          </div>
        </div>"""
        return _page(f"{project['project_name']} – Checking", body)

    # ── Datei-Checkboxen ──────────────────────────────────────────────────────
    file_checkboxes = ""
    for i, d in enumerate(docs):
        size_label = _fmt_size(d.get("file_size", 0))
        folder = _e(d.get("folder_path") or "Root")
        doc_id = _e(d["document_id"])
        fname  = _e(d["original_filename"])
        checked = "checked" if i == 0 else ""
        ext_badge = _e(d["file_extension"].upper().lstrip("."))
        file_checkboxes += f"""
        <label style="display:flex;align-items:center;gap:9px;padding:9px 12px;
          border-radius:8px;cursor:pointer;font-size:12px;
          background:var(--surface2);border:1px solid var(--border);
          transition:border-color .15s"
          onmouseenter="this.style.borderColor='var(--accent)'"
          onmouseleave="this.style.borderColor='var(--border)'">
          <input type="checkbox" class="doc-chk" value="{doc_id}" {checked}
            style="accent-color:var(--accent);width:13px;height:13px;flex-shrink:0;margin-top:2px;cursor:pointer">
          <div style="width:28px;height:28px;border-radius:6px;background:rgba(79,195,247,0.1);
            border:1px solid rgba(79,195,247,0.25);display:flex;align-items:center;
            justify-content:center;flex-shrink:0">
            <span style="font-size:8px;font-weight:700;color:var(--accent)">{ext_badge}</span>
          </div>
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;color:var(--text);overflow:hidden;
              text-overflow:ellipsis;white-space:nowrap" title="{fname}">{fname}</div>
            <div style="font-size:10px;color:var(--muted);margin-top:1px">
              {size_label} &nbsp;·&nbsp; {folder}
            </div>
          </div>
        </label>"""

    # ── Regel-Checkboxen ──────────────────────────────────────────────────────
    SEV_STYLE = {
        "error":   ("rgba(233,69,96,.15)",  "#e94560", "Fehler"),
        "warning": ("rgba(255,183,77,.15)", "#ffb74d", "Warnung"),
        "info":    ("rgba(79,195,247,.15)", "#4fc3f7", "Hinweis"),
    }
    rules_html = ""
    for rule in ALL_RULES:
        sev = rule["severity"]
        bg_c, txt_c, sev_label = SEV_STYLE.get(sev, ("rgba(255,255,255,.1)", "#fff", sev))
        rules_html += f"""
        <label style="display:flex;align-items:flex-start;gap:10px;
          padding:10px 12px;border-radius:8px;cursor:pointer;
          background:var(--surface2);border:1px solid var(--border);
          transition:border-color .15s"
          onmouseenter="this.style.borderColor='var(--accent)'"
          onmouseleave="this.style.borderColor='var(--border)'">
          <input type="checkbox" class="rule-chk" value="{_e(rule['id'])}" checked
            style="accent-color:var(--accent);width:13px;height:13px;flex-shrink:0;margin-top:2px;cursor:pointer">
          <div style="flex:1">
            <div style="display:flex;align-items:center;gap:7px;margin-bottom:2px">
              <span style="font-weight:600;font-size:12px">{_e(rule['label'])}</span>
              <span style="display:inline-block;padding:1px 8px;border-radius:100px;
                font-size:10px;font-weight:600;background:{bg_c};color:{txt_c}">{sev_label}</span>
            </div>
            <div style="font-size:11px;color:var(--muted);line-height:1.4">
              {_e(rule['description'])}
            </div>
          </div>
        </label>"""

    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "checking")}

<div style="display:flex;flex-direction:column;height:calc(100vh - 94px);overflow:hidden">
  <div style="display:flex;flex:1;overflow:hidden">

    <!-- ── Linkes Panel: Konfiguration ──────────────────────────────────── -->
    <div style="width:380px;min-width:340px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <div style="padding:8px 14px;font-size:10px;font-weight:700;
        background:var(--surface2);color:var(--muted);
        text-transform:uppercase;letter-spacing:.8px;
        border-bottom:1px solid var(--border);flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>✓ Rule-Check</span>
        <button id="btn-run" class="btn btn-primary"
          style="font-size:11px;padding:3px 14px">▶ Prüfung starten</button>
      </div>

      <div style="flex:1;overflow-y:auto;padding:10px;display:flex;flex-direction:column;gap:10px">

        <!-- IFC-Dateien -->
        <div class="card" style="padding:14px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
            <div style="font-size:11px;font-weight:600;color:var(--muted);
              text-transform:uppercase;letter-spacing:.5px">📁 IFC-Dateien</div>
            <a href="/projects/{pid}/documents"
              style="font-size:10px;color:var(--accent);text-decoration:none">Documents →</a>
          </div>
          <div style="display:flex;flex-direction:column;gap:5px">
            {file_checkboxes}
          </div>
          <div style="display:flex;gap:6px;margin-top:8px">
            <button type="button" onclick="toggleAllDocs(true)" class="btn"
              style="flex:1;font-size:10px;padding:3px 0">Alle</button>
            <button type="button" onclick="toggleAllDocs(false)" class="btn"
              style="flex:1;font-size:10px;padding:3px 0">Keine</button>
          </div>
        </div>

        <!-- Regeln -->
        <div class="card" style="padding:14px">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
            <div style="font-size:11px;font-weight:600;color:var(--muted);
              text-transform:uppercase;letter-spacing:.5px">⚙ Regeln</div>
            <div style="display:flex;gap:5px">
              <button type="button" onclick="toggleAllRules(true)" class="btn"
                style="font-size:10px;padding:2px 9px">Alle</button>
              <button type="button" onclick="toggleAllRules(false)" class="btn"
                style="font-size:10px;padding:2px 9px">Keine</button>
            </div>
          </div>
          <div style="display:flex;flex-direction:column;gap:6px">
            {rules_html}
          </div>
        </div>

        <!-- Cache leeren -->
        <div id="cache-clear-wrap" style="display:none">
          <button id="btn-clear-cache" class="btn"
            style="width:100%;font-size:11px;padding:6px;
            color:var(--muted);border-color:var(--border)">
            🗑 Cache leeren &amp; neu starten
          </button>
        </div>

      </div>

      <!-- Status-Bar unten -->
      <div id="run-status"
        style="padding:8px 14px;font-size:11px;color:var(--muted);
        border-top:1px solid var(--border);flex-shrink:0;background:var(--surface2);
        min-height:32px;display:flex;align-items:center;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
        Dateien und Regeln auswählen, dann ▶ klicken.
      </div>
    </div>

    <!-- ── Rechtes Panel: Ergebnisse ──────────────────────────────────── -->
    <div style="flex:1;display:flex;flex-direction:column;overflow:hidden">

      <!-- Toolbar -->
      <div id="toolbar" style="padding:8px 14px;background:var(--surface2);font-size:11px;
        color:var(--muted);border-bottom:1px solid var(--border);flex-shrink:0;
        display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <div id="summary-chips" style="display:flex;gap:6px;flex-wrap:wrap"></div>
        <div style="margin-left:auto;display:flex;gap:6px">
          <button id="btn-sev-all"     onclick="filterSev('all')"     class="btn" style="font-size:10px;padding:2px 9px">Alle</button>
          <button id="btn-sev-error"   onclick="filterSev('error')"   class="btn" style="font-size:10px;padding:2px 9px">Fehler</button>
          <button id="btn-sev-warning" onclick="filterSev('warning')" class="btn" style="font-size:10px;padding:2px 9px">Warnungen</button>
          <button id="btn-sev-info"    onclick="filterSev('info')"    class="btn" style="font-size:10px;padding:2px 9px">Hinweise</button>
          <a id="btn-export" href="#" style="display:none" class="btn"
            style="font-size:10px;padding:2px 9px;color:var(--accent)">⬇ Export</a>
        </div>
      </div>

      <!-- Tabelle -->
      <div id="table-wrap" style="flex:1;overflow:auto">

        <!-- Placeholder -->
        <div id="placeholder" style="display:flex;flex-direction:column;align-items:center;
          justify-content:center;height:100%;gap:14px;padding:40px;text-align:center">
          <svg width="44" height="44" fill="none" stroke="var(--muted)" stroke-width="1.5" viewBox="0 0 24 24">
            <path d="M9 11l3 3L22 4"/>
            <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
          </svg>
          <div>
            <div style="font-size:14px;color:var(--text);margin-bottom:6px">Rule-Check bereit</div>
            <div style="font-size:12px;color:var(--muted)">
              IFC-Datei(en) und Regeln auswählen,<br>
              dann <strong style="color:var(--accent)">▶ Prüfung starten</strong> klicken.
            </div>
          </div>
        </div>

        <!-- Ladeanimation -->
        <div id="loading" style="display:none;flex-direction:column;align-items:center;
          justify-content:center;height:100%;gap:16px">
          <div style="width:36px;height:36px;border:3px solid var(--border);
            border-top-color:var(--accent);border-radius:50%;
            animation:bp-spin .7s linear infinite"></div>
          <div id="loading-txt" style="font-size:13px;color:var(--muted)">
            Dateien werden aus R2 geladen …
          </div>
        </div>

        <table id="result-table" style="display:none;border-collapse:collapse;width:100%">
          <thead id="result-thead"></thead>
          <tbody id="result-tbody"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<style>
@keyframes bp-spin {{ to {{ transform: rotate(360deg); }} }}
.sev-error   {{ color:#e94560;font-weight:700 }}
.sev-warning {{ color:#ffb74d;font-weight:700 }}
.sev-info    {{ color:#4fc3f7;font-weight:700 }}
#result-table th {{
  background:var(--surface2);color:#8ab;font-size:11px;font-weight:600;
  padding:8px 12px;text-align:left;border:1px solid var(--border);
  position:sticky;top:0;z-index:1
}}
#result-table td {{
  padding:9px 12px;border:1px solid var(--border);font-size:11px;vertical-align:top
}}
#result-table tr:hover td {{ background:rgba(79,195,247,.04) }}
</style>

<script>
(function() {{

const PROJECT_ID  = {json.dumps(project_id)};
const RUN_URL     = `/projects/${{encodeURIComponent(PROJECT_ID)}}/checking/run`;
const EXPORT_BASE = `/projects/${{encodeURIComponent(PROJECT_ID)}}/checking/export`;

// ── sessionStorage key (identisch zu project_clash.py Muster) ────────────
const STATE_KEY = "bp_check_v1_" + PROJECT_ID;

let _allResults    = [];
let _currentFilter = "all";

function esc(s) {{
  return String(s ?? "")
    .replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

// ── sessionStorage Helpers ────────────────────────────────────────────────

function saveToCache(data) {{
  try {{
    sessionStorage.setItem(STATE_KEY, JSON.stringify(data));
    showCacheBadge(true);
  }} catch(e) {{
    // QuotaExceededError: ignorieren, kein Cache
    console.warn("sessionStorage save failed:", e);
  }}
}}

function loadFromCache() {{
  try {{
    const raw = sessionStorage.getItem(STATE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  }} catch(_) {{
    return null;
  }}
}}

function clearCache() {{
  try {{ sessionStorage.removeItem(STATE_KEY); }} catch(_) {{}}
  showCacheBadge(false);
}}

function showCacheBadge(visible) {{
  const clearWrap = document.getElementById("cache-clear-wrap");
  if (clearWrap) clearWrap.style.display = visible ? "block" : "none";
}}

// ── UI Helpers ────────────────────────────────────────────────────────────

window.toggleAllDocs = function(v) {{
  document.querySelectorAll(".doc-chk").forEach(c => {{ c.checked = !!v; }});
}};

window.toggleAllRules = function(v) {{
  document.querySelectorAll(".rule-chk").forEach(c => {{ c.checked = !!v; }});
}};

function sevLabel(s) {{
  return {{error:"Fehler", warning:"Warnung", info:"Hinweis"}}[s] || s;
}}
function sevClass(s) {{
  return {{error:"sev-error", warning:"sev-warning", info:"sev-info"}}[s] || "";
}}

// ── Tabelle rendern ───────────────────────────────────────────────────────

function renderTable(results) {{
  const thead = document.getElementById("result-thead");
  const tbody = document.getElementById("result-tbody");
  thead.innerHTML = `<tr>
    <th>#</th><th>Schwere</th><th>Regel</th>
    <th>Datei</th><th>IFC-Typ</th><th>Name</th>
    <th>GlobalId</th><th>Meldung</th>
  </tr>`;
  if (!results.length) {{
    tbody.innerHTML = `<tr><td colspan="8"
      style="text-align:center;color:var(--muted);padding:24px">
      Keine Ergebnisse für diesen Filter.
    </td></tr>`;
    return;
  }}
  tbody.innerHTML = results.map((r, i) => `<tr data-sev="${{esc(r.severity)}}">
    <td style="color:var(--muted);text-align:right">${{i+1}}</td>
    <td><span class="${{sevClass(r.severity)}}">${{esc(sevLabel(r.severity))}}</span></td>
    <td style="font-size:10px;color:var(--muted)">${{esc(r.rule_id)}}</td>
    <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      title="${{esc(r.file_label)}}">${{esc(r.file_label)}}</td>
    <td>${{esc(r.ifc_type)}}</td>
    <td>${{esc(r.name)}}</td>
    <td style="font-family:monospace;font-size:10px">${{esc(r.global_id)}}</td>
    <td style="max-width:360px">${{esc(r.message)}}</td>
  </tr>`).join("");
}}

// ── Severity Filter ───────────────────────────────────────────────────────

window.filterSev = function(sev) {{
  _currentFilter = sev;
  document.querySelectorAll("[id^='btn-sev-']").forEach(b => {{
    b.style.borderColor = b.id === `btn-sev-${{sev}}` ? "var(--accent)" : "";
    b.style.color       = b.id === `btn-sev-${{sev}}` ? "var(--accent)" : "";
  }});
  const filtered = sev === "all" ? _allResults : _allResults.filter(r => r.severity === sev);
  renderTable(filtered);
}};

// ── Summary Chips ─────────────────────────────────────────────────────────

function updateSummaryChips(summary) {{
  const el = document.getElementById("summary-chips");
  if (!summary) {{ el.innerHTML = ""; return; }}
  const chips = [
    [summary.total,    "#d0dce8", "Gesamt"],
    [summary.errors,   "#e94560", "Fehler"],
    [summary.warnings, "#ffb74d", "Warnungen"],
    [summary.infos,    "#4fc3f7", "Hinweise"],
  ];
  el.innerHTML = chips.map(([val, color, label]) =>
    `<span style="display:inline-flex;align-items:center;gap:5px;
      padding:2px 10px;border-radius:100px;font-size:11px;font-weight:600;
      background:rgba(0,0,0,.2);border:1px solid ${{color}}22;color:${{color}}">
      ${{val}} ${{label}}
    </span>`
  ).join("");
}}

// ── Prüfung starten ───────────────────────────────────────────────────────

async function runCheck() {{
  const docIds = [...document.querySelectorAll(".doc-chk:checked")].map(c => c.value);
  const rules  = [...document.querySelectorAll(".rule-chk:checked")].map(c => c.value);

  if (!docIds.length) {{
    document.getElementById("run-status").textContent = "⚠ Bitte mindestens eine Datei auswählen.";
    return;
  }}
  if (!rules.length) {{
    document.getElementById("run-status").textContent = "⚠ Bitte mindestens eine Regel auswählen.";
    return;
  }}

  const btn = document.getElementById("btn-run");
  btn.disabled = true;
  btn.textContent = "⏳ …";

  document.getElementById("placeholder").style.display = "none";
  document.getElementById("result-table").style.display = "none";
  document.getElementById("loading").style.display = "flex";
  document.getElementById("run-status").textContent =
    `${{docIds.length}} Datei(en) werden aus R2 geladen …`;
  updateSummaryChips(null);
  clearCache();

  try {{
    const resp = await fetch(RUN_URL, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{ document_ids: docIds, rules }}),
    }});
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${{resp.status}}`);

    _allResults = data.results || [];
    const s = data.summary || {{}};

    updateSummaryChips(s);

    // Export-Link aufbauen
    const exportBtn = document.getElementById("btn-export");
    const params = new URLSearchParams();
    docIds.forEach(id => params.append("document_ids", id));
    rules.forEach(r => params.append("rules", r));
    exportBtn.href = EXPORT_BASE + "?" + params.toString();
    exportBtn.style.display = "";

    document.getElementById("loading").style.display = "none";
    document.getElementById("result-table").style.display = "";
    filterSev("all");

    let statusText = `✓ ${{_allResults.length}} Befunde`;
    if (data.file_errors && data.file_errors.length) {{
      statusText += ` · ⚠ ${{data.file_errors.length}} Datei-Fehler`;
    }}
    document.getElementById("run-status").textContent = statusText;

    // ── sessionStorage: Ergebnisse cachen (identisch zu clash module) ─────
    saveToCache({{
      results:    _allResults,
      summary:    s,
      doc_ids:    docIds,
      rules:      rules,
      filter:     _currentFilter,
      export_params: params.toString(),
      ts:         Date.now(),
    }});

  }} catch(e) {{
    document.getElementById("loading").style.display = "none";
    document.getElementById("placeholder").style.display = "flex";
    document.getElementById("placeholder").innerHTML =
      `<span style="color:var(--accent2)">⚠ ${{esc(e.message)}}</span>`;
    document.getElementById("run-status").textContent = "Fehler: " + e.message;
  }} finally {{
    btn.disabled = false;
    btn.textContent = "▶ Prüfung starten";
  }}
}}

// ── Restore-Helfer ────────────────────────────────────────────────────────

function restoreDocSelection(docIds) {{
  if (!docIds || !docIds.length) return;
  document.querySelectorAll(".doc-chk").forEach(cb => {{
    cb.checked = docIds.includes(cb.value);
  }});
}}

function restoreRuleSelection(rules) {{
  if (!rules || !rules.length) return;
  document.querySelectorAll(".rule-chk").forEach(cb => {{
    cb.checked = rules.includes(cb.value);
  }});
}}

// ── Event Listeners ───────────────────────────────────────────────────────

document.getElementById("btn-run").addEventListener("click", runCheck);

document.getElementById("btn-clear-cache").addEventListener("click", () => {{
  clearCache();
  _allResults = [];
  _currentFilter = "all";
  document.getElementById("result-table").style.display = "none";
  document.getElementById("placeholder").style.display = "flex";
  document.getElementById("placeholder").innerHTML = `
    <svg width="44" height="44" fill="none" stroke="var(--muted)" stroke-width="1.5" viewBox="0 0 24 24">
      <path d="M9 11l3 3L22 4"/>
      <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11"/>
    </svg>
    <div>
      <div style="font-size:14px;color:var(--text);margin-bottom:6px">Cache geleert</div>
      <div style="font-size:12px;color:var(--muted)">
        Klicke <strong style="color:var(--accent)">▶ Prüfung starten</strong> um neu zu laden.
      </div>
    </div>`;
  updateSummaryChips(null);
  document.getElementById("run-status").textContent = "Cache geleert – neu laden";
  document.getElementById("btn-export").style.display = "none";
}});

// ── Restore aus sessionStorage (identisch zu project_clash.py) ───────────
(function restore() {{
  const cached = loadFromCache();
  if (!cached) return;

  // Auswahl wiederherstellen
  restoreDocSelection(cached.doc_ids);
  restoreRuleSelection(cached.rules);

  _allResults = cached.results || [];
  if (!_allResults.length) return;

  // Summary + Tabelle anzeigen
  updateSummaryChips(cached.summary || {{}});

  // Export-Link wiederherstellen
  const exportBtn = document.getElementById("btn-export");
  if (cached.export_params) {{
    exportBtn.href = EXPORT_BASE + "?" + cached.export_params;
    exportBtn.style.display = "";
  }}

  document.getElementById("loading").style.display = "none";
  document.getElementById("result-table").style.display = "";
  filterSev(cached.filter || "all");

  const age = Math.round((Date.now() - (cached.ts || 0)) / 1000);
  const ageLabel = age < 60 ? `${{age}}s` : `${{Math.round(age/60)}}min`;
  document.getElementById("run-status").textContent =
    `${{_allResults.length}} Befunde (Cache ${{ageLabel}} alt)`;

  showCacheBadge(true);

  // Sofort nach oben scrollen damit Ergebnisse direkt sichtbar sind
  const tableWrap = document.getElementById("table-wrap");
  if (tableWrap) tableWrap.scrollTop = 0;
}})();

}})();
</script>
"""
    return _page(f"{project['project_name']} – Checking", body)


# ─────────────────────────────────────────────────────────────────────────────
# Routen
# ─────────────────────────────────────────────────────────────────────────────

@project_rulecheck_router.get("/projects/{project_id}/checking", response_class=HTMLResponse)
def project_checking_page(
    request: Request,
    project_id: str,
    saved: str = Query(default=""),
    error: str = Query(default=""),
):
    account, project = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _checking_page(project, account)


@project_rulecheck_router.post("/projects/{project_id}/checking/run")
async def project_checking_run(request: Request, project_id: str):
    """
    Body (JSON):
    {
      "document_ids": ["id1", "id2", ...],
      "rules": ["missing_names", ...]
    }
    Lädt Dateien direkt aus R2 — kein slot cache.
    """
    account, project = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiges JSON."}, status_code=400)

    document_ids = [str(d).strip() for d in body.get("document_ids", []) if str(d).strip()]
    rules        = [str(r).strip() for r in body.get("rules", [])        if str(r).strip()]

    if not document_ids:
        return JSONResponse({"error": "Bitte mindestens eine Datei auswählen."}, status_code=400)
    if not rules:
        return JSONResponse({"error": "Bitte mindestens eine Regel auswählen."}, status_code=400)

    # Nur bekannte Regeln zulassen
    rules = [r for r in rules if r in _RULE_FUNCTIONS]
    if not rules:
        return JSONResponse({"error": "Keine gültigen Regeln angegeben."}, status_code=400)

    all_results: list = []
    file_errors: list = []
    files_checked = 0

    for doc_id in document_ids:
        try:
            model, label = _open_ifc_from_document(
                account["account_id"], project_id, doc_id
            )
        except Exception as exc:
            file_errors.append(f"{doc_id}: {exc}")
            continue

        results = run_rules_on_model(model, file_label=label, document_id=doc_id, rules=rules)
        all_results.extend(results)
        files_checked += 1

    errors   = sum(1 for r in all_results if r["severity"] == "error")
    warnings = sum(1 for r in all_results if r["severity"] == "warning")
    infos    = sum(1 for r in all_results if r["severity"] == "info")

    response_body: dict = {
        "summary": {
            "total":         len(all_results),
            "errors":        errors,
            "warnings":      warnings,
            "infos":         infos,
            "files_checked": files_checked,
        },
        "results": all_results,
    }
    if file_errors:
        response_body["file_errors"] = file_errors

    return JSONResponse(response_body)


@project_rulecheck_router.get("/projects/{project_id}/checking/export")
def project_checking_export(
    request: Request,
    project_id: str,
    document_ids: list[str] = Query(default=[]),
    rules: list[str]        = Query(default=[]),
):
    account, project = _load_context(request, project_id)
    if not project:
        return Response(content="Projekt nicht gefunden.", status_code=404)

    rule_list = [r for r in rules if r in _RULE_FUNCTIONS] or list(_RULE_FUNCTIONS.keys())

    all_results: list = []
    for doc_id in document_ids:
        try:
            model, label = _open_ifc_from_document(account["account_id"], project_id, doc_id)
            results = run_rules_on_model(model, file_label=label, document_id=doc_id, rules=rule_list)
            all_results.extend(results)
        except Exception:
            continue

    export_data = {
        "project_id":   project_id,
        "project_name": project.get("project_name", ""),
        "summary": {
            "total":    len(all_results),
            "errors":   sum(1 for r in all_results if r["severity"] == "error"),
            "warnings": sum(1 for r in all_results if r["severity"] == "warning"),
            "infos":    sum(1 for r in all_results if r["severity"] == "info"),
        },
        "results": all_results,
    }

    content = json.dumps(export_data, ensure_ascii=False, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="rulecheck_export.json"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Redirects  /viewer/rulecheck/*
# ─────────────────────────────────────────────────────────────────────────────

@project_rulecheck_router.get("/viewer/rulecheck/")
def viewer_rulecheck_legacy(
    project_id: str = Query(default=""),
    session_id: str = Query(default=""),
):
    if project_id:
        return RedirectResponse(f"/projects/{project_id}/checking", status_code=302)
    return HTMLResponse(
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<title>Rule-Check verschoben</title></head>"
        "<body style='font-family:sans-serif;padding:40px;background:#0e0e1a;color:#d0dce8'>"
        "<h2>Rule-Check ist jetzt ein eigenständiges Projektmodul</h2>"
        "<p style='color:#4a6080;margin-top:8px'>Öffne ein Projekt und wechsle zum Tab "
        "<strong>Checking</strong>.</p>"
        "<p style='margin-top:20px'><a href='/' style='color:#4fc3f7'>Zur Projektübersicht</a></p>"
        "</body></html>"
    )


@project_rulecheck_router.post("/viewer/rulecheck/run/")
async def viewer_rulecheck_run_legacy():
    return JSONResponse(
        {"error": "Diese API wurde nach /projects/{project_id}/checking/run verschoben."},
        status_code=410,
    )


@project_rulecheck_router.get("/viewer/rulecheck/export/")
def viewer_rulecheck_export_legacy():
    return Response(
        content="Dieser Endpunkt wurde nach /projects/{project_id}/checking/export verschoben.",
        status_code=410,
    )
