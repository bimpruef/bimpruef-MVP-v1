"""
projects.py – BIMPruef Projekt-Routen

Routen:
  GET  /                           → Account-/Projektübersicht
  GET  /projects/new               → Formular: neues Projekt anlegen
  POST /projects/create            → Projekt erstellen
  GET  /projects/{project_id}      → Projekt-Dashboard
  GET  /projects/{project_id}/model → integrierter Model-/Viewer-Bereich
  GET  /projects/{project_id}/model/clash → integrierte Clash-Analyse
  GET  /projects/{project_id}/model/list → integrierte Elementliste
  GET  /projects/{project_id}/model/rulecheck → integrierter Rule-Check
  GET  /projects/{project_id}/documents  → Platzhalter
  GET  /projects/{project_id}/issues     → Platzhalter
  GET  /projects/{project_id}/todo       → Platzhalter
  GET  /projects/{project_id}/checking   → Platzhalter
  GET  /projects/{project_id}/settings   → Projekt-Einstellungen
"""

import html
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import require_user

from app.project_storage import (
    list_projects,
    create_project,
    get_project,
    update_project,
    delete_project,
    get_or_create_project_session,
    get_project_model_count,
)

projects_router = APIRouter()


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
</style>
"""


def _e(s) -> str:
    return html.escape(str(s or ""))


def _brand_logo(height_px: int = 28) -> str:
    icon_size = max(27, int(height_px * 1.28))
    text_size = max(18, int(height_px * 0.92))
    return f"""<div style="display:flex;align-items:center;gap:8px;line-height:1">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 54" role="img" style="height:{icon_size}px;width:auto;display:block">
    <g fill="none" stroke-linejoin="round" stroke-linecap="round" stroke-width="2.8">
      <g stroke="#1f5f9f"><path d="M8 18 24 8l16 10-16 10z"/><path d="M8 18v18l16 10V28z"/><path d="M40 18v18L24 46V28z"/></g>
      <g stroke="#d8192f"><path d="M29 18 45 8l16 10-16 10z"/><path d="M29 18v18l16 10V28z"/><path d="M61 18v18L45 46V28z"/></g>
      <g stroke="#8f4399"><path d="M50 18 66 8l16 10-16 10z"/><path d="M50 18v18l16 10V28z"/><path d="M82 18v18L66 46V28z"/></g>
      <g stroke="#27a6ad"><path d="M71 18 87 8l16 10-16 10z"/><path d="M71 18v18l16 10V28z"/><path d="M103 18v18L87 46V28z"/></g>
    </g>
  </svg>
  <span style="font-family:'Avenir Next','Montserrat','Segoe UI',sans-serif;font-weight:300;letter-spacing:1.2px;color:var(--text);font-size:{text_size}px;white-space:nowrap;text-transform:uppercase">BIMPRUEF</span>
