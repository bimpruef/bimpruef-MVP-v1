"""
projects.py – BIMPruef Projekt-Routen (zentrales Arbeitsmodul)

Der Viewer ist kein eigenständiges Modul mehr, sondern wird als integrierter
Bereich innerhalb eines geöffneten Projekts gerendert.  Alle Viewer-,
Clash-, Elementlisten- und Rule-Check-Seiten, die früher unter /viewer/…
aufgerufen wurden, stehen jetzt auch unter /projects/{id}/… zur Verfügung
und werden innerhalb der Projekt-Shell (Topbar + Subnav) dargestellt.

Die technischen API-Endpunkte in viewer.py, list_module.py und rulecheck.py
(AJAX-Calls, Datei-Downloads, JSON-Rückgaben) bleiben erhalten und werden
von JavaScript aus weiterhin direkt aufgerufen.

Route-Übersicht
───────────────
GET  /                                           → Projektübersicht
GET  /projects/new                               → Formular: neues Projekt
POST /projects/create                            → Projekt erstellen
GET  /projects/{id}                              → Projekt-Dashboard

── Model-Bereich (integrierter Viewer) ─────────────────────────────────────
GET  /projects/{id}/model                        → Viewer im Projekt-Shell
POST /projects/{id}/model/upload                 → IFC hochladen
POST /projects/{id}/model/remove                 → Slot entfernen
GET  /projects/{id}/model/file                   → Rohe IFC-Datei
GET  /projects/{id}/model/clash                  → Clash-Analyse
GET  /projects/{id}/model/clash/detail           → Clash-Detail-Ansicht
GET  /projects/{id}/model/clash/bcf              → BCF alle Clashes
GET  /projects/{id}/model/clash/bcf-single       → BCF einzelner Clash
GET  /projects/{id}/model/list                   → Elementliste
GET  /projects/{id}/model/list/data              → JSON-API Elementdaten
GET  /projects/{id}/model/list/export            → Excel-Export
GET  /projects/{id}/model/rulecheck              → Rule-Check
POST /projects/{id}/model/rulecheck/run          → Rule-Check ausführen (JSON)
GET  /projects/{id}/model/rulecheck/export       → Rule-Check-Export

── sonstige Projekt-Module (Platzhalter) ────────────────────────────────────
GET  /projects/{id}/documents
GET  /projects/{id}/issues
GET  /projects/{id}/todo
GET  /projects/{id}/checking
GET  /projects/{id}/settings
POST /projects/{id}/settings
POST /projects/{id}/delete
"""

import html
import json
import os

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_user
from app.project_storage import (
    create_project,
    delete_project,
    get_or_create_project_session,
    get_project,
    get_project_model_count,
    get_project_session,
    list_projects,
    update_project,
)
from app.storage import (
    ALLOWED_EXTENSIONS,
    MAX_FILES_PER_SESSION,
    get_ifc_label,
    get_ifc_path,
    get_session_slots,
    load_clash_cache,
    remove_ifc_slot,
    save_clash_cache,
    save_ifc_file,
    session_exists,
)

projects_router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsame Konstanten
# ─────────────────────────────────────────────────────────────────────────────

MAX_FILE_SIZE_MB = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
BCF_CLASH_LIMIT = int(os.environ.get("BCF_CLASH_LIMIT", "500"))


# ─────────────────────────────────────────────────────────────────────────────
# CSS / Layout-Helpers
# ─────────────────────────────────────────────────────────────────────────────

GLOBAL_STYLES = """
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0e1a;--surface:#16213e;--surface2:#1a2a4a;
  --border:#1e3a6e;--accent:#4fc3f7;--accent2:#e94560;
  --text:#d0dce8;--muted:#4a6080;--success:#4caf50;
}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);
  color:var(--text);min-height:100vh;line-height:1.5}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button,.btn{padding:7px 14px;background:var(--surface2);border:1px solid var(--border);
  color:var(--text);border-radius:6px;cursor:pointer;font-size:13px;
  transition:background .15s,border-color .15s;display:inline-block;text-decoration:none}
button:hover,.btn:hover{background:#223a5e;border-color:var(--accent);text-decoration:none}
.btn-primary{background:var(--accent);color:#0a1a2e;border-color:var(--accent);font-weight:600}
.btn-primary:hover{background:#81d4fa;text-decoration:none}
.btn-danger{background:#2a0a14;border-color:var(--accent2);color:#ffb3b3}
.btn-danger:hover{background:#6e1a2e;text-decoration:none}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:20px;margin-bottom:16px}
input[type=text],input[type=email],textarea,select{
  background:var(--surface2);border:1px solid var(--border);color:var(--text);
  padding:8px 12px;border-radius:6px;font-size:14px;font-family:inherit;
  outline:none;transition:border-color .2s;width:100%}
input:focus,textarea:focus,select:focus{border-color:var(--accent)}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px;margin-top:12px}
.flash-err{background:#2a0a10;border:1px solid var(--accent2);border-radius:8px;
  padding:10px 14px;color:#ffaaaa;font-size:13px;margin-bottom:14px}
.flash-ok{background:#0a2a10;border:1px solid var(--success);border-radius:8px;
  padding:10px 14px;color:#aaffaa;font-size:13px;margin-bottom:14px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--border);padding:10px 14px;text-align:left;vertical-align:middle}
th{background:var(--surface2);color:#8ab;font-size:12px;font-weight:600}
tr:hover td{background:rgba(79,195,247,.04)}
.badge{display:inline-block;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600}
.badge-active{background:#0a3a20;color:#4caf50;border:1px solid #1a6040}
.badge-inactive{background:#2a1a0a;color:#ff9800;border:1px solid #6e3a1e}
footer{text-align:center;padding:24px 0 12px;border-top:1px solid var(--border);
  color:var(--muted);font-size:12px;margin-top:40px}
/* Viewer-spezifische Stile (für integrierte Ansichten) */
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.tag-1{background:#0d2a3e;color:#4fc3f7;border:1px solid #1a4a6e}
.tag-2{background:#3e0d1a;color:#ef9a9a;border:1px solid #6e1a2e}
.model-card{padding:5px 10px;border-bottom:1px solid var(--border)}
.cat-item{display:flex;align-items:center;gap:6px;padding:3px 10px;cursor:pointer;
  user-select:none;border-bottom:1px solid #1a2540}
.cat-item:hover{background:#1e2f50}
@keyframes spin{to{transform:rotate(360deg)}}
</style>"""


def _e(s) -> str:
    return html.escape(str(s or ""))


def _brand_logo(height_px: int = 28) -> str:
    icon_size = max(27, int(height_px * 1.28))
    text_size = max(18, int(height_px * 0.92))
    return (
        f'<div style="display:flex;align-items:center;gap:8px;line-height:1">'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 54" role="img"'
        f' style="height:{icon_size}px;width:auto;display:block">'
        f'<g fill="none" stroke-linejoin="round" stroke-linecap="round" stroke-width="2.8">'
        f'<g stroke="#1f5f9f"><path d="M8 18 24 8l16 10-16 10z"/>'
        f'<path d="M8 18v18l16 10V28z"/><path d="M40 18v18L24 46V28z"/></g>'
        f'<g stroke="#d8192f"><path d="M29 18 45 8l16 10-16 10z"/>'
        f'<path d="M29 18v18l16 10V28z"/><path d="M61 18v18L45 46V28z"/></g>'
        f'<g stroke="#8f4399"><path d="M50 18 66 8l16 10-16 10z"/>'
        f'<path d="M50 18v18l16 10V28z"/><path d="M82 18v18L66 46V28z"/></g>'
        f'<g stroke="#27a6ad"><path d="M71 18 87 8l16 10-16 10z"/>'
        f'<path d="M71 18v18l16 10V28z"/><path d="M103 18v18L87 46V28z"/></g>'
        f'</g></svg>'
        f'<span style="font-family:\'Avenir Next\',\'Montserrat\',\'Segoe UI\',sans-serif;'
        f'font-weight:300;letter-spacing:1.2px;color:var(--text);font-size:{text_size}px;'
        f'white-space:nowrap;text-transform:uppercase">BIMPRUEF</span>'
        f'</div>'
    )


def _topbar_global(account: dict) -> str:
    return (
        f'<div style="display:flex;align-items:center;gap:12px;padding:8px 20px;'
        f'background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">'
        f'{_brand_logo(22)}'
        f'<span style="color:var(--muted);font-size:12px">|</span>'
        f'<span style="font-size:12px;color:var(--muted)">Account:</span>'
        f'<span style="font-size:13px;font-weight:600;color:var(--accent)">'
        f'{_e(account["account_name"])}</span>'
        f'<div style="margin-left:auto;display:flex;gap:8px">'
        f'<a href="/impressum" style="font-size:11px;color:var(--muted)">Impressum</a>'
        f'<a href="/datenschutz" style="font-size:11px;color:var(--muted)">Datenschutz</a>'
        f'<form method="POST" action="/auth/logout" style="margin:0">'
        f'<button type="submit" class="btn" style="font-size:11px;padding:3px 9px">'
        f'Logout</button></form>'
        f'</div></div>'
    )


