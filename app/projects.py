"""
projects.py – BIMPruef Projekt-Routen

Routen:
  GET  /                           → Account-/Projektübersicht
  GET  /projects/new               → Formular: neues Projekt anlegen
  POST /projects/create            → Projekt erstellen
  GET  /projects/{project_id}      → Projekt-Dashboard
  GET  /projects/{project_id}/model → Redirect → /projects/{project_id}/view
  GET  /projects/{project_id}/view  → 3D Viewer (project_viewer.py)
  GET  /projects/{project_id}/documents  → Documents
  GET  /projects/{project_id}/issues     → Issues
  GET  /projects/{project_id}/checking   → Rule-Check Modul
  GET  /projects/{project_id}/settings   → Projekt-Einstellungen
"""

import html
from urllib.parse import quote_plus
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import (
    AUTH_COOKIE_NAME,
    delete_user_account,
    require_user,
    update_user_email,
    update_user_password,
    update_user_profile,
)
from app.exceptions import AuthError, ConflictError, NotFoundError, StorageError, ValidationError

from app.project_storage import (
    list_projects,
    create_project,
    get_project,
    update_project,
    delete_project,
    get_project_model_count,
    get_project_document_count,
)


from app.document_storage import (
    MAX_DOCUMENT_SIZE_MB,
    create_folder,
    delete_document,
    delete_folder,
    download_document_to_temp,
    get_document,
    get_folder,
    list_documents,
    list_folders,
    list_project_ifc_documents,
    save_project_document,
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
input[type=text],input[type=email],input[type=password],input[type=tel],textarea,select{
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
    email = _e(account.get("account_name", ""))
    initials_source = str(account.get("account_name", "") or "?").split("@", 1)[0]
    initials = "".join(part[:1].upper() for part in initials_source.replace("_", ".").split(".") if part)[:2] or "?"
    return (
        '<nav class="bp-nav">'
        '<div class="bp-nav__inner">'
        '<a href="/projects" class="bp-nav__logo"><div class="bp-nav__logo-mark">BP</div>BIMPruef</a>'
        '<div class="bp-nav__links">'
        '<a href="/projects" class="bp-nav__link bp-nav__link--active">Projekte</a>'
        '<a href="/impressum" class="bp-nav__link">Impressum</a>'
        '<a href="/datenschutz" class="bp-nav__link">Datenschutz</a>'
        '</div>'
        '<div class="bp-nav__user">'
        f'<div class="bp-nav__avatar" title="{email}">{_e(initials)}</div>'
        f'<span style="font-size:.8125rem;color:rgba(255,255,255,.65)">{email}</span>'
        '<a href="/account" class="bp-btn bp-btn--ghost bp-btn--sm" style="color:rgba(255,255,255,.75)">Account</a>'
        '<form method="POST" action="/auth/logout" style="margin:0">'
        '<button type="submit" class="bp-btn bp-btn--ghost bp-btn--sm" style="color:rgba(255,255,255,.75)">Logout</button>'
        '</form>'
        '</div>'
        '</div>'
        '</nav>'
    )


def _project_subnav(project_id: str, active: str) -> str:
    """
    Subnav für Projektseiten.

    Der alte Model-Viewer ist entfernt.
    "model" und "view" zeigen beide auf den Direct Viewer.
    """
    pid = _e(project_id)
    effective_active = "view" if active in ("model", "view") else active

    items = [
        ("dashboard", f"/projects/{pid}",          "Dashboard"),
        ("view",      f"/projects/{pid}/view",      "3D Viewer"),
        ("documents", f"/projects/{pid}/documents", "Documents"),
        ("clash",     f"/projects/{pid}/clash",     "Clash"),
        ("list",      f"/projects/{pid}/list",      "List"),
        ("issues",    f"/projects/{pid}/issues",    "Issues"),
        ("checking",  f"/projects/{pid}/checking",  "Checking"),
        ("settings",  f"/projects/{pid}/settings",  "Settings"),
    ]

    links = []
    for key, href, label in items:
        cls = "bp-tab bp-tab--active" if key == effective_active else "bp-tab"
        links.append(f'<a href="{href}" class="{cls}">{label}</a>')

    return (
        '<div class="bp-container">'
        '<div class="bp-tabs" style="margin-top:16px;margin-bottom:0">'
        + "".join(links) +
        '</div></div>'
    )

def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
{GLOBAL_STYLES}
<link rel="stylesheet" href="/static/bimpruef.css">
</head>
<body>
<div class="bp-page">
{body}
</div>
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
        "full_name": user.get("full_name", ""),
        "company": user.get("company", ""),
        "role_title": user.get("role_title", ""),
        "phone": user.get("phone", ""),
        "account_notes": user.get("account_notes", ""),
    }


def _flash_from_query(saved: str = "", error: str = "") -> str:
    if error:
        return f'<div class="flash-err">{_e(error)}</div>'
    if saved:
        messages = {
            "email": "✓ E-Mail-Adresse wurde aktualisiert.",
            "password": "✓ Passwort wurde aktualisiert.",
            "profile": "✓ Persönliche Account-Daten wurden gespeichert.",
        }
        return f'<div class="flash-ok">{_e(messages.get(saved, "✓ Änderungen gespeichert."))}</div>'
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Root → Projektübersicht
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects", response_class=HTMLResponse)
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
# Account-Verwaltung
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/account", response_class=HTMLResponse)
def account_settings(request: Request, saved: str = "", error: str = ""):
    account = _account_from_request(request)
    flash_html = _flash_from_query(saved=saved, error=error)

    body = (
        f'{_topbar_global(account)}'
        '<div style="padding:28px 32px;max-width:1050px;margin:0 auto">'
        '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px">'
        '<div>'
        '<h1 style="font-size:22px;font-weight:600">Account-Verwaltung</h1>'
        '<p style="color:var(--muted);font-size:13px;margin-top:4px">Login-Daten, persönliche Angaben und vollständige Account-Löschung.</p>'
        '</div>'
        '<a href="/projects" class="btn" style="text-decoration:none;font-size:12px">← Zur Projektübersicht</a>'
        '</div>'
        f'{flash_html}'
        '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:16px;align-items:start">'

        '<div class="card">'
        '<h2 style="font-size:16px;margin-bottom:8px">Login-Daten ändern</h2>'
        '<p style="color:var(--muted);font-size:12px;margin-bottom:12px">Jede Änderung wird mit dem bisherigen Passwort bestätigt.</p>'
        '<form method="POST" action="/account/email">'
        '<label>Neue E-Mail-Adresse</label>'
        f'<input type="email" name="new_email" value="{_e(account["account_name"])}" required maxlength="254" autocomplete="email">'
        '<label>Bisheriges Passwort</label>'
        '<input type="password" name="current_password" required autocomplete="current-password">'
        '<button type="submit" class="btn btn-primary" style="margin-top:16px">E-Mail ändern</button>'
        '</form>'
        '<hr style="border:0;border-top:1px solid var(--border);margin:20px 0">'
        '<form method="POST" action="/account/password">'
        '<label>Bisheriges Passwort</label>'
        '<input type="password" name="current_password" required autocomplete="current-password">'
        '<label>Neues Passwort</label>'
        '<input type="password" name="new_password" required autocomplete="new-password">'
        '<div style="font-size:11px;color:var(--muted);margin-top:8px">Mindestens 6 Zeichen und mindestens drei Gruppen: Kleinbuchstaben, Großbuchstaben, Zahl, Sonderzeichen.</div>'
        '<button type="submit" class="btn btn-primary" style="margin-top:16px">Passwort ändern</button>'
        '</form>'
        '</div>'

        '<div class="card">'
        '<h2 style="font-size:16px;margin-bottom:8px">Persönliche Account-Daten</h2>'
        '<p style="color:var(--muted);font-size:12px;margin-bottom:12px">Diese Angaben werden direkt am Account gespeichert.</p>'
        '<form method="POST" action="/account/profile">'
        '<label>Name</label>'
        f'<input type="text" name="full_name" value="{_e(account.get("full_name", ""))}" maxlength="255" autocomplete="name">'
        '<label>Büro / Firma</label>'
        f'<input type="text" name="company" value="{_e(account.get("company", ""))}" maxlength="255" autocomplete="organization">'
        '<label>Rolle / Funktion</label>'
        f'<input type="text" name="role_title" value="{_e(account.get("role_title", ""))}" maxlength="255" autocomplete="organization-title">'
        '<label>Telefon</label>'
        f'<input type="tel" name="phone" value="{_e(account.get("phone", ""))}" maxlength="80" autocomplete="tel">'
        '<label>Notizen / interne Angaben</label>'
        f'<textarea name="account_notes" rows="5" maxlength="3000" style="resize:vertical">{_e(account.get("account_notes", ""))}</textarea>'
        '<button type="submit" class="btn btn-primary" style="margin-top:16px">Account-Daten speichern</button>'
        '</form>'
        '</div>'

        '<div class="card" style="border-color:var(--accent2)">'
        '<h2 style="font-size:16px;margin-bottom:8px;color:#ffb3b3">Account vollständig löschen</h2>'
        '<p style="color:var(--muted);font-size:12px;margin-bottom:12px">Dabei werden der Account, alle Projekte und alle Projektdokumente aus PostgreSQL und Cloudflare R2 gelöscht.</p>'
        '<form method="POST" action="/account/delete" onsubmit="return confirm(\'Account wirklich endgültig löschen? Diese Aktion kann nicht rückgängig gemacht werden.\')">'
        '<label>Passwort erneut eingeben</label>'
        '<input type="password" name="current_password" required autocomplete="current-password">'
        '<label>Bestätigung</label>'
        '<input type="text" name="confirm_text" required placeholder="DELETE" autocomplete="off">'
        '<div style="font-size:11px;color:var(--muted);margin-top:8px">Schreibe DELETE in das Feld, damit versehentliches Löschen verhindert wird.</div>'
        '<button type="submit" class="btn btn-danger" style="margin-top:16px">Account endgültig löschen</button>'
        '</form>'
        '</div>'

        '</div>'
        '</div>'
    )
    return _page("Account-Verwaltung – BIMPruef", body)


@projects_router.post("/account/email")
async def account_email_update(
    request: Request,
    new_email: str = Form(...),
    current_password: str = Form(...),
):
    account = _account_from_request(request)
    try:
        update_user_email(account["account_id"], new_email, current_password)
    except (AuthError, ConflictError, ValidationError) as exc:
        return RedirectResponse(f"/account?error={quote_plus(str(exc))}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/account?error={quote_plus('E-Mail konnte nicht geändert werden: ' + str(exc))}", status_code=303)
    return RedirectResponse("/account?saved=email", status_code=303)


@projects_router.post("/account/password")
async def account_password_update(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
):
    account = _account_from_request(request)
    try:
        update_user_password(account["account_id"], current_password, new_password)
    except (AuthError, ValidationError) as exc:
        return RedirectResponse(f"/account?error={quote_plus(str(exc))}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/account?error={quote_plus('Passwort konnte nicht geändert werden: ' + str(exc))}", status_code=303)
    return RedirectResponse("/account?saved=password", status_code=303)


@projects_router.post("/account/profile")
async def account_profile_update(
    request: Request,
    full_name: str = Form(default=""),
    company: str = Form(default=""),
    role_title: str = Form(default=""),
    phone: str = Form(default=""),
    account_notes: str = Form(default=""),
):
    account = _account_from_request(request)
    try:
        update_user_profile(
            account["account_id"],
            full_name=full_name,
            company=company,
            role_title=role_title,
            phone=phone,
            account_notes=account_notes,
        )
    except AuthError as exc:
        return RedirectResponse(f"/account?error={quote_plus(str(exc))}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/account?error={quote_plus('Account-Daten konnten nicht gespeichert werden: ' + str(exc))}", status_code=303)
    return RedirectResponse("/account?saved=profile", status_code=303)


@projects_router.post("/account/delete")
async def account_delete(
    request: Request,
    current_password: str = Form(...),
    confirm_text: str = Form(...),
):
    account = _account_from_request(request)
    if str(confirm_text or "").strip() != "DELETE":
        return RedirectResponse("/account?error=Bitte+DELETE+zur+Bestätigung+eingeben.", status_code=303)

    try:
        delete_user_account(account["account_id"], current_password)
    except AuthError as exc:
        return RedirectResponse(f"/account?error={quote_plus(str(exc))}", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/account?error={quote_plus('Account konnte nicht gelöscht werden: ' + str(exc))}", status_code=303)

    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


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
        '<a href="/projects" class="btn" style="text-decoration:none">Abbrechen</a>'
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
        return _page("Nicht gefunden", '<div style="padding:40px">Projekt nicht gefunden. <a href="/projects">← Zurück</a></div>')

    pid    = _e(project_id)
    pname  = project["project_name"]
    pcode  = project["project_code"]
    status = project.get("status", "active")
    model_count = get_project_model_count(account_id, project_id)
    try:
        from app.issue_storage import count_project_issues
        issue_count = count_project_issues(account_id, project_id)
    except Exception:
        issue_count = 0

    badge = (f'<span class="badge badge-active">active</span>'
             if status == "active"
             else f'<span class="badge badge-inactive">{_e(status)}</span>')


    stat_cards = (
        f'<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px">'
        f'<div class="card" style="text-align:center">'
        f'<div style="font-size:28px;font-weight:700;color:var(--accent)">{model_count}</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-top:4px">Modelle</div>'
        f'</div>'
        f'<div class="card" style="text-align:center">'
        f'<div style="font-size:28px;font-weight:700;color:var(--accent)">{issue_count}</div>'
        f'<div style="font-size:12px;color:var(--muted);margin-top:4px">Issues</div>'
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
        f'<a href="/projects" class="btn" style="text-decoration:none;font-size:12px">← Alle Projekte</a>'
        f'</div>'
    )

    if project.get("description"):
        body += f'<p style="color:var(--muted);font-size:13px;margin-top:8px">{_e(project["description"])}</p>'

    body += stat_cards + '</div>'

    return _page(f"{pname} – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# Model-Routen
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/model", response_class=HTMLResponse)
def project_model(
    request: Request,
    project_id: str,
    error: str = Query(default=""),
    mode: str = Query(default=""),
):
    """
    Redirect: /model ist jetzt ein Alias für /view (3D Viewer).
    Die gesamte Viewer-Logik liegt in project_viewer.py.
    """
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    url = f"/projects/{_e(project_id)}/view"
    if error:
        url += f"?error={quote_plus(error)}"
    return RedirectResponse(url, status_code=302)


@projects_router.post("/projects/{project_id}/model/load")
def project_model_load(
    request: Request,
    project_id: str,
    document_ids: list[str] = Form(default=[]),
):
    """
    Redirect: model/load leitet auf /view weiter und übergibt doc_ids als
    Query-Parameter, die project_viewer.py direkt versteht.
    """
    from urllib.parse import urlencode
    params = urlencode([("doc_ids", did) for did in document_ids]) if document_ids else ""
    url = f"/projects/{_e(project_id)}/view"
    if params:
        url += "?" + params
    return RedirectResponse(url, status_code=303)


@projects_router.get("/projects/{project_id}/model/clash", response_class=HTMLResponse)
def project_model_clash_redirect(request: Request, project_id: str):
    return RedirectResponse(f"/projects/{_e(project_id)}/clash", status_code=302)


@projects_router.get("/projects/{project_id}/model/list", response_class=HTMLResponse)
def project_model_list_redirect(request: Request, project_id: str):
    return RedirectResponse(f"/projects/{_e(project_id)}/list", status_code=302)


@projects_router.get("/projects/{project_id}/model/rulecheck", response_class=HTMLResponse)
def project_model_rulecheck(request: Request, project_id: str):
    return RedirectResponse(f"/projects/{_e(project_id)}/checking", status_code=302)


# ─────────────────────────────────────────────────────────────────────────────
# List-Modul
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/list", response_class=HTMLResponse)
def project_list(
    request: Request,
    project_id: str,
    saved: str = Query(default=""),
    error: str = Query(default=""),
):
    """Delegation an list_module.project_list."""
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    from app.list_module import project_list as _list_view
    return _list_view(project_id=project_id, request=request, saved=saved, error=error)


# ─────────────────────────────────────────────────────────────────────────────
# Documents-Modul
# ─────────────────────────────────────────────────────────────────────────────

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


def _documents_url(project_id: str, folder_id: str = "", saved: str = "", error: str = "") -> str:
    url = f"/projects/{_e(project_id)}/documents"
    params = []
    if folder_id:
        params.append("folder_id=" + quote_plus(folder_id))
    if saved:
        params.append("saved=" + quote_plus(saved))
    if error:
        params.append("error=" + quote_plus(error))
    return url + ("?" + "&".join(params) if params else "")


@projects_router.get("/projects/{project_id}/documents", response_class=HTMLResponse)
def project_documents(
    request: Request,
    project_id: str,
    folder_id: str = Query(default=""),
    saved: str = Query(default=""),
    error: str = Query(default=""),
):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    current_folder = get_folder(account_id, project_id, folder_id) if folder_id else None
    if folder_id and not current_folder:
        return RedirectResponse(_documents_url(project_id, error="Ordner nicht gefunden."), status_code=303)

    folders = list_folders(account_id, project_id, folder_id)
    docs = list_documents(account_id, project_id, folder_id)

    flash = ""
    if error:
        flash = f'<div class="flash-err">{_e(error)}</div>'
    elif saved:
        messages = {
            "upload": "✓ Datei wurde hochgeladen.",
            "folder": "✓ Ordner wurde erstellt.",
            "deleted": "✓ Datei wurde gelöscht.",
            "folder_deleted": "✓ Ordner wurde gelöscht.",
        }
        flash = f'<div class="flash-ok">{_e(messages.get(saved, "✓ Änderung gespeichert."))}</div>'

    folder_options = '<option value="">Root</option>'
    all_folders = []
    def collect(parent_id="", depth=0):
        for f in list_folders(account_id, project_id, parent_id):
            all_folders.append((f, depth))
            collect(f["folder_id"], depth + 1)
    collect()
    for f, depth in all_folders:
        selected = " selected" if f["folder_id"] == folder_id else ""
        prefix = "— " * depth
        folder_options += f'<option value="{_e(f["folder_id"])}"{selected}>{_e(prefix + f["path"])}</option>'

    breadcrumb = f'<a href="/projects/{_e(project_id)}/documents">Root</a>'
    if current_folder:
        parts = current_folder["path"].split("/")
        breadcrumb += " / " + " / ".join(_e(p) for p in parts)

    rows = ""
    for f in folders:
        rows += f"""
        <tr>
          <td>📁</td>
          <td><a href="{_documents_url(project_id, f['folder_id'])}" style="font-weight:600">{_e(f['name'])}</a></td>
          <td>Ordner</td>
          <td>–</td>
          <td>{_e(f['path'])}</td>
          <td style="font-size:12px;color:var(--muted)">{_e(f.get('created_at','')[:10])}</td>
          <td>
            <form method="POST" action="/projects/{_e(project_id)}/documents/folders/delete" style="display:inline" onsubmit="return confirm('Ordner wirklich löschen? Nur leere Ordner können gelöscht werden.')">
              <input type="hidden" name="folder_id" value="{_e(f['folder_id'])}">
              <input type="hidden" name="current_folder_id" value="{_e(folder_id)}">
              <button type="submit" class="btn btn-danger" style="font-size:11px;padding:3px 8px">Löschen</button>
            </form>
          </td>
        </tr>"""

    for d in docs:
        icon = "🏗" if d["file_extension"] in (".ifc", ".ifczip") else "📄"
        rows += f"""
        <tr>
          <td>{icon}</td>
          <td style="font-weight:600;color:var(--text)">{_e(d['original_filename'])}</td>
          <td>{_e(d['file_extension'])}<br><span style="font-size:11px;color:var(--muted)">{_e(d['document_kind'])}</span></td>
          <td>{_fmt_size(d['file_size'])}</td>
          <td>{_e(d.get('folder_path') or 'Root')}</td>
          <td style="font-size:12px;color:var(--muted)">{_e(d.get('created_at','')[:10])}</td>
          <td style="white-space:nowrap">
            <a class="btn" href="/projects/{_e(project_id)}/documents/download/{_e(d['document_id'])}" style="font-size:11px;padding:3px 8px;text-decoration:none">Download</a>
            <form method="POST" action="/projects/{_e(project_id)}/documents/delete" style="display:inline" onsubmit="return confirm('Datei wirklich löschen? Die Datei wird auch aus R2 entfernt.')">
              <input type="hidden" name="document_id" value="{_e(d['document_id'])}">
              <input type="hidden" name="folder_id" value="{_e(folder_id)}">
              <button type="submit" class="btn btn-danger" style="font-size:11px;padding:3px 8px">Löschen</button>
            </form>
          </td>
        </tr>"""

    empty = ""
    if not rows:
        empty = '<div class="flash-ok">Dieser Ordner ist leer.</div>'

    body = f"""
    {_topbar_global(account)}
    {_project_subnav(project_id, "documents")}
    <div style="padding:28px 32px;max-width:1180px;margin:0 auto">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px">
        <div>
          <h1 style="font-size:22px;font-weight:600">Documents</h1>
          <div style="color:var(--muted);font-size:13px;margin-top:4px">{breadcrumb}</div>
        </div>
        <a href="/projects/{_e(project_id)}/view" class="btn" style="text-decoration:none">3D Viewer</a>
      </div>
      {flash}
      <div style="display:grid;grid-template-columns:minmax(280px,360px) 1fr;gap:16px;align-items:start">
        <div>
          <div class="card">
            <h2 style="font-size:15px;margin-bottom:10px">Datei hochladen</h2>
            <form method="POST" action="/projects/{_e(project_id)}/documents/upload" enctype="multipart/form-data">
              <input type="hidden" name="current_folder_id" value="{_e(folder_id)}">
              <label>Zielordner</label>
              <select name="folder_id">{folder_options}</select>
              <label>Datei</label>
              <input type="file" name="file" required>
              <div style="font-size:11px;color:var(--muted);margin-top:8px">Max. {MAX_DOCUMENT_SIZE_MB} MB pro Datei. IFC, BCF, PDF, Office, CSV, TXT, ZIP und Bilder sind erlaubt.</div>
              <button type="submit" class="btn btn-primary" style="margin-top:14px">Datei hochladen</button>
            </form>
          </div>
          <div class="card">
            <h2 style="font-size:15px;margin-bottom:10px">Ordner erstellen</h2>
            <form method="POST" action="/projects/{_e(project_id)}/documents/folders">
              <input type="hidden" name="parent_folder_id" value="{_e(folder_id)}">
              <label>Ordnername</label>
              <input type="text" name="name" placeholder="z. B. IFC, BCF, Pläne" required maxlength="120">
              <button type="submit" class="btn btn-primary" style="margin-top:14px">Ordner erstellen</button>
            </form>
          </div>
        </div>
        <div class="card" style="overflow-x:auto">
          <table>
            <tr><th style="width:42px">Typ</th><th>Name</th><th>Extension</th><th>Größe</th><th>Ordnerpfad</th><th>Datum</th><th>Aktionen</th></tr>
            {rows}
          </table>
          {empty}
        </div>
      </div>
    </div>"""
    return _page(f"{project['project_name']} – Documents", body)


@projects_router.post("/projects/{project_id}/documents/upload")
async def project_documents_upload(
    request: Request,
    project_id: str,
    file: UploadFile = File(...),
    folder_id: str = Form(default=""),
    current_folder_id: str = Form(default=""),
):
    account = _account_from_request(request)
    try:
        data = await file.read()
        save_project_document(
            account["account_id"],
            project_id,
            data,
            file.filename or "uploaded_file",
            content_type=file.content_type or "application/octet-stream",
            folder_id=folder_id,
        )
        return RedirectResponse(_documents_url(project_id, current_folder_id, saved="upload"), status_code=303)
    except Exception as exc:
        return RedirectResponse(_documents_url(project_id, current_folder_id, error=str(exc)), status_code=303)


@projects_router.post("/projects/{project_id}/documents/folders")
def project_documents_create_folder(
    request: Request,
    project_id: str,
    name: str = Form(...),
    parent_folder_id: str = Form(default=""),
):
    account = _account_from_request(request)
    try:
        create_folder(account["account_id"], project_id, name, parent_folder_id)
        return RedirectResponse(_documents_url(project_id, parent_folder_id, saved="folder"), status_code=303)
    except Exception as exc:
        return RedirectResponse(_documents_url(project_id, parent_folder_id, error=str(exc)), status_code=303)


@projects_router.post("/projects/{project_id}/documents/folders/delete")
def project_documents_delete_folder(
    request: Request,
    project_id: str,
    folder_id: str = Form(...),
    current_folder_id: str = Form(default=""),
):
    account = _account_from_request(request)
    try:
        delete_folder(account["account_id"], project_id, folder_id)
        return RedirectResponse(_documents_url(project_id, current_folder_id, saved="folder_deleted"), status_code=303)
    except Exception as exc:
        return RedirectResponse(_documents_url(project_id, current_folder_id, error=str(exc)), status_code=303)


@projects_router.get("/projects/{project_id}/documents/download/{document_id}")
def project_documents_download(request: Request, project_id: str, document_id: str):
    account = _account_from_request(request)
    tmp_path = ""
    try:
        doc, tmp_path = download_document_to_temp(account["account_id"], project_id, document_id)
        with open(tmp_path, "rb") as f:
            data = f.read()
        return Response(
            content=data,
            media_type=doc.get("content_type") or "application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{_e(doc["safe_filename"])}"'},
        )
    finally:
        if tmp_path:
            try:
                import os
                os.remove(tmp_path)
            except OSError:
                pass


@projects_router.post("/projects/{project_id}/documents/delete")
def project_documents_delete(
    request: Request,
    project_id: str,
    document_id: str = Form(...),
    folder_id: str = Form(default=""),
):
    account = _account_from_request(request)
    try:
        delete_document(account["account_id"], project_id, document_id)
        return RedirectResponse(_documents_url(project_id, folder_id, saved="deleted"), status_code=303)
    except Exception as exc:
        return RedirectResponse(_documents_url(project_id, folder_id, error=str(exc)), status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Issues-Modul
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/issues", response_class=HTMLResponse)
def project_issues(request: Request, project_id: str, saved: str = "", error: str = ""):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    from app.issue_storage import list_project_issues
    issues = list_project_issues(account_id, project_id)

    flash = ""
    if error:
        flash = f'<div class="flash-err">⚠ {_e(error)}</div>'
    elif saved:
        flash = '<div class="flash-ok">✓ Änderung gespeichert.</div>'

    rows = ""
    for i in issues:
        issue_id = _e(i["issue_id"])
        rows += f"""
        <tr>
          <td style="font-weight:600;color:var(--accent)">{_e(i.get('title',''))}</td>
          <td>{_e(i.get('issue_type',''))}</td>
          <td>{_e(i.get('status',''))}</td>
          <td style="font-size:11px;color:var(--muted);font-family:monospace">
            {_e(i.get('global_id_1',''))}<br>{_e(i.get('global_id_2',''))}
          </td>
          <td style="font-size:12px;color:var(--muted)">{_e(i.get('created_at','')[:10])}</td>
          <td style="white-space:nowrap">
            <a class="btn" href="/projects/{_e(project_id)}/issues/{issue_id}/bcf" style="font-size:11px;padding:3px 8px;text-decoration:none">BCF</a>
            <form method="POST" action="/projects/{_e(project_id)}/issues/delete" style="display:inline" onsubmit="return confirm('Issue wirklich löschen?')">
              <input type="hidden" name="issue_id" value="{issue_id}">
              <button class="btn btn-danger" type="submit" style="font-size:11px;padding:3px 8px">Löschen</button>
            </form>
          </td>
        </tr>"""

    empty = ""
    if not issues:
        empty = (
            '<div class="card" style="text-align:center">'
            '<h3 style="font-size:16px;margin-bottom:8px">Noch keine Issues vorhanden</h3>'
            '<p style="color:var(--muted);font-size:13px;margin-bottom:14px">Speichere ausgewählte Clash-Zeilen im Clash-Modul als Issues.</p>'
            f'<a class="btn btn-primary" href="/projects/{_e(project_id)}/clash" style="text-decoration:none">Zur Clash-Analyse</a>'
            '</div>'
        )

    export_all = ""
    if issues:
        export_all = f'<a class="btn btn-primary" href="/projects/{_e(project_id)}/issues/bcf" style="text-decoration:none">Alle Clash-Issues als BCF exportieren</a>'

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "issues")}'
        '<div style="padding:28px 32px;max-width:1200px;margin:0 auto">'
        '<div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px">'
        '<div>'
        '<h1 style="font-size:22px;font-weight:600">Issues</h1>'
        '<p style="color:var(--muted);font-size:13px;margin-top:4px">Zentrale Issue-Verwaltung. BCF-Export gehört zu diesem Modul.</p>'
        '</div>'
        f'{export_all}'
        '</div>'
        f'{flash}'
    )
    if issues:
        body += '<div class="card" style="overflow-x:auto"><table><tr><th>Titel</th><th>Typ</th><th>Status</th><th>GlobalIds</th><th>Erstellt</th><th>Aktion</th></tr>' + rows + '</table></div>'
    body += empty + '</div>'
    return _page(f"{project['project_name']} – Issues", body)


@projects_router.get("/projects/{project_id}/issues/data")
def project_issues_data(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]

    project = get_project(account_id, project_id)
    if not project:
        return JSONResponse(
            {"error": "Projekt nicht gefunden."},
            status_code=404,
        )

    from app.issue_storage import list_project_issues
    issues = list_project_issues(account_id, project_id)

    return JSONResponse({
        "issues": [
            {
                "title": issue.get("title", ""),
                "status": issue.get("status", ""),
                "issue_type": issue.get("issue_type", ""),
                "created_at": str(issue.get("created_at", "")),
            }
            for issue in issues
        ]
    })


@projects_router.get("/projects/{project_id}/issues/bcf")
def project_issues_bcf(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    try:
        from app.bcf_export import create_bcf_zip_from_clashes
        from app.issue_storage import issue_to_bcf_clash, list_project_issues
        issues = [i for i in list_project_issues(account_id, project_id) if i.get("issue_type") == "clash"]
        data = create_bcf_zip_from_clashes([issue_to_bcf_clash(i) for i in issues], project_name=project.get("project_name") or "BIMPruef Issues")
        return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="project_issues.bcfzip"'})
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)


@projects_router.get("/projects/{project_id}/issues/{issue_id}/bcf")
def project_issue_single_bcf(request: Request, project_id: str, issue_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    try:
        from app.bcf_export import create_bcf_zip_from_clashes
        from app.issue_storage import get_issue, issue_to_bcf_clash
        issue = get_issue(account_id, project_id, issue_id)
        data = create_bcf_zip_from_clashes([issue_to_bcf_clash(issue)], project_name=project.get("project_name") or "BIMPruef Issue")
        return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": f'attachment; filename="issue_{issue_id}.bcfzip"'})
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)


@projects_router.post("/projects/{project_id}/issues/delete")
def project_issue_delete(request: Request, project_id: str, issue_id: str = Form(...)):
    account = _account_from_request(request)
    account_id = account["account_id"]
    try:
        from app.issue_storage import delete_issue
        delete_issue(account_id, project_id, issue_id)
        return RedirectResponse(f"/projects/{_e(project_id)}/issues?saved=deleted", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/projects/{_e(project_id)}/issues?error={quote_plus(str(exc))}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Checking-Modul
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/checking", response_class=HTMLResponse)
def project_checking(request: Request, project_id: str,
                     saved: str = Query(default=""), error: str = Query(default="")):
    from app.project_rulecheck import project_checking_page
    return project_checking_page(request, project_id, saved=saved, error=error)


# ─────────────────────────────────────────────────────────────────────────────
# Settings
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.get("/projects/{project_id}/settings", response_class=HTMLResponse)
def project_settings(
    request: Request,
    project_id: str,
    saved: str = "",
    error: str = "",
):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    pid = _e(project_id)

    flash_html = ""
    if error:
        flash_html = f'<div class="flash-err">{_e(error)}</div>'
    elif saved == "1":
        flash_html = '<div class="flash-ok">✓ Einstellungen gespeichert.</div>'

    project_name_escaped = _e(project["project_name"])

    body = (
        f'{_topbar_global(account)}'
        f'{_project_subnav(project_id, "settings")}'
        '<div style="padding:28px 32px;max-width:760px">'
        f'{flash_html}'

        '<div class="card">'
        '<h2 style="font-size:16px;margin-bottom:16px">Projekteinstellungen</h2>'
        f'<form method="POST" action="/projects/{pid}/settings">'
        '<label>Projektcode</label>'
        f'<input type="text" name="project_code" value="{_e(project["project_code"])}" required maxlength="40">'
        '<label>Projektname</label>'
        f'<input type="text" name="project_name" value="{project_name_escaped}" required maxlength="120">'
        '<label>Beschreibung</label>'
        f'<textarea name="description" rows="3" style="resize:vertical">{_e(project.get("description",""))}</textarea>'
        '<label>Status</label>'
        '<select name="status">'
        f'<option value="active" {"selected" if project.get("status")=="active" else ""}>active</option>'
        f'<option value="inactive" {"selected" if project.get("status")=="inactive" else ""}>inactive</option>'
        '</select>'
        '<div style="margin-top:20px;display:flex;gap:10px">'
        '<button type="submit" class="btn btn-primary">Speichern</button>'
        f'<a href="/projects/{pid}" class="btn" style="text-decoration:none">Abbrechen</a>'
        '</div>'
        '</form>'
        '</div>'

        '<div class="card" style="border-color:var(--accent2)">'
        '<h2 style="font-size:16px;margin-bottom:8px;color:#ffb3b3">Projekt vollständig löschen</h2>'
        '<p style="color:var(--muted);font-size:12px;margin-bottom:12px">'
        'Dabei wird das Projekt endgültig gelöscht. Alle zugehörigen Projektdokumente, '
        'Metadaten, Issues und Cloudflare-R2-Dateien werden entfernt. '
        'Diese Aktion kann nicht rückgängig gemacht werden.'
        '</p>'
        f'<form method="POST" action="/projects/{pid}/delete" '
        'onsubmit="return confirm(\'Projekt wirklich endgültig löschen? Diese Aktion kann nicht rückgängig gemacht werden.\')">'
        '<label>Projektname zur Bestätigung</label>'
        f'<input type="text" name="confirm_project_name" required '
        f'placeholder="{project_name_escaped}" autocomplete="off">'
        '<label>Bestätigung</label>'
        '<input type="text" name="confirm_text" required placeholder="DELETE" autocomplete="off">'
        '<div style="font-size:11px;color:var(--muted);margin-top:8px">'
        'Gib exakt den Projektnamen ein und schreibe DELETE in das zweite Feld.'
        '</div>'
        '<button type="submit" class="btn btn-danger" style="margin-top:16px">'
        'Projekt endgültig löschen'
        '</button>'
        '</form>'
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
# Projekt löschen
# ─────────────────────────────────────────────────────────────────────────────

@projects_router.post("/projects/{project_id}/delete")
async def project_delete(
    request: Request,
    project_id: str,
    confirm_project_name: str = Form(...),
    confirm_text: str = Form(...),
):
    account = _account_from_request(request)
    account_id = account["account_id"]

    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=303)

    if confirm_project_name.strip() != project["project_name"]:
        return RedirectResponse(
            f"/projects/{project_id}/settings?error="
            f"{quote_plus('Der eingegebene Projektname stimmt nicht überein.')}",
            status_code=303,
        )

    if confirm_text.strip() != "DELETE":
        return RedirectResponse(
            f"/projects/{project_id}/settings?error="
            f"{quote_plus('Bitte schreibe DELETE, um das Löschen zu bestätigen.')}",
            status_code=303,
        )

    try:
        delete_project(account_id, project_id)
    except NotFoundError:
        return RedirectResponse("/", status_code=303)
    except (ValidationError, StorageError) as exc:
        return RedirectResponse(
            f"/projects/{project_id}/settings?error="
            f"{quote_plus('Projekt konnte nicht vollständig gelöscht werden: ' + str(exc))}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/projects/{project_id}/settings?error="
            f"{quote_plus('Unerwarteter Fehler beim Löschen des Projekts: ' + str(exc))}",
            status_code=303,
        )

    return RedirectResponse("/?project_deleted=1", status_code=303)