</div>"""


def _topbar_global(account: dict) -> str:
    return (
        f'<div style="display:flex;align-items:center;gap:12px;padding:8px 20px;'
        f'background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">'
        f'{_brand_logo(22)}'
        f'<span style="color:var(--muted);font-size:12px">|</span>'
        f'<span style="font-size:12px;color:var(--muted)">Account:</span>'
        f'<span style="font-size:13px;font-weight:600;color:var(--accent)">{_e(account["account_name"])}</span>'
        f'<span style="color:var(--muted);font-size:12px">·</span>'
        f'<span style="font-size:12px;color:var(--muted)">Workspace: {_e(account["workspace"])}</span>'
        f'<div style="margin-left:auto;display:flex;gap:8px">'
        f'<a href="/impressum" style="font-size:11px;color:var(--muted)">Impressum</a>'
        f'<a href="/datenschutz" style="font-size:11px;color:var(--muted)">Datenschutz</a>'
        f'<form method="POST" action="/auth/logout" style="margin:0">'
        f'<button type="submit" class="btn" style="font-size:11px;padding:3px 9px">Logout</button>'
        f'</form>'
        f'</div>'
        f'</div>'
    )


def _project_subnav(project_id: str, active: str) -> str:
    pid = _e(project_id)
    items = [
        ("dashboard",  f"/projects/{pid}",             "📊 Dashboard"),
        ("model",      f"/projects/{pid}/model",        "🏗 Model"),
        ("documents",  f"/projects/{pid}/documents",    "📄 Documents"),
        ("issues",     f"/projects/{pid}/issues",       "🐛 Issues"),
        ("todo",       f"/projects/{pid}/todo",         "☑ To-do"),
        ("checking",   f"/projects/{pid}/checking",     "✅ Checking"),
        ("settings",   f"/projects/{pid}/settings",     "⚙ Settings"),
    ]
    parts = []
    for key, href, label in items:
        if key == active:
            style = ("padding:8px 14px;font-size:13px;border-radius:6px;"
                     "color:var(--accent);border-bottom:2px solid var(--accent);text-decoration:none")
        else:
            style = ("padding:8px 14px;font-size:13px;border-radius:6px;"
                     "color:var(--text);text-decoration:none")
        parts.append(f'<a href="{href}" style="{style}">{label}</a>')
    return (
        '<div style="display:flex;align-items:center;gap:2px;padding:4px 16px;'
        'background:#0d1a30;border-bottom:1px solid var(--border)">'
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


def _placeholder_page(project: dict, account: dict, module: str) -> HTMLResponse:
    pid   = project["project_id"]
    pname = project["project_name"]
    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(pid, module)}'
        '<div style="padding:40px 32px">'
        f'<h2 style="font-size:18px;margin-bottom:8px">{_e(module.capitalize())}</h2>'
        f'<p style="color:var(--muted)">Dieses Modul ist noch nicht implementiert.</p>'
        f'<p style="margin-top:20px"><a class="btn" href="/projects/{_e(pid)}">← Dashboard</a></p>'
        '</div>'
    )
    return _page(f"{pname} – {module}", body)




def _account_from_request(request: Request) -> dict:
    user = require_user(request)
    return {
        "account_id": user["user_id"],
        "account_name": user["email"],
        "workspace": "Personal",
        "created_at": user.get("created_at", ""),
    }

# ─────────────────────────────────────────────────────────────────────────────
# Root → Projektübersicht
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/", response_class=HTMLResponse)
def projects_home(request: Request):
    account  = _account_from_request(request)
    account_id = account["account_id"]
    projects = list_projects(account_id)

    rows = ""
    for p in projects:
        pid    = _e(p["project_id"])
        badge  = (f'<span class="badge badge-active">active</span>'
                  if p.get("status") == "active"
                  else f'<span class="badge badge-inactive">{_e(p.get("status",""))}</span>')
        model_count = get_project_model_count(account_id, p["project_id"])
        rows += (
            f"<tr>"
            f"<td style='font-weight:600;color:var(--accent)'>{_e(p.get('project_code',''))}</td>"
            f"<td>{_e(p.get('project_name',''))}</td>"
            f"<td style='color:var(--muted);font-size:12px'>{_e(p.get('description',''))}</td>"
            f"<td>{badge}</td>"
            f"<td style='text-align:center'>{model_count}</td>"
            f"<td style='font-size:12px;color:var(--muted)'>{_e(p.get('created_at','')[:10])}</td>"
            f"<td>"
            f"<a href='/projects/{pid}' class='btn btn-primary' "
            f"style='font-size:12px;padding:4px 12px;text-decoration:none'>Öffnen</a>"
            f"</td>"
            f"</tr>"
        )

    empty_hint = ""
    if not projects:
        empty_hint = (
            '<div class="flash-ok" style="margin-top:20px">'
            '💡 Noch keine Projekte. Erstelle dein erstes Projekt mit dem Button oben.'
            '</div>'
        )

    body = (
        f'{_topbar_global(account)}'
        '<div style="padding:32px;max-width:1100px;margin:0 auto">'
        '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">'
        '<div>'
        '<h1 style="font-size:22px;font-weight:600">Projekte</h1>'
        f'<p style="color:var(--muted);font-size:13px;margin-top:4px">Workspace: {_e(account["workspace"])}</p>'
        '</div>'
        '<a href="/projects/new" class="btn btn-primary" style="text-decoration:none;font-size:14px">+ Neues Projekt</a>'
        '</div>'
    )

    if projects:
        body += (
            '<div class="card" style="overflow-x:auto">'
            '<table>'
            '<tr><th>Code</th><th>Projektname</th><th>Beschreibung</th>'
            '<th>Status</th><th style="text-align:center">Modelle</th>'
            '<th>Erstellt</th><th></th></tr>'
            + rows +
            '</table>'
            '</div>'
        )
    body += empty_hint + '</div>'

    return _page("BIMPruef – Projekte", body)


# ─────────────────────────────────────────────────────────────────────────────
# Projekt erstellen
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/new", response_class=HTMLResponse)
def new_project_form(request: Request, error: str = ""):
    account = _account_from_request(request)
    err_html = f'<div class="flash-err">{_e(error)}</div>' if error else ""
    body = (
        f'{_topbar_global(account)}'
        '<div style="padding:32px;max-width:560px;margin:0 auto">'
        f'{err_html}'
        '<div class="card">'
        '<h2 style="font-size:18px;margin-bottom:20px">Neues Projekt</h2>'
        '<form method="POST" action="/projects/create">'
        '<label>Projektcode <span style="color:var(--accent2)">*</span></label>'
        '<input type="text" name="project_code" required placeholder="z. B. PRJ-001" maxlength="40">'
        '<label>Projektname <span style="color:var(--accent2)">*</span></label>'
        '<input type="text" name="project_name" required placeholder="z. B. Wohngebäude Musterstraße" maxlength="120">'
        '<label>Beschreibung (optional)</label>'
        '<textarea name="description" rows="3" placeholder="Kurze Beschreibung …" style="resize:vertical"></textarea>'
        '<div style="margin-top:20px;display:flex;gap:10px">'
        '<button type="submit" class="btn btn-primary">Projekt erstellen</button>'
        '<a href="/" class="btn" style="text-decoration:none">Abbrechen</a>'
        '</div>'
        '</form>'
        '</div>'
        '</div>'
    )
    return _page("Neues Projekt – BIMPruef", body)


@projects_router.post("/projects/create")
async def create_project_post(
    request: Request,
    project_code: str = Form(...),
    project_name: str = Form(...),
    description: str  = Form(default=""),
):
    if not project_code.strip():
        return RedirectResponse("/projects/new?error=Projektcode+darf+nicht+leer+sein", status_code=303)
    if not project_name.strip():
        return RedirectResponse("/projects/new?error=Projektname+darf+nicht+leer+sein", status_code=303)

    account = _account_from_request(request)
    p = create_project(account["account_id"], project_code, project_name, description)
    return RedirectResponse(f"/projects/{p['project_id']}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Projekt-Dashboard
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}", response_class=HTMLResponse)
def project_dashboard(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return _page("Nicht gefunden", '<div style="padding:40px">Projekt nicht gefunden. <a href="/">← Zurück</a></div>')

    pid    = _e(project_id)
    pname  = project["project_name"]
    pcode  = project["project_code"]
    status = project.get("status", "active")
    model_count = get_project_model_count(account_id, project_id)

    badge = (f'<span class="badge badge-active">active</span>'
             if status == "active"
             else f'<span class="badge badge-inactive">{_e(status)}</span>')

    # Session-ID für direkten Viewer-Link
    sid = get_or_create_project_session(account_id, project_id)

    stat_cards = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px">'
        f'<div class="card" style="text-align:center">'
        f'<div style="font-size:28px;font-weight:700;color:var(--accent)">{model_count}</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-top:4px">Modelle</div>'
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
        f'<h3 style="font-size:14px;color:var(--muted);margin-bottom:10px;text-transform:uppercase;letter-spacing:.5px">Schnellzugriff</h3>'
        f'<div style="display:flex;flex-wrap:wrap;gap:8px">'
        f'<a href="/projects/{pid}/model" class="btn btn-primary" style="text-decoration:none">🏗 Model öffnen</a>'
        f'<a href="/projects/{pid}/model/clash" class="btn" style="text-decoration:none">⚡ Clash-Analyse</a>'
        f'<a href="/projects/{pid}/model/list" class="btn" style="text-decoration:none">📋 Elementliste</a>'
        f'<a href="/projects/{pid}/model/rulecheck" class="btn" style="text-decoration:none">✅ Rule-Check</a>'
        f'</div>'
        f'</div>'
    )

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "dashboard")}'
        '<div style="padding:28px 32px;max-width:1000px">'
        f'<div style="display:flex;align-items:flex-start;justify-content:space-between;margin-bottom:4px">'
        f'<div>'
        f'<h1 style="font-size:22px;font-weight:600">{_e(pname)}</h1>'
        f'<p style="color:var(--muted);font-size:13px;margin-top:2px">{_e(pcode)} &nbsp;·&nbsp; {badge}</p>'
        f'</div>'
        f'<a href="/" class="btn" style="text-decoration:none;font-size:12px">← Alle Projekte</a>'
        f'</div>'
    )

    if project.get("description"):
        body += f'<p style="color:var(--muted);font-size:13px;margin-top:8px">{_e(project["description"])}</p>'

    body += stat_cards + quick_links + '</div>'

    return _page(f"{pname} – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# Model-Modul
# ─────────────────────────────────────────────────────────────────────────────

def _load_project_or_home(request: Request, project_id: str) -> tuple[dict, dict, str] | RedirectResponse:
    """Load project, account and the stable project upload-session."""
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    sid = get_or_create_project_session(account_id, project_id)
    return account, project, sid


@projects_router.get("/projects/{project_id}/model", response_class=HTMLResponse)
def project_model(request: Request, project_id: str, error: str = Query(default="")):
    """Integrated Model/Viewer area for an opened project."""
    loaded = _load_project_or_home(request, project_id)
    if isinstance(loaded, RedirectResponse):
        return loaded
    _account, _project, sid = loaded
    from app.viewer import viewer_main
    return viewer_main(request, session_id=sid, error=error, project_id=project_id)


@projects_router.get("/projects/{project_id}/model/clash", response_class=HTMLResponse)
def project_model_clash(
    request: Request,
    project_id: str,
    tolerance: float = Query(default=0.0),
):
    """Integrated Clash area for an opened project."""
    loaded = _load_project_or_home(request, project_id)
    if isinstance(loaded, RedirectResponse):
        return loaded
    _account, _project, sid = loaded
    from app.viewer import viewer_clash
    return viewer_clash(session_id=sid, tolerance=tolerance, project_id=project_id)


@projects_router.get("/projects/{project_id}/model/list", response_class=HTMLResponse)
def project_model_list(request: Request, project_id: str):
    """Integrated Element List area for an opened project."""
    loaded = _load_project_or_home(request, project_id)
    if isinstance(loaded, RedirectResponse):
        return loaded
    _account, _project, sid = loaded
    from app.list_module import viewer_list
    return viewer_list(session_id=sid, project_id=project_id)


@projects_router.get("/projects/{project_id}/model/rulecheck", response_class=HTMLResponse)
def project_model_rulecheck(request: Request, project_id: str):
    """Integrated Rule-Check area for an opened project."""
    loaded = _load_project_or_home(request, project_id)
    if isinstance(loaded, RedirectResponse):
        return loaded
    _account, _project, sid = loaded
    from app.rulecheck import viewer_rulecheck
    return viewer_rulecheck(session_id=sid, project_id=project_id)


# ─────────────────────────────────────────────────────────────────────────────
# Platzhalter-Module
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/documents", response_class=HTMLResponse)
def project_documents(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "documents")


@projects_router.get("/projects/{project_id}/issues", response_class=HTMLResponse)
def project_issues(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "issues")


@projects_router.get("/projects/{project_id}/todo", response_class=HTMLResponse)
def project_todo(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "todo")


@projects_router.get("/projects/{project_id}/checking", response_class=HTMLResponse)
def project_checking(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _placeholder_page(project, account, "checking")


@projects_router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(request: Request, project_id: str, saved: str = ""):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pid     = _e(project_id)
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
        f'<a href="/projects/{pid}" class="btn" style="text-decoration:none">Abbrechen</a>'
        '</div>'
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
    update_project(account["account_id"], project_id, project_code=project_code,
                   project_name=project_name, description=description, status=status)
    return RedirectResponse(f"/projects/{project_id}/settings?saved=1", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Projekt löschen (aus Settings heraus)
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.post("/projects/{project_id}/delete")
async def project_delete(request: Request, project_id: str):
    account = _account_from_request(request)
    delete_project(account["account_id"], project_id)
    return RedirectResponse("/", status_code=303)