def _project_subnav(project_id: str, active: str) -> str:
    """Haupt-Subnav mit Model als integriertem Bereich (kein externer Link mehr)."""
    pid = _e(project_id)
    items = [
        ("dashboard",  f"/projects/{pid}",              "📊 Dashboard"),
        ("model",      f"/projects/{pid}/model",         "🏗 Model"),
        ("documents",  f"/projects/{pid}/documents",     "📄 Documents"),
        ("issues",     f"/projects/{pid}/issues",        "🐛 Issues"),
        ("todo",       f"/projects/{pid}/todo",          "☑ To-do"),
        ("checking",   f"/projects/{pid}/checking",      "✅ Checking"),
        ("settings",   f"/projects/{pid}/settings",      "⚙ Settings"),
    ]
    parts = []
    for key, href, label in items:
        if key == active:
            style = (
                "padding:8px 14px;font-size:13px;border-radius:6px;"
                "color:var(--accent);border-bottom:2px solid var(--accent);"
                "text-decoration:none"
            )
        else:
            style = "padding:8px 14px;font-size:13px;border-radius:6px;color:var(--text);text-decoration:none"
        parts.append(f'<a href="{href}" style="{style}">{label}</a>')
    return (
        '<div style="display:flex;align-items:center;gap:2px;padding:4px 16px;'
        'background:#0d1a30;border-bottom:1px solid var(--border)">'
        + "".join(parts) + "</div>"
    )


def _model_subnav(project_id: str, session_id: str, active: str) -> str:
    """
    Sekundäre Navigationsleiste innerhalb des Model-Bereichs.
    Zeigt Viewer, Clash, Liste, Rule-Check als integrierte Tabs.
    """
    pid = _e(project_id)
    sid = _e(session_id)
    items = [
        ("viewer",     f"/projects/{pid}/model",                 "🔍 3D-Viewer"),
        ("clash",      f"/projects/{pid}/model/clash?session_id={sid}", "⚡ Clash"),
        ("list",       f"/projects/{pid}/model/list?session_id={sid}",  "📋 Elementliste"),
        ("rulecheck",  f"/projects/{pid}/model/rulecheck?session_id={sid}", "✅ Rule-Check"),
    ]
    parts = []
    for key, href, label in items:
        if key == active:
            style = (
                "padding:5px 12px;font-size:12px;border-radius:4px;"
                "background:var(--surface2);border:1px solid var(--accent);"
                "color:var(--accent);text-decoration:none"
            )
        else:
            style = (
                "padding:5px 12px;font-size:12px;border-radius:4px;"
                "background:var(--surface2);border:1px solid var(--border);"
                "color:var(--text);text-decoration:none"
            )
        parts.append(f'<a href="{href}" style="{style}">{label}</a>')
    return (
        '<div style="display:flex;align-items:center;gap:6px;padding:6px 16px;'
        'background:#0a1528;border-bottom:1px solid var(--border)">'
        + "".join(parts) + "</div>"
    )


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
{GLOBAL_STYLES}
</head>
<body>
{body}
<footer>
  <p>BIMPruef Platform by Foad Amini &nbsp;·&nbsp;
  <a href="/impressum">Impressum</a> &nbsp;·&nbsp;
  <a href="/datenschutz">Datenschutz</a></p>
</footer>
</body>
</html>""")


def _page_no_footer(title: str, body: str) -> HTMLResponse:
    """Seitenvorlage ohne Footer – für Vollbild-Viewer-Ansichten."""
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
{GLOBAL_STYLES}
</head>
<body style="overflow:hidden">
{body}
</body>
</html>""")


def _account_from_request(request: Request) -> dict:
    user = require_user(request)
    return {
        "account_id":   user["user_id"],
        "account_name": user["email"],
        "workspace":    "Default",
    }


def _placeholder_page(project: dict, account: dict, active: str) -> HTMLResponse:
    pname = project.get("project_name", "")
    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project["project_id"], active)}'
        '<div style="padding:40px 32px;max-width:800px">'
        f'<h2 style="color:var(--muted);font-size:18px">{_e(active.title())} – {_e(pname)}</h2>'
        '<p style="color:var(--muted);margin-top:12px">Dieses Modul ist in Entwicklung.</p>'
        '</div>'
    )
    return _page(f"{active.title()} – {pname}", body)


# ─────────────────────────────────────────────────────────────────────────────
# Viewer-JS  (unverändert aus viewer.py – hier eingebettet, damit der
# Model-Tab vollständig in der Projekt-Shell gerendert werden kann ohne
# eine HTTP-Weiterleitung zu benötigen)
# ─────────────────────────────────────────────────────────────────────────────

def _viewer_js_url(session_id: str, project_id: str) -> str:
    """
    Gibt die URL zurück, unter der der Viewer-JS die IFC-Datei abruft.
    Wir delegieren an den bestehenden /viewer/file/-Endpunkt, da dort
    die gesamte IFC-Auslieferungslogik (inkl. Content-Type) implementiert ist.
    """
    return f"/viewer/file/?session_id={_e(session_id)}"


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktion: Slot-Übersicht (Upload-Bereich)
# ─────────────────────────────────────────────────────────────────────────────

def _render_slot_list(project_id: str, session_id: str) -> str:
    """Rendert die Liste der hochgeladenen IFC-Dateien mit Entfernen-Buttons."""
    pid = _e(project_id)
    sid = _e(session_id)
    slots = get_session_slots(session_id)
    if not slots:
        return (
            '<p style="color:var(--muted);font-size:13px;padding:8px 0">'
            'Noch keine Dateien hochgeladen.</p>'
        )
    parts = []
    for slot in slots:
        label = get_ifc_label(session_id, slot)
        parts.append(
            f'<div style="display:flex;align-items:center;gap:10px;padding:6px 0;'
            f'border-bottom:1px solid var(--border)">'
            f'<span style="font-size:11px;background:var(--surface2);padding:2px 7px;'
            f'border-radius:4px;border:1px solid var(--border);color:var(--muted)">Slot {slot}</span>'
            f'<span style="font-size:13px;flex:1">{_e(label)}</span>'
            f'<form method="POST" action="/projects/{pid}/model/remove" style="margin:0">'
            f'<input type="hidden" name="session_id" value="{sid}">'
            f'<input type="hidden" name="slot" value="{slot}">'
            f'<button type="submit" class="btn btn-danger" style="font-size:11px;padding:3px 9px">'
            f'Entfernen</button>'
            f'</form>'
            f'</div>'
        )
    return "".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Startseite – Projektübersicht
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/", response_class=HTMLResponse)
def project_list(request: Request):
    account = _account_from_request(request)
    account_id = account["account_id"]
    projects = list_projects(account_id)

    rows = []
    for p in projects:
        count = get_project_model_count(account_id, p["project_id"])
        pid = _e(p["project_id"])
        badge_cls = "badge-active" if p.get("status") == "active" else "badge-inactive"
        rows.append(
            f'<tr>'
            f'<td><a href="/projects/{pid}" style="font-weight:600">{_e(p["project_name"])}</a></td>'
            f'<td>{_e(p["project_code"])}</td>'
            f'<td><span class="badge {badge_cls}">{_e(p.get("status",""))}</span></td>'
            f'<td style="color:var(--muted)">{count} Modell{"e" if count != 1 else ""}</td>'
            f'<td style="color:var(--muted);font-size:11px">{_e(p.get("created_at","")[:10])}</td>'
            f'<td>'
            f'<a href="/projects/{pid}" class="btn" style="font-size:11px;padding:3px 9px">Öffnen</a>'
            f'</td>'
            f'</tr>'
        )

    table_html = (
        '<table><thead><tr>'
        '<th>Projektname</th><th>Code</th><th>Status</th>'
        '<th>Modelle</th><th>Erstellt</th><th></th>'
        '</tr></thead><tbody>'
        + ("".join(rows) if rows else
           '<tr><td colspan="6" style="text-align:center;color:var(--muted);padding:30px">'
           'Noch keine Projekte vorhanden.</td></tr>')
        + '</tbody></table>'
    )

    body = (
        f'{_topbar_global(account)}'
        '<div style="padding:32px;max-width:1100px">'
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">'
        '<h1 style="font-size:22px;font-weight:600">Meine Projekte</h1>'
        '<a href="/projects/new" class="btn btn-primary">+ Neues Projekt</a>'
        '</div>'
        f'<div class="card" style="padding:0;overflow:hidden">{table_html}</div>'
        '</div>'
    )
    return _page("Projekte – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# Projekt erstellen
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/new", response_class=HTMLResponse)
def project_new(request: Request):
    account = _account_from_request(request)
    body = (
        f'{_topbar_global(account)}'
        '<div style="padding:32px;max-width:600px">'
        '<h1 style="font-size:20px;margin-bottom:20px">Neues Projekt anlegen</h1>'
        '<div class="card">'
        '<form method="POST" action="/projects/create">'
        '<label>Projektcode *</label>'
        '<input type="text" name="project_code" required maxlength="40" placeholder="z. B. P-2024-001">'
        '<label>Projektname *</label>'
        '<input type="text" name="project_name" required maxlength="120" placeholder="Projektbezeichnung">'
        '<label>Beschreibung</label>'
        '<textarea name="description" rows="3" style="resize:vertical" placeholder="Optional"></textarea>'
        '<div style="margin-top:20px;display:flex;gap:10px">'
        '<button type="submit" class="btn btn-primary">Projekt anlegen</button>'
        '<a href="/" class="btn">Abbrechen</a>'
        '</div>'
        '</form>'
        '</div>'
        '</div>'
    )
    return _page("Neues Projekt – BIMPruef", body)


@projects_router.post("/projects/create")
async def project_create(
    request: Request,
    project_code: str = Form(...),
    project_name: str = Form(...),
    description: str  = Form(default=""),
):
    account = _account_from_request(request)
    project = create_project(
        account["account_id"],
        project_code=project_code,
        project_name=project_name,
        description=description,
    )
    return RedirectResponse(f"/projects/{project['project_id']}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Projekt-Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_dashboard(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pname  = project.get("project_name", "")
    pcode  = project.get("project_code", "")
    status = project.get("status", "active")
    badge  = (
        f'<span class="badge badge-active">active</span>'
        if status == "active"
        else f'<span class="badge badge-inactive">{_e(status)}</span>'
    )
    pid = _e(project_id)

    # Session-ID für Schnellzugriff-Links (nur wenn bereits vorhanden)
    sid = get_project_session(account_id, project_id) or ""
    model_count = get_project_model_count(account_id, project_id)

    stat_cards = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));'
        f'gap:12px;margin-top:20px">'
        f'<div class="card" style="text-align:center">'
        f'<div style="font-size:28px;font-weight:700;color:var(--accent)">{model_count}</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-top:4px">IFC-Modelle</div>'
        f'</div>'
        f'<div class="card" style="text-align:center">'
        f'<div style="font-size:28px;font-weight:700;color:var(--muted)">–</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-top:4px">Issues</div>'
        f'</div>'
        f'<div class="card" style="text-align:center">'
        f'<div style="font-size:28px;font-weight:700;color:var(--muted)">–</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-top:4px">To-dos</div>'
        f'</div>'
        f'</div>'
    )

    quick_links = (
        f'<div style="margin-top:20px">'
        f'<h3 style="font-size:14px;color:var(--muted);margin-bottom:10px;'
        f'text-transform:uppercase;letter-spacing:.5px">Schnellzugriff</h3>'
        f'<div style="display:flex;flex-wrap:wrap;gap:8px">'
        f'<a href="/projects/{pid}/model" class="btn btn-primary">🏗 Model öffnen</a>'
        f'</div>'
        f'</div>'
    )

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "dashboard")}'
        '<div style="padding:28px 32px;max-width:1000px">'
        f'<div style="display:flex;align-items:flex-start;'
        f'justify-content:space-between;margin-bottom:4px">'
        f'<div>'
        f'<h1 style="font-size:22px;font-weight:600">{_e(pname)}</h1>'
        f'<p style="color:var(--muted);font-size:13px;margin-top:2px">'
        f'{_e(pcode)} &nbsp;·&nbsp; {badge}</p>'
        f'</div>'
        f'<a href="/" class="btn" style="font-size:12px">← Alle Projekte</a>'
        f'</div>'
    )

    if project.get("description"):
        body += (
            f'<p style="color:var(--muted);font-size:13px;margin-top:8px">'
            f'{_e(project["description"])}</p>'
        )

    body += stat_cards + quick_links + "</div>"
    return _page(f"{pname} – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# ── MODEL-BEREICH  (integrierter Viewer) ─────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

# ── Hauptseite: Upload + 3D-Viewer ──────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model", response_class=HTMLResponse)
def project_model(request: Request, project_id: str):
    """
    Haupt-Viewer-Seite innerhalb des Projekts.
    Zeigt Upload-Bereich und 3D-Viewer – alles innerhalb der Projekt-Shell.
    """
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    sid = get_or_create_project_session(account_id, project_id)
    pid = _e(project_id)
    sid_e = _e(sid)
    pname = _e(project.get("project_name", ""))

    slots = get_session_slots(sid)
    slot_list_html = _render_slot_list(project_id, sid)

    # Farben für bis zu 10 Slots
    COLORS = [
        "#4fc3f7", "#ef9a9a", "#a5d6a7", "#fff176", "#ce93d8",
        "#ffb74d", "#80cbc4", "#f48fb1", "#bcaaa4", "#90caf9",
    ]

    model_cards_html = ""
    model_urls_js = "[]"
    if slots:
        cards = []
        urls_list = []
        for i, slot in enumerate(slots):
            col = COLORS[i % len(COLORS)]
            label = get_ifc_label(sid, slot)
            url = f"/viewer/file/?session_id={sid_e}&slot={slot}"
            cards.append(
                f'<div class="model-card">'
                f'<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">'
                f'<input type="checkbox" class="chk-model" data-slot="{slot}" checked '
                f'style="accent-color:{col};width:13px;height:13px">'
                f'<span style="width:10px;height:10px;border-radius:50%;border:2px solid {col};'
                f'flex-shrink:0;display:inline-block"></span>'
                f'{_e(label)}'
                f'</label>'
                f'</div>'
            )
            urls_list.append(
                f'{{url:"{url}",label:{json.dumps(label)},slot:{slot},color:{json.dumps(col)}}}'
            )
        model_cards_html = "".join(cards)
        model_urls_js = "[" + ",".join(urls_list) + "]"

    # Viewer-JS aus viewer.py wiederverwenden (über Import)
    from app.viewer import _viewer_js, BCF_CLASH_LIMIT as _BCF
    viewer_js_code = _viewer_js(model_urls_js, session_id=sid)

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "model")}'
        f'{_model_subnav(project_id, sid, "viewer")}'
        # Layout: Sidebar links, Canvas rechts
        '<div style="display:flex;height:calc(100vh - 110px);overflow:hidden">'

        # ── Linke Sidebar ──────────────────────────────────────────────────
        '<div style="width:260px;min-width:260px;background:var(--surface);'
        'border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">'

        # Upload-Bereich
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Modelle hochladen</div>'
        f'<form method="POST" action="/projects/{pid}/model/upload"'
        ' enctype="multipart/form-data"'
        ' style="padding:10px">'
        f'<input type="hidden" name="session_id" value="{sid_e}">'
        f'<input type="file" name="file" accept=".ifc,.ifczip" required '
        f'style="background:var(--surface2);border:1px solid var(--border);color:var(--text);'
        f'padding:6px;border-radius:6px;font-size:12px;width:100%;margin-bottom:6px">'
        f'<button type="submit" class="btn btn-primary" style="width:100%;font-size:12px">'
        f'Hochladen</button>'
        f'<div style="font-size:10px;color:var(--muted);margin-top:4px">'
        f'.ifc / .ifczip · max. {MAX_FILE_SIZE_MB} MB · max. {MAX_FILES_PER_SESSION} Dateien'
        f'</div>'
        f'</form>'

        # Geladene Modelle
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Geladene Modelle</div>'
        f'<div id="model-list" style="padding:6px 10px;overflow-y:auto;flex:1">'
        + (model_cards_html or
           '<p style="color:var(--muted);font-size:12px;padding:8px 0">Keine Modelle geladen.</p>')
        + '</div>'

        # Slot-Verwaltung
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Dateiverwaltung</div>'
        f'<div style="padding:6px 10px;overflow-y:auto;max-height:160px">'
        f'{slot_list_html}'
        f'</div>'

        # Kategorien
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Kategorien</div>'
        '<div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0">'
        '<div style="padding:8px 10px;font-size:11px;color:var(--muted);font-style:italic">'
        'Wird nach dem Laden angezeigt …'
        '</div>'
        '</div>'
        '</div>'  # Ende Sidebar

        # ── Canvas ────────────────────────────────────────────────────────
        '<div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden">'
        '<canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>'
        '<div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;background:rgba(14,14,26,.93);z-index:20">'
        '<div style="width:40px;height:40px;border:4px solid #0f3460;'
        'border-top-color:var(--accent2);border-radius:50%;'
        'animation:spin .7s linear infinite;margin-bottom:12px"></div>'
        '<p id="load-txt" style="color:#889;font-size:13px">'
        + ('Modell wird geladen …' if slots else 'Bitte eine IFC-Datei hochladen.')
        + '</p>'
        '</div>'
        '</div>'  # Ende Canvas

        # ── Info-Panel rechts ─────────────────────────────────────────────
        '<div id="info-panel" style="width:280px;min-width:280px;background:var(--surface);'
        'border-left:1px solid var(--border);display:flex;flex-direction:column;'
        'overflow:hidden;flex-shrink:0">'
        '<div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px;'
        'display:flex;align-items:center;justify-content:space-between;flex-shrink:0">'
        '<span>Element-Info</span>'
        '<span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px">✕</span>'
        '</div>'
        '<div id="info-body" style="flex:1;overflow-y:auto;padding:10px;font-size:12px">'
        '<div style="color:var(--muted);font-style:italic">Klick auf ein Element für Details.</div>'
        '</div>'

        # AI-Chat
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">KI-Assistent</div>'
        '<div style="padding:8px 10px;display:flex;flex-direction:column;gap:6px">'
        f'<select id="ai-slot" style="font-size:11px">'
        + "".join(
            f'<option value="{s}">{_e(get_ifc_label(sid, s))}</option>'
            for s in slots
        )
        + ('</select>'
           '<textarea id="ai-question" rows="2" placeholder="Frage zum Modell …" '
           'style="font-size:12px;resize:none"></textarea>'
           '<button id="ai-send" class="btn btn-primary" style="font-size:11px">Senden</button>'
           '<div id="ai-answer" style="font-size:11px;color:var(--muted);'
           'min-height:40px;overflow-y:auto;max-height:120px"></div>'
           '</div>'
           '</div>')  # Ende Info-Panel

        + '</div>'  # Ende Haupt-Flex

        + '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>'
        + f'<script>{viewer_js_code}</script>'

        # AI-Chat JS (ruft /viewer/ai-chat/ – bestehender Endpunkt)
        + f"""<script>
(function(){{
  const sendBtn = document.getElementById('ai-send');
  if (!sendBtn) return;
  sendBtn.addEventListener('click', async () => {{
    const slot = document.getElementById('ai-slot')?.value || '1';
    const q    = document.getElementById('ai-question')?.value?.trim();
    const ans  = document.getElementById('ai-answer');
    if (!q) {{ ans.textContent = 'Bitte eine Frage eingeben.'; return; }}
    ans.textContent = '⏳ Wird verarbeitet …';
    sendBtn.disabled = true;
    try {{
      const fd = new FormData();
      fd.append('session_id', '{sid_e}');
      fd.append('slot', slot);
      fd.append('question', q);
      const r = await fetch('/viewer/ai-chat/', {{method:'POST', body:fd}});
      const d = await r.json();
      ans.textContent = d.answer || d.error || 'Keine Antwort.';
    }} catch(e) {{
      ans.textContent = 'Fehler: ' + e.message;
    }} finally {{
      sendBtn.disabled = false;
    }}
  }});
}})();
</script>"""
    )

    return _page_no_footer(f"Model – {project.get('project_name','')} – BIMPruef", body)


# ── Upload ────────────────────────────────────────────────────────────────────

@projects_router.post("/projects/{project_id}/model/upload")
async def project_model_upload(
    request: Request,
    project_id: str,
    session_id: str = Form(...),
    file: UploadFile = File(...),
):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pid = _e(project_id)
    try:
        content = await file.read()
        if len(content) > MAX_FILE_SIZE_BYTES:
            return HTMLResponse(
                f'<p style="color:red">Datei zu groß (max. {MAX_FILE_SIZE_MB} MB).</p>'
                f'<a href="/projects/{pid}/model">← Zurück</a>',
                status_code=413,
            )
        # Nächsten freien Slot ermitteln
        slots = get_session_slots(session_id)
        if len(slots) >= MAX_FILES_PER_SESSION:
            return HTMLResponse(
                f'<p style="color:red">Maximale Anzahl Dateien ({MAX_FILES_PER_SESSION}) erreicht.</p>'
                f'<a href="/projects/{pid}/model">← Zurück</a>',
                status_code=400,
            )
        next_slot = max(slots, default=0) + 1
        save_ifc_file(session_id, next_slot, content, file.filename or "uploaded.ifc")
    except Exception as exc:
        return HTMLResponse(
            f'<p style="color:red">Upload fehlgeschlagen: {_e(str(exc))}</p>'
            f'<a href="/projects/{pid}/model">← Zurück</a>',
            status_code=500,
        )

    return RedirectResponse(f"/projects/{pid}/model", status_code=303)


# ── Slot entfernen ────────────────────────────────────────────────────────────

@projects_router.post("/projects/{project_id}/model/remove")
async def project_model_remove(
    request: Request,
    project_id: str,
    session_id: str = Form(...),
    slot: int       = Form(...),
):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    try:
        remove_ifc_slot(session_id, slot)
    except Exception:
        pass
    return RedirectResponse(f"/projects/{_e(project_id)}/model", status_code=303)


# ── Rohe IFC-Datei ausliefern ────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/file")
def project_model_file(
    request: Request,
    project_id: str,
    session_id: str = Query(...),
    slot: int       = Query(default=1),
):
    """
    Liefert die rohe IFC-Datei für den 3D-Viewer.
    Delegiert an den bestehenden /viewer/file/-Endpunkt-Implementierung,
    reproduziert die Logik hier direkt um eine Weiterleitung zu vermeiden.
    """
    account = _account_from_request(request)
    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return Response(content="Datei nicht gefunden.", status_code=404)
    try:
        with open(path, "rb") as f:
            data = f.read()
        filename = get_ifc_label(session_id, slot, fallback=f"model_{slot}.ifc")
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)


# ── Clash-Analyse ─────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/clash", response_class=HTMLResponse)
def project_model_clash(
    request: Request,
    project_id: str,
    session_id: str  = Query(default=""),
    slot_a: int      = Query(default=0),
    slot_b: int      = Query(default=0),
    tolerance: float = Query(default=0.0),
    run: int         = Query(default=0),
):
    """
    Clash-Analyse-Seite innerhalb der Projekt-Shell.
    Delegiert die eigentliche Berechnung an clash.py / die bestehende Logik.
    Die UI wird in der Projekt-Shell (Topbar + Subnav) eingebettet.
    """
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pid = _e(project_id)

    # Session aus Projekt ableiten falls nicht übergeben
    if not session_id:
        session_id = get_or_create_project_session(account_id, project_id)
    sid = _e(session_id)

    if not session_exists(session_id):
        return RedirectResponse(f"/projects/{pid}/model", status_code=302)

    slots = get_session_slots(session_id)

    # Slot-Vorauswahl
    if slot_a == 0 and len(slots) >= 1:
        slot_a = slots[0]
    if slot_b == 0 and len(slots) >= 2:
        slot_b = slots[1]

    def _slot_options(selected: int) -> str:
        return "".join(
            f'<option value="{s}" {"selected" if s == selected else ""}>'
            f'{_e(get_ifc_label(session_id, s))}</option>'
            for s in slots
        )

    clashes = []
    error_msg = ""
    if run and slot_a and slot_b:
        try:
            cached = load_clash_cache(session_id, tolerance, slot_a=slot_a, slot_b=slot_b)
            if cached is not None:
                clashes = cached
            else:
                from app.clash import get_group_elements, compare_element_groups_for_clashes
                ga = get_group_elements(session_id, [slot_a], [])
                gb = get_group_elements(session_id, [slot_b], [])
                clashes = compare_element_groups_for_clashes(ga, gb, tolerance=tolerance)
                save_clash_cache(session_id, tolerance, clashes, slot_a=slot_a, slot_b=slot_b)
        except Exception as exc:
            error_msg = str(exc)

    # Clash-Tabelle
    clash_rows = ""
    if clashes:
        for i, c in enumerate(clashes):
            detail_url = (
                f"/projects/{pid}/model/clash/detail"
                f"?session_id={sid}&slot_a={slot_a}&slot_b={slot_b}"
                f"&clash_index={i}&tolerance={tolerance}"
            )
            clash_rows += (
                f'<tr>'
                f'<td style="text-align:right;color:var(--muted)">{i+1}</td>'
                f'<td>{_e(str(c.get("type_1","")))}</td>'
                f'<td>{_e(str(c.get("name_1","")))}</td>'
                f'<td style="font-family:monospace;font-size:11px">{_e(str(c.get("global_id_1","")))}</td>'
                f'<td>{_e(str(c.get("type_2","")))}</td>'
                f'<td>{_e(str(c.get("name_2","")))}</td>'
                f'<td style="font-family:monospace;font-size:11px">{_e(str(c.get("global_id_2","")))}</td>'
                f'<td><a href="{detail_url}" class="btn" style="font-size:10px;padding:2px 7px">Detail</a>'
                f'<a href="/projects/{pid}/model/clash/bcf-single'
                f'?session_id={sid}&slot_a={slot_a}&slot_b={slot_b}'
                f'&clash_index={i}&tolerance={tolerance}" '
                f'class="btn" style="font-size:10px;padding:2px 7px;margin-left:4px">BCF</a>'
                f'</td>'
                f'</tr>'
            )

    bcf_url = (
        f"/projects/{pid}/model/clash/bcf"
        f"?session_id={sid}&slot_a={slot_a}&slot_b={slot_b}&tolerance={tolerance}"
    )

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "model")}'
        f'{_model_subnav(project_id, session_id, "clash")}'
        '<div style="padding:20px 24px;overflow-y:auto;height:calc(100vh - 115px)">'

        # Formular
        '<div class="card">'
        '<h2 style="font-size:15px;margin-bottom:14px">Clash-Analyse</h2>'
        f'<form method="GET" action="/projects/{pid}/model/clash" style="display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end">'
        f'<input type="hidden" name="session_id" value="{sid}">'
        f'<div><label>Modell A</label><select name="slot_a">{_slot_options(slot_a)}</select></div>'
        f'<div><label>Modell B</label><select name="slot_b">{_slot_options(slot_b)}</select></div>'
        f'<div><label>Toleranz (m)</label>'
        f'<input type="number" name="tolerance" value="{tolerance}" step="0.001" min="0" style="width:100px"></div>'
        f'<input type="hidden" name="run" value="1">'
        f'<button type="submit" class="btn btn-primary">Analyse starten</button>'
        f'</form>'
        '</div>'

        + (f'<div class="flash-err">Fehler: {_e(error_msg)}</div>' if error_msg else "")

        + (
            f'<div class="card" style="margin-top:0">'
            f'<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">'
            f'<span style="font-weight:600">{len(clashes)} Clash{"es" if len(clashes)!=1 else ""} gefunden</span>'
            + (f'<a href="{bcf_url}" class="btn btn-primary" style="font-size:12px">BCF herunterladen</a>'
               if clashes else "")
            + '</div>'
            + (
                '<div style="overflow-x:auto"><table>'
                '<thead><tr><th>#</th><th>Typ A</th><th>Name A</th><th>GlobalId A</th>'
                '<th>Typ B</th><th>Name B</th><th>GlobalId B</th><th></th></tr></thead>'
                f'<tbody>{clash_rows}</tbody></table></div>'
                if clashes else
                '<p style="color:var(--muted)">Keine Clashes gefunden.</p>'
            )
            + '</div>'
            if run else ""
        )

        + '</div>'
    )
    return _page(f"Clash – {project.get('project_name','')} – BIMPruef", body)


# ── Clash-Detail ──────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/clash/detail", response_class=HTMLResponse)
def project_model_clash_detail(
    request: Request,
    project_id:  str,
    session_id:  str  = Query(...),
    slot_a:      int  = Query(...),
    slot_b:      int  = Query(...),
    clash_index: int  = Query(...),
    tolerance:   float = Query(default=0.0),
):
    """
    Detail-Ansicht eines einzelnen Clashs im integrierten 3D-Viewer.
    Ruft die Logik von viewer.py auf, bettet sie aber in die Projekt-Shell ein.
    """
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pid = _e(project_id)
    sid = _e(session_id)

    clashes = load_clash_cache(session_id, tolerance, slot_a=slot_a, slot_b=slot_b)
    if clashes is None:
        try:
            from app.clash import get_group_elements, compare_element_groups_for_clashes
            ga = get_group_elements(session_id, [slot_a], [])
            gb = get_group_elements(session_id, [slot_b], [])
            clashes = compare_element_groups_for_clashes(ga, gb, tolerance=tolerance)
            save_clash_cache(session_id, tolerance, clashes, slot_a=slot_a, slot_b=slot_b)
        except Exception as exc:
            return Response(content=f"Fehler: {exc}", status_code=500)

    if clash_index < 0 or clash_index >= len(clashes):
        return Response(content="Clash-Index außerhalb des Bereichs.", status_code=404)

    clash = clashes[clash_index]
    gid1 = clash.get("global_id_1", "")
    gid2 = clash.get("global_id_2", "")

    COLORS = ["#4fc3f7", "#ef9a9a"]
    label_a = get_ifc_label(session_id, slot_a)
    label_b = get_ifc_label(session_id, slot_b)
    col_a, col_b = COLORS[0], COLORS[1]

    model_urls_js = (
        f'[{{url:"/viewer/file/?session_id={sid}&slot={slot_a}",'
        f'label:{json.dumps(label_a)},slot:{slot_a},color:{json.dumps(col_a)}}},'
        f'{{url:"/viewer/file/?session_id={sid}&slot={slot_b}",'
        f'label:{json.dumps(label_b)},slot:{slot_b},color:{json.dumps(col_b)}}}]'
    )

    back_url = (
        f"/projects/{pid}/model/clash"
        f"?session_id={sid}&slot_a={slot_a}&slot_b={slot_b}&run=1&tolerance={tolerance}"
    )

    from app.viewer import _viewer_js
    viewer_js_code = _viewer_js(model_urls_js, highlight_gids=[gid1, gid2], session_id=session_id)

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "model")}'
        f'{_model_subnav(project_id, session_id, "clash")}'
        '<div style="display:flex;height:calc(100vh - 110px);overflow:hidden">'

        # Sidebar
        '<div style="width:220px;min-width:220px;background:var(--surface);'
        'border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">'
        f'<div style="padding:6px 10px"><a href="{back_url}" class="btn" style="font-size:11px;width:100%;text-align:center">← Clash-Liste</a></div>'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;color:#8ab;text-transform:uppercase;letter-spacing:.7px">Modelle</div>'
        f'<div class="model-card"><label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">'
        f'<input type="checkbox" class="chk-model" data-slot="{slot_a}" checked style="accent-color:{col_a};width:13px;height:13px">'
        f'<span style="width:10px;height:10px;border-radius:50%;border:2px solid {col_a};flex-shrink:0;display:inline-block"></span>'
        f'{_e(label_a)}</label></div>'
        f'<div class="model-card"><label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">'
        f'<input type="checkbox" class="chk-model" data-slot="{slot_b}" checked style="accent-color:{col_b};width:13px;height:13px">'
        f'<span style="width:10px;height:10px;border-radius:50%;border:2px solid {col_b};flex-shrink:0;display:inline-block"></span>'
        f'{_e(label_b)}</label></div>'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;color:#8ab;text-transform:uppercase;letter-spacing:.7px;margin-top:4px">Clash-Elemente</div>'
        f'<div style="padding:8px 10px;font-size:11px">'
        f'<div style="margin-bottom:8px"><span class="tag tag-1">A</span>'
        f'<span style="font-size:10px;color:var(--muted);margin-left:4px;font-family:monospace;word-break:break-all">{_e(gid1)}</span></div>'
        f'<div><span class="tag tag-2">B</span>'
        f'<span style="font-size:10px;color:var(--muted);margin-left:4px;font-family:monospace;word-break:break-all">{_e(gid2)}</span></div>'
        '</div>'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;color:#8ab;text-transform:uppercase;letter-spacing:.7px;margin-top:4px">Kategorien</div>'
        '<div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0">'
        '<div style="padding:8px 10px;font-size:11px;color:var(--muted);font-style:italic">Wird geladen …</div>'
        '</div>'
        '</div>'  # Ende Sidebar

        # Canvas
        '<div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden">'
        '<canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>'
        '<div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;'
        'align-items:center;justify-content:center;background:rgba(14,14,26,.93);z-index:20">'
        '<div style="width:40px;height:40px;border:4px solid #0f3460;border-top-color:var(--accent2);'
        'border-radius:50%;animation:spin .7s linear infinite;margin-bottom:12px"></div>'
        '<p id="load-txt" style="color:#889;font-size:13px">Clash-Elemente werden geladen …</p>'
        '</div>'
        '</div>'  # Ende Canvas

        # Info-Panel
        '<div id="info-panel" style="width:280px;min-width:280px;background:var(--surface);'
        'border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">'
        '<div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;color:#8ab;'
        'text-transform:uppercase;letter-spacing:.7px;display:flex;align-items:center;justify-content:space-between;flex-shrink:0">'
        '<span>Element-Info</span><span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px">✕</span>'
        '</div>'
        '<div id="info-body" style="flex:1;overflow-y:auto;padding:10px;font-size:12px">'
        '<div style="color:var(--muted);font-style:italic">Klick auf ein Element für Details.</div>'
        '</div>'
        '</div>'  # Ende Info-Panel

        + '</div>'  # Ende Haupt-Flex

        + '<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>'
        + f'<script>{viewer_js_code}</script>'
    )

    return _page_no_footer(f"Clash-Detail – {project.get('project_name','')} – BIMPruef", body)


# ── BCF – alle Clashes ────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/clash/bcf")
def project_model_clash_bcf(
    request: Request,
    project_id: str,
    session_id:  str  = Query(...),
    slot_a:      int  = Query(...),
    slot_b:      int  = Query(...),
    tolerance:   float = Query(default=0.0),
):
    account = _account_from_request(request)
    try:
        clashes = load_clash_cache(session_id, tolerance, slot_a=slot_a, slot_b=slot_b)
        if clashes is None:
            from app.clash import get_group_elements, compare_element_groups_for_clashes
            ga = get_group_elements(session_id, [slot_a], [])
            gb = get_group_elements(session_id, [slot_b], [])
            clashes = compare_element_groups_for_clashes(ga, gb, tolerance=tolerance)
            save_clash_cache(session_id, tolerance, clashes, slot_a=slot_a, slot_b=slot_b)
        from app.bcf_export import create_bcf_zip_from_clashes
        data = create_bcf_zip_from_clashes(clashes[:BCF_CLASH_LIMIT])
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="ifc_clashes.bcfzip"'},
        )
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)


# ── BCF – einzelner Clash ─────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/clash/bcf-single")
def project_model_clash_bcf_single(
    request: Request,
    project_id:  str,
    session_id:  str  = Query(...),
    slot_a:      int  = Query(...),
    slot_b:      int  = Query(...),
    clash_index: int  = Query(...),
    tolerance:   float = Query(default=0.0),
):
    account = _account_from_request(request)
    try:
        clashes = load_clash_cache(session_id, tolerance, slot_a=slot_a, slot_b=slot_b)
        if clashes is None:
            from app.clash import get_group_elements, compare_element_groups_for_clashes
            ga = get_group_elements(session_id, [slot_a], [])
            gb = get_group_elements(session_id, [slot_b], [])
            clashes = compare_element_groups_for_clashes(ga, gb, tolerance=tolerance)
            save_clash_cache(session_id, tolerance, clashes, slot_a=slot_a, slot_b=slot_b)
        if clash_index < 0 or clash_index >= len(clashes):
            return Response(content="Clash-Index außerhalb des Bereichs.", status_code=404)
        from app.bcf_export import create_bcf_zip_from_clashes
        data = create_bcf_zip_from_clashes([clashes[clash_index]])
        fname = f"clash_{clash_index + 1:04d}.bcfzip"
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# ── ELEMENTLISTE (integriert) ────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/list", response_class=HTMLResponse)
def project_model_list(
    request: Request,
    project_id: str,
    session_id: str = Query(default=""),
):
    """
    Elementliste innerhalb der Projekt-Shell.
    Die eigentliche Daten-API (/viewer/list/data/) und der Excel-Export
    (/viewer/list/export/) werden von list_module.py bedient; dieses
    Template bindet sie per JavaScript ein.
    """
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    if not session_id:
        session_id = get_or_create_project_session(account_id, project_id)

    pid = _e(project_id)
    sid = _e(session_id)

    slots = get_session_slots(session_id)
    slot_checkboxes = "".join(
        f'<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
        f'<input type="checkbox" class="slot-chk" value="{s}" checked '
        f'style="accent-color:var(--accent)"> {_e(get_ifc_label(session_id, s))}</label>'
        for s in slots
    )

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "model")}'
        f'{_model_subnav(project_id, session_id, "list")}'
        '<div style="display:flex;height:calc(100vh - 115px);overflow:hidden">'

        # Linke Sidebar
        '<div style="width:240px;min-width:240px;background:var(--surface);'
        'border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Modelle</div>'
        f'<div style="padding:8px 10px;display:flex;flex-direction:column;gap:4px">{slot_checkboxes}</div>'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px;margin-top:4px">Filter</div>'
        '<div id="filter-area" style="padding:8px 10px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:6px"></div>'
        '<div style="padding:8px 10px;border-top:1px solid var(--border)">'
        '<button id="btn-add-filter" class="btn" style="width:100%;font-size:11px">+ Filter hinzufügen</button>'
        '</div>'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Spalten</div>'
        '<div id="col-list" style="padding:6px 10px;overflow-y:auto;flex:1;max-height:200px"></div>'
        '<div style="padding:6px 10px;display:flex;gap:4px">'
        '<button id="btn-cols-all" class="btn" style="font-size:10px;flex:1">Alle</button>'
        '<button id="btn-cols-none" class="btn" style="font-size:10px;flex:1">Keine</button>'
        '</div>'
        '</div>'  # Ende Sidebar

        # Hauptbereich
        '<div style="flex:1;display:flex;flex-direction:column;overflow:hidden">'
        '<div style="padding:8px 14px;background:var(--surface);border-bottom:1px solid var(--border);'
        'display:flex;align-items:center;gap:10px;flex-shrink:0">'
        '<button id="btn-run" class="btn btn-primary" style="font-size:12px">▶ Anwenden</button>'
        '<a id="btn-export" class="btn" style="font-size:12px;text-decoration:none">⬇ Excel</a>'
        '<span id="status-total" style="font-size:12px;color:var(--muted)"></span>'
        '<span id="status-filtered" style="font-size:12px;color:var(--accent)"></span>'
        '<span id="status-cols" style="font-size:12px;color:var(--muted);margin-left:auto"></span>'
        '</div>'
        '<div style="flex:1;overflow:auto">'
        '<div id="table-placeholder" style="display:flex;align-items:center;justify-content:center;'
        'height:100%;color:var(--muted);font-size:13px;font-style:italic">'
        'Lade Daten …</div>'
        '<table id="result-table" style="display:none">'
        '<thead id="result-thead"></thead>'
        '<tbody id="result-tbody"></tbody>'
        '</table>'
        '</div>'
        '</div>'  # Ende Hauptbereich

        + '</div>'  # Ende Flex

        + f"""<script>
(function(){{
const SESSION_ID   = '{sid}';
const API_BASE     = '/viewer/list/data/';
const EXPORT_BASE  = '/viewer/list/export/';
const BASE_COLS    = [
  {{key:'file_label',label:'Datei'}},
  {{key:'slot',label:'Slot'}},
  {{key:'express_id',label:'Express-ID'}},
  {{key:'type',label:'IFC-Typ'}},
  {{key:'name',label:'Name'}},
  {{key:'global_id',label:'GlobalId'}},
  {{key:'object_type',label:'ObjectType'}},
  {{key:'predefined_type',label:'PredefinedType'}},
];
const OPS = ['contains','not_contains','equals','not_equals','starts_with','ends_with'];
let filters=[], selectedCols=[], psetKeys=[], colDefs=[], allElements=[];
const filterArea   = document.getElementById('filter-area');
const columnList   = document.getElementById('col-list');
const btnRun       = document.getElementById('btn-run');
const btnExport    = document.getElementById('btn-export');
const statusTotal  = document.getElementById('status-total');
const statusFiltered = document.getElementById('status-filtered');
const statusCols   = document.getElementById('status-cols');
const tablePlaceholder = document.getElementById('table-placeholder');
const resultTable  = document.getElementById('result-table');
const resultThead  = document.getElementById('result-thead');
const resultTbody  = document.getElementById('result-tbody');

function esc(s){{return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}}

function buildFieldOptions(extra=[]){{
  let o='<option value="">-- Feld --</option>';
  BASE_COLS.forEach(c=>{{o+=`<option value="${{esc(c.key)}}">${{esc(c.label)}}</option>`;}});
  if(extra.length){{o+='<option disabled>── Eigenschaften ──</option>';extra.forEach(k=>{{o+=`<option value="${{esc(k)}}">${{esc(k.startsWith("pset:")?k.slice(5):k)}}</option>`;}});}}
  return o;
}}

function addFilter(f='type',o='contains',v=''){{
  const row=document.createElement('div');
  row.style.cssText='display:flex;flex-direction:column;gap:3px;padding:6px;background:var(--surface2);border:1px solid var(--border);border-radius:6px';
  row.innerHTML=`
    <div style="display:flex;gap:4px;align-items:center">
      <select class="filter-field" style="flex:1;font-size:11px;padding:3px">${{buildFieldOptions(psetKeys)}}</select>
      <button class="filter-del btn" style="font-size:10px;padding:2px 6px;color:var(--accent2);border-color:var(--accent2)">✕</button>
    </div>
    <select class="filter-op" style="font-size:11px;padding:3px">${{OPS.map(op=>`<option value="${{op}}">${{op}}</option>`).join('')}}</select>
    <input class="filter-val" type="text" value="${{esc(v)}}" placeholder="Wert …" style="font-size:11px;padding:3px">
  `;
  row.querySelector('.filter-field').value=f;
  row.querySelector('.filter-op').value=o;
  row.querySelector('.filter-del').addEventListener('click',()=>{{row.remove();syncFilters();}});
  filterArea.appendChild(row);
  syncFilters();
}}

function syncFilters(){{
  filters=[];
  filterArea.querySelectorAll(':scope > div').forEach(row=>{{
    const f=row.querySelector('.filter-field')?.value||'';
    const o=row.querySelector('.filter-op')?.value||'contains';
    const v=row.querySelector('.filter-val')?.value||'';
    filters.push({{field:f,operator:o,value:v}});
  }});
}}

function buildColumnUI(){{
  colDefs=[...BASE_COLS];
  psetKeys.forEach(k=>{{const lbl=k.startsWith("pset:")?k.slice(5):k;colDefs.push({{key:k,label:lbl}});}});
  columnList.innerHTML='';
  const baseSection=document.createElement('div');
  baseSection.style.cssText='margin-bottom:6px';
  baseSection.innerHTML=`<div style="font-size:10px;color:var(--muted);margin-bottom:3px;text-transform:uppercase">Basis</div>`;
  BASE_COLS.forEach(col=>{{baseSection.appendChild(makeColRow(col));}});
  columnList.appendChild(baseSection);
  if(psetKeys.length){{
    const ps=document.createElement('div');
    ps.innerHTML=`<div style="font-size:10px;color:var(--muted);margin-bottom:3px;text-transform:uppercase">Eigenschaften</div>`;
    psetKeys.forEach(k=>{{const lbl=k.startsWith("pset:")?k.slice(5):k;ps.appendChild(makeColRow({{key:k,label:lbl}}));}});
    columnList.appendChild(ps);
  }}
  syncColumns();
}}

function makeColRow(col){{
  const div=document.createElement('label');
  div.style.cssText='display:flex;align-items:center;gap:5px;cursor:pointer;padding:2px 3px;border-radius:4px;font-size:11px';
  div.innerHTML=`<input type="checkbox" class="col-chk" value="${{esc(col.key)}}" checked style="accent-color:var(--accent);width:11px;height:11px;flex-shrink:0"><span style="color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{esc(col.label)}}">${{esc(col.label)}}</span>`;
  div.addEventListener('mouseenter',()=>div.style.background='rgba(79,195,247,.06)');
  div.addEventListener('mouseleave',()=>div.style.background='');
  div.querySelector('.col-chk').addEventListener('change',syncColumns);
  return div;
}}

function syncColumns(){{
  selectedCols=[...document.querySelectorAll('.col-chk:checked')].map(c=>c.value);
  statusCols.textContent=selectedCols.length+' Spalten';
}}

async function loadData(){{
  syncFilters();syncColumns();
  const slots=[...document.querySelectorAll('.slot-chk:checked')].map(c=>c.value);
  btnRun.disabled=true;btnRun.textContent='⏳ …';
  const params=new URLSearchParams({{session_id:SESSION_ID,slots:slots.join(','),filters_json:JSON.stringify(filters),columns_json:JSON.stringify(selectedCols)}});
  try{{
    const resp=await fetch(API_BASE+'?'+params.toString());
    const data=await resp.json();
    if(data.error)throw new Error(data.error);
    allElements=data.rows;
    if(!psetKeys.length&&data.pset_keys.length){{psetKeys=data.pset_keys;buildColumnUI();document.querySelectorAll('.filter-field').forEach(sel=>{{const cur=sel.value;sel.innerHTML=buildFieldOptions(psetKeys);sel.value=cur;}});}}
    statusTotal.textContent=`Gesamt: ${{data.total}} Elemente`;
    statusFiltered.textContent=data.filtered<data.total?`Gefiltert: ${{data.filtered}}`:'';
    syncColumns();renderTable(data.rows);
  }}catch(e){{statusTotal.textContent='Fehler: '+e.message;tablePlaceholder.style.display='flex';tablePlaceholder.innerHTML=`<span style="color:var(--accent2)">⚠ ${{esc(e.message)}}</span>`;resultTable.style.display='none';}}
  finally{{btnRun.disabled=false;btnRun.textContent='▶ Anwenden';}}
}}

function renderTable(rows){{
  const activeCols=colDefs.filter(c=>selectedCols.includes(c.key));
  if(!activeCols.length){{tablePlaceholder.style.display='flex';tablePlaceholder.innerHTML='<span>Bitte mindestens eine Spalte wählen.</span>';resultTable.style.display='none';return;}}
  const visibleCols=activeCols.filter(c=>rows.some(row=>{{const v=row[c.key];return v!==undefined&&v!==null&&String(v).trim()!=='';}}));
  if(!visibleCols.length){{tablePlaceholder.style.display='flex';tablePlaceholder.innerHTML='<span>Keine Werte für gewählte Spalten.</span>';resultTable.style.display='none';return;}}
  let thead='<tr><th style="min-width:40px;width:40px">#</th>';
  visibleCols.forEach(c=>{{thead+=`<th style="min-width:80px">${{esc(c.label)}}</th>`;}});
  thead+='</tr>';resultThead.innerHTML=thead;
  const MAX=2000;let tbody='';
  rows.slice(0,MAX).forEach((row,idx)=>{{tbody+=`<tr><td style="color:var(--muted);text-align:right">${{idx+1}}</td>`;visibleCols.forEach(c=>{{const v=row[c.key]!==undefined?row[c.key]:'';tbody+=`<td title="${{esc(String(v))}}">${{esc(String(v))}}</td>`;}});tbody+='</tr>';}});
  if(rows.length>MAX)tbody+=`<tr><td colspan="${{visibleCols.length+1}}" style="text-align:center;color:var(--muted);font-style:italic;padding:10px">… ${{rows.length-MAX}} weitere Zeilen</td></tr>`;
  resultTbody.innerHTML=tbody;tablePlaceholder.style.display='none';resultTable.style.display='';
}}

function doExport(){{
  syncFilters();syncColumns();
  const slots=[...document.querySelectorAll('.slot-chk:checked')].map(c=>c.value);
  const params=new URLSearchParams({{session_id:SESSION_ID,slots:slots.join(','),filters_json:JSON.stringify(filters),columns_json:JSON.stringify(selectedCols)}});
  window.location.href=EXPORT_BASE+'?'+params.toString();
}}

document.getElementById('btn-cols-all').addEventListener('click',()=>{{document.querySelectorAll('.col-chk').forEach(c=>{{c.checked=true;}});syncColumns();}});
document.getElementById('btn-cols-none').addEventListener('click',()=>{{document.querySelectorAll('.col-chk').forEach(c=>{{c.checked=false;}});syncColumns();}});
document.getElementById('btn-add-filter').addEventListener('click',()=>addFilter('type','contains',''));
btnRun.addEventListener('click',loadData);
btnExport.addEventListener('click',doExport);
document.addEventListener('keydown',e=>{{if(e.key==='Enter'&&e.target.classList.contains('filter-val'))loadData();}});
buildColumnUI();
loadData();
}})();
</script>"""
    )

    return _page(f"Elementliste – {project.get('project_name','')} – BIMPruef", body)


# Elementlisten-Daten-API: delegiert direkt an list_module (AJAX-Endpunkt bleibt /viewer/list/data/)
# Elementlisten-Excel-Export: delegiert direkt an list_module (/viewer/list/export/)
# (Diese beiden Endpunkte sind technische APIs und werden von JS aufgerufen – kein eigener
#  Projekt-Endpunkt nötig, da die session_id den Projektkontext vollständig identifiziert.)


# ─────────────────────────────────────────────────────────────────────────────
# ── RULE-CHECK (integriert) ──────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model/rulecheck", response_class=HTMLResponse)
def project_model_rulecheck(
    request: Request,
    project_id: str,
    session_id: str = Query(default=""),
):
    """
    Rule-Check-Seite innerhalb der Projekt-Shell.
    Die eigentliche Prüfung (/viewer/rulecheck/run/) und der Export
    (/viewer/rulecheck/export/) werden von rulecheck.py bedient.
    """
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    if not session_id:
        session_id = get_or_create_project_session(account_id, project_id)

    pid = _e(project_id)
    sid = _e(session_id)

    slots = get_session_slots(session_id)

    from app.rulecheck import ALL_RULES

    slot_checkboxes = "".join(
        f'<label style="display:flex;align-items:center;gap:5px;font-size:12px;cursor:pointer">'
        f'<input type="checkbox" class="slot-chk" value="{s}" checked '
        f'style="accent-color:var(--accent)"> {_e(get_ifc_label(session_id, s))}</label>'
        for s in slots
    )

    SEV_COLOR = {"error": "#ef9a9a", "warning": "#ffb74d", "info": "#64b5f6"}

    rule_checkboxes = "".join(
        f'<label style="display:flex;align-items:flex-start;gap:6px;cursor:pointer;'
        f'padding:5px 0;border-bottom:1px solid var(--border)">'
        f'<input type="checkbox" class="rule-chk" value="{_e(r["id"])}" checked '
        f'style="accent-color:var(--accent);margin-top:2px">'
        f'<div>'
        f'<div style="font-size:12px">{_e(r["label"])}</div>'
        f'<div style="font-size:10px;color:{SEV_COLOR.get(r["severity"],"var(--muted)")}">{_e(r["severity"])}</div>'
        f'</div>'
        f'</label>'
        for r in ALL_RULES
    )

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "model")}'
        f'{_model_subnav(project_id, session_id, "rulecheck")}'
        '<div style="display:flex;height:calc(100vh - 115px);overflow:hidden">'

        # Sidebar
        '<div style="width:240px;min-width:240px;background:var(--surface);'
        'border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px">Modelle</div>'
        f'<div style="padding:8px 10px;display:flex;flex-direction:column;gap:4px">{slot_checkboxes}</div>'
        '<div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;'
        'color:#8ab;text-transform:uppercase;letter-spacing:.7px;margin-top:4px">Regeln</div>'
        f'<div style="padding:6px 10px;overflow-y:auto;flex:1">{rule_checkboxes}</div>'
        '<div style="padding:8px 10px;border-top:1px solid var(--border)">'
        '<button id="btn-run" class="btn btn-primary" style="width:100%;font-size:12px">▶ Prüfung starten</button>'
        '<span id="run-status" style="font-size:11px;color:var(--muted);display:block;margin-top:6px;text-align:center"></span>'
        '</div>'
        '</div>'  # Ende Sidebar

        # Ergebnisbereich
        '<div style="flex:1;overflow-y:auto;padding:16px 20px">'
        '<div id="result-summary"></div>'
        '<div id="result-area" style="display:none;margin-top:14px">'
        '<div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">'
        '<button class="btn" onclick="filterSev(\'all\')" id="sev-all">Alle</button>'
        '<button class="btn" onclick="filterSev(\'error\')" style="color:#ef9a9a;border-color:#6e1a2e">Fehler</button>'
        '<button class="btn" onclick="filterSev(\'warning\')" style="color:#ffb74d;border-color:#6e3a1e">Warnungen</button>'
        '<button class="btn" onclick="filterSev(\'info\')" style="color:#64b5f6;border-color:#1a4a6e">Hinweise</button>'
        '<a id="btn-export" class="btn" style="display:none;text-decoration:none;margin-left:auto">⬇ JSON-Export</a>'
        '</div>'
        '<div id="results-table-wrap" style="overflow-x:auto"><table>'
        '<thead><tr><th>Schwere</th><th>Regel</th><th>Datei</th><th>Typ</th>'
        '<th>Name</th><th>GlobalId</th><th>Meldung</th></tr></thead>'
        '<tbody id="results-tbody"></tbody>'
        '</table></div>'
        '</div>'
        '<div id="empty-hint" style="color:var(--muted);font-size:13px;font-style:italic;padding:20px 0">'
        'Wähle Modelle und Regeln aus und starte die Prüfung.'
        '</div>'
        '</div>'  # Ende Ergebnisbereich

        + '</div>'  # Ende Flex

        + f"""<script>
(function(){{
const SESSION_ID='{sid}';
let _allResults=[];

function esc(s){{return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");}}

const SEV_COLOR={{error:'#ef9a9a',warning:'#ffb74d',info:'#64b5f6'}};
const SEV_BG   ={{error:'#2a0a10',warning:'#3e2800',info:'#0d2a3e'}};

function filterSev(sev){{
  const rows=_allResults.filter(r=>sev==='all'||r.severity===sev);
  const tbody=document.getElementById('results-tbody');
  tbody.innerHTML=rows.map((r,i)=>`
    <tr>
      <td><span style="color:${{SEV_COLOR[r.severity]||'var(--text)'}};font-weight:600;font-size:11px">${{esc(r.severity)}}</span></td>
      <td style="font-size:11px">${{esc(r.rule_id)}}</td>
      <td style="font-size:11px">${{esc(r.file_label)}}</td>
      <td style="font-size:11px">${{esc(r.ifc_type)}}</td>
      <td style="font-size:11px">${{esc(r.name)}}</td>
      <td style="font-family:monospace;font-size:10px">${{esc(r.global_id)}}</td>
      <td style="font-size:11px">${{esc(r.message)}}</td>
    </tr>`).join('');
}}

document.getElementById('btn-run').addEventListener('click', async ()=>{{
  const slots=[...document.querySelectorAll('.slot-chk:checked')].map(c=>parseInt(c.value));
  const rules=[...document.querySelectorAll('.rule-chk:checked')].map(c=>c.value);
  const btn=document.getElementById('btn-run');
  const status=document.getElementById('run-status');
  if(!slots.length){{alert('Bitte mindestens ein Modell auswählen.');return;}}
  if(!rules.length){{alert('Bitte mindestens eine Regel auswählen.');return;}}
  btn.disabled=true;status.textContent='⏳ Prüfung läuft …';
  document.getElementById('empty-hint').style.display='none';
  try{{
    const resp=await fetch('/viewer/rulecheck/run/',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{session_id:SESSION_ID,slots,rules}})
    }});
    const data=await resp.json();
    if(!resp.ok||data.error){{alert('Fehler: '+(data.error||resp.status));btn.disabled=false;return;}}
    _allResults=data.results||[];
    const counts=data.summary||{{}};
    document.getElementById('result-summary').innerHTML=`
      <h2 style="font-size:15px;margin-bottom:12px">Ergebnis-Zusammenfassung</h2>
      <div style="display:flex;gap:12px;flex-wrap:wrap">
        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 18px;text-align:center">
          <div style="font-size:22px;font-weight:700">${{counts.total??0}}</div>
          <div style="font-size:11px;color:var(--muted)">Gesamt</div>
        </div>
        <div style="background:#2a0a10;border:1px solid #6e1a2e;border-radius:8px;padding:10px 18px;text-align:center">
          <div style="font-size:22px;font-weight:700;color:#ef9a9a">${{counts.errors??0}}</div>
          <div style="font-size:11px;color:var(--muted)">Fehler</div>
        </div>
        <div style="background:#3e2800;border:1px solid #6e4800;border-radius:8px;padding:10px 18px;text-align:center">
          <div style="font-size:22px;font-weight:700;color:#ffb74d">${{counts.warnings??0}}</div>
          <div style="font-size:11px;color:var(--muted)">Warnungen</div>
        </div>
        <div style="background:#0d2a3e;border:1px solid #1a4a6e;border-radius:8px;padding:10px 18px;text-align:center">
          <div style="font-size:22px;font-weight:700;color:#64b5f6">${{counts.infos??0}}</div>
          <div style="font-size:11px;color:var(--muted)">Hinweise</div>
        </div>
      </div>`;
    const exportParams=new URLSearchParams({{session_id:SESSION_ID,slots:slots.join(','),rules:rules.join(',')}});
    const exportBtn=document.getElementById('btn-export');
    exportBtn.href='/viewer/rulecheck/export/?'+exportParams.toString();
    exportBtn.style.display='';
    document.getElementById('result-area').style.display='';
    filterSev('all');
    status.textContent=`Fertig – ${{_allResults.length}} Befunde.`;
  }}catch(err){{alert('Netzwerkfehler: '+err.message);status.textContent='';}}
  finally{{btn.disabled=false;}}
}});
}})();
</script>"""
    )

    return _page(f"Rule-Check – {project.get('project_name','')} – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# ── PLATZHALTER-MODULE ───────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/documents", response_class=HTMLResponse)
def project_documents(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "documents")


@projects_router.get("/projects/{project_id}/issues", response_class=HTMLResponse)
def project_issues(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "issues")


@projects_router.get("/projects/{project_id}/todo", response_class=HTMLResponse)
def project_todo(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "todo")


@projects_router.get("/projects/{project_id}/checking", response_class=HTMLResponse)
def project_checking(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "checking")


# ─────────────────────────────────────────────────────────────────────────────
# Projekt-Einstellungen
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: str, saved: str = ""):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pid = _e(project_id)
    ok_html = '<div class="flash-ok">✓ Einstellungen gespeichert.</div>' if saved == "1" else ""

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "settings")}'
        '<div style="padding:28px 32px;max-width:600px">'
        f'{ok_html}'
        '<div class="card">'
        '<h2 style="font-size:16px;margin-bottom:16px">Projekteinstellungen</h2>'
        f'<form method="POST" action="/projects/{pid}/settings">'
        '<label>Projektcode</label>'
        f'<input type="text" name="project_code" value="{_e(project["project_code"])}" required maxlength="40">'
        '<label>Projektname</label>'
        f'<input type="text" name="project_name" value="{_e(project["project_name"])}" required maxlength="120">'
        '<label>Beschreibung</label>'
        f'<textarea name="description" rows="3" style="resize:vertical">{_e(project.get("description",""))}</textarea>'
        '<label>Status</label>'
        f'<select name="status">'
        f'<option value="active" {"selected" if project.get("status")=="active" else ""}>active</option>'
        f'<option value="inactive" {"selected" if project.get("status")=="inactive" else ""}>inactive</option>'
        '</select>'
        '<div style="margin-top:20px;display:flex;gap:10px">'
        '<button type="submit" class="btn btn-primary">Speichern</button>'
        f'<a href="/projects/{pid}" class="btn">Abbrechen</a>'
        '</div>'
        '</form>'
        '</div>'

        # Projekt löschen
        '<div class="card" style="border-color:var(--accent2)">'
        '<h3 style="font-size:14px;color:var(--accent2);margin-bottom:10px">Gefahrenzone</h3>'
        '<p style="font-size:13px;color:var(--muted);margin-bottom:12px">'
        'Das Projekt und alle zugehörigen Daten werden unwiderruflich gelöscht.'
        '</p>'
        f'<form method="POST" action="/projects/{pid}/delete" '
        'onsubmit="return confirm(\'Projekt wirklich löschen?\');">'
        '<button type="submit" class="btn btn-danger">Projekt löschen</button>'
        '</form>'
        '</div>'

        '</div>'
    )
    return _page(f"Einstellungen – {project['project_name']}", body)


@projects_router.post("/projects/{project_id}/settings")
async def project_settings_save(
    request: Request,
    project_id:   str,
    project_code: str = Form(...),
    project_name: str = Form(...),
    description:  str = Form(default=""),
    status:       str = Form(default="active"),
):
    account = _account_from_request(request)
    update_project(
        account["account_id"], project_id,
        project_code=project_code,
        project_name=project_name,
        description=description,
        status=status,
    )
    return RedirectResponse(f"/projects/{project_id}/settings?saved=1", status_code=303)


@projects_router.post("/projects/{project_id}/delete")
async def project_delete(request: Request, project_id: str):
    account = _account_from_request(request)
    delete_project(account["account_id"], project_id)
    return RedirectResponse("/", status_code=303)
