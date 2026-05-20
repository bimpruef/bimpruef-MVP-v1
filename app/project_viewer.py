"""
project_viewer.py – BIMPruef 3D-Viewer als Projektmodul

Der Viewer ist jetzt ein gleichrangiges Projektmodul neben Documents, Issues,
Clash und Checking. Dateien werden ausschließlich aus dem Documents-Modul
des jeweiligen Projekts geladen. Es gibt keinen direkten Upload mehr.

Routen (alle unter /projects/{project_id}/viewer/):
  GET  /projects/{project_id}/viewer/          → Viewer-Hauptseite
  GET  /projects/{project_id}/viewer/select    → Modell-Auswahl aus Documents
  POST /projects/{project_id}/viewer/load      → Ausgewählte Modelle in Cache laden
  POST /projects/{project_id}/viewer/remove    → Einzelnen Slot entfernen
  GET  /projects/{project_id}/viewer/file/     → Rohe IFC-Datei ausliefern
  POST /projects/{project_id}/viewer/element/update/ → Element-Eigenschaften aktualisieren
  GET  /projects/{project_id}/viewer/export-ifc/     → Modifizierte IFC-Datei herunterladen

Technische API-Endpunkte (session-basiert, weiterhin gültig):
  POST /viewer/ai-chat/     → KI-Assistent (wird von viewer.py bedient)
"""

import html
import io
import os
import urllib.parse

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_user
from app.document_storage import (
    list_project_ifc_documents,
    prepare_viewer_session_from_project_documents,
)
from app.exceptions import NotFoundError, StorageError, ValidationError
from app.project_storage import (
    get_or_create_project_session,
    get_project,
)
from app.storage import (
    get_ifc_label,
    get_ifc_path,
    get_session_slots,
    remove_ifc_slot,
    session_exists,
)

project_viewer_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s or ""))


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


SLOT_COLORS = [
    "#4fc3f7", "#ef9a9a", "#a5d6a7", "#ffcc80",
    "#ce93d8", "#80cbc4", "#f48fb1", "#ffab40",
    "#80deea", "#bcaaa4",
]

def _slot_color(slot: int) -> str:
    return SLOT_COLORS[(slot - 1) % len(SLOT_COLORS)]


def _load_context(request: Request, project_id: str) -> tuple:
    """
    Lade Account, Projekt und Session-ID. Gibt (account, project, session_id)
    zurück oder wirft NotFoundError.
    """
    user = require_user(request)
    account_id = user["user_id"]
    project = get_project(account_id, project_id)
    if not project:
        raise NotFoundError("Projekt nicht gefunden.")
    sid = get_or_create_project_session(account_id, project_id)
    return user, project, sid


# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsame UI-Bausteine (im Projektstil)
# ─────────────────────────────────────────────────────────────────────────────

_VIEWER_STYLES = """\
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0e1a;--surface:#16213e;--surface2:#1a2a4a;
  --border:#1e3a6e;--accent:#4fc3f7;--accent2:#e94560;
  --text:#d0dce8;--muted:#4a6080;--success:#4caf50;
}
html,body{height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);
  color:var(--text);line-height:1.5}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button,.btn{padding:7px 14px;background:var(--surface2);border:1px solid var(--border);
  color:var(--text);border-radius:6px;cursor:pointer;font-size:13px;
  transition:background .15s,border-color .15s;display:inline-block;text-decoration:none}
button:hover,.btn:hover{background:#223a5e;border-color:var(--accent)}
.btn-primary{background:var(--accent);color:#0a1a2e;border-color:var(--accent);font-weight:600}
.btn-primary:hover{background:#81d4fa}
.btn-danger{background:#2a0a14;border-color:var(--accent2);color:#ffb3b3}
.btn-danger:hover{background:#6e1a2e}
.btn-sm{font-size:11px;padding:3px 9px}
.flash-err{background:#2a0a10;border:1px solid var(--accent2);border-radius:8px;
  padding:9px 13px;color:#ffaaaa;font-size:12px;margin:6px 14px 0}
.flash-ok{background:#0a2a10;border:1px solid var(--success);border-radius:8px;
  padding:9px 13px;color:#aaffaa;font-size:12px;margin:6px 14px 0}
.model-card{padding:5px 10px;border-bottom:1px solid var(--border)}
.cat-item{display:flex;align-items:center;gap:6px;padding:3px 10px;cursor:pointer;
  user-select:none;border-bottom:1px solid #1a2540}
.cat-item:hover{background:#1e2f50}
@keyframes spin{to{transform:rotate(360deg)}}
</style>"""


def _page_wrapper(title: str, body: str) -> HTMLResponse:
    """Vollständige HTML-Seite im Projektstil."""
    return HTMLResponse(f"""<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} – BIMPruef</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
{_VIEWER_STYLES}
<link rel="stylesheet" href="/static/bimpruef.css">
</head>
<body>
{body}
</body>
</html>""")


def _topbar(project: dict, session_id: str, active_tab: str = "viewer") -> str:
    """
    Obere Navigationsleiste mit globalem Nav (Projekte/Account) und
    Projekt-Subnav (alle Projektmodule) im bp-Stil.
    """
    pid = _e(project["project_id"])
    pname = _e(project.get("project_name", ""))
    sid = _e(session_id)

    # Globale Nav
    global_nav = (
        '<nav class="bp-nav">'
        '<div class="bp-nav__inner">'
        '<a href="/" class="bp-nav__logo"><div class="bp-nav__logo-mark">BP</div>BIMPruef</a>'
        '<div class="bp-nav__links">'
        '<a href="/" class="bp-nav__link">Projekte</a>'
        '</div>'
        f'<span style="font-size:.8125rem;color:rgba(255,255,255,.55);margin-left:auto">'
        f'{pname}'
        f'</span>'
        '</div>'
        '</nav>'
    )

    # Subnav (alle Projektmodule)
    tabs = [
        ("dashboard",  f"/projects/{pid}",              "Dashboard"),
        ("viewer",     f"/projects/{pid}/viewer/",      "Viewer"),
        ("documents",  f"/projects/{pid}/documents",    "Documents"),
        ("clash",      f"/projects/{pid}/clash",        "Clash"),
        ("list",       f"/projects/{pid}/list",         "List"),
        ("issues",     f"/projects/{pid}/issues",       "Issues"),
        ("checking",   f"/projects/{pid}/checking",     "Checking"),
        ("settings",   f"/projects/{pid}/settings",     "Settings"),
    ]
    tab_links = "".join(
        f'<a href="{href}" class="bp-tab{"  bp-tab--active" if key == active_tab else ""}">{label}</a>'
        for key, href, label in tabs
    )
    subnav = (
        f'<div class="bp-container"><div class="bp-tabs" style="margin-top:14px;margin-bottom:0">'
        f'{tab_links}'
        f'</div></div>'
    )

    return global_nav + subnav


def _sidebar_model_cards(project_id: str, session_id: str, slots: list) -> str:
    """Modell-Karten für die Viewer-Sidebar."""
    pid = _e(project_id)
    sid = _e(session_id)
    cards = ""
    for s in slots:
        col = _slot_color(s)
        lbl = _e(get_ifc_label(session_id, s))
        escaped_lbl = lbl.replace("'", "\\'").replace("\\", "\\\\")
        cards += f"""
<div class="model-card">
  <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">
    <input type="checkbox" class="chk-model" data-slot="{s}" checked
      style="accent-color:{col};width:13px;height:13px;flex-shrink:0">
    <span style="width:10px;height:10px;border-radius:50%;border:2px solid {col};
      flex-shrink:0;display:inline-block"></span>
    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      title="{lbl}">{lbl}</span>
    <form method="post" action="/projects/{pid}/viewer/remove"
      style="display:inline;margin:0"
      onsubmit="return confirm('Datei \\'{escaped_lbl}\\' wirklich aus dem Viewer entfernen?')">
      <input type="hidden" name="session_id" value="{sid}">
      <input type="hidden" name="slot" value="{s}">
      <button type="submit" title="Entfernen"
        style="padding:1px 6px;font-size:10px;background:#2a0d14;
        border:1px solid #6e1a2e;color:#ff8080;border-radius:3px;cursor:pointer">✕</button>
    </form>
  </label>
</div>"""
    return cards


def _sidebar_actions(project_id: str) -> str:
    """Aktionsbereich oben in der Sidebar – Links zu Documents und Modell-Auswahl."""
    pid = _e(project_id)
    return f"""
<div style="padding:8px 10px;border-bottom:1px solid var(--border)">
  <a class="btn btn-primary btn-sm"
    href="/projects/{pid}/viewer/select"
    style="display:block;text-align:center;text-decoration:none;margin-bottom:5px">
    ＋ Modelle aus Documents laden
  </a>
  <a class="btn btn-sm"
    href="/projects/{pid}/documents"
    style="display:block;text-align:center;text-decoration:none">
    Zu Documents
  </a>
  <div style="font-size:10px;color:var(--muted);margin-top:6px;line-height:1.4">
    Uploads erfolgen ausschließlich im Documents-Modul.
  </div>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
# Modell-Auswahl (aus Documents)
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/viewer/select", response_class=HTMLResponse)
def project_viewer_select(request: Request, project_id: str, error: str = Query(default="")):
    """
    Zeigt alle IFC/IFCZIP-Dokumente des Projekts zur Auswahl für den Viewer.
    Dies ersetzt den alten direkten Upload-Bereich im Model-Modul.
    """
    try:
        user, project, sid = _load_context(request, project_id)
    except NotFoundError:
        return RedirectResponse("/", status_code=302)

    account_id = user["user_id"]
    pid = _e(project_id)
    docs = list_project_ifc_documents(account_id, project_id)

    error_html = f'<div class="flash-err">{_e(error)}</div>' if error else ""

    if not docs:
        content = f"""
<div style="max-width:680px;margin:40px auto;text-align:center">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:32px 24px">
    <div style="font-size:36px;margin-bottom:12px">🏗</div>
    <h2 style="font-size:18px;margin-bottom:8px">Keine IFC-Modelle in Documents vorhanden</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:20px;line-height:1.6">
      Der Viewer lädt Modelle ausschließlich aus dem Documents-Modul dieses Projekts.
      Lade zuerst eine .ifc- oder .ifczip-Datei in Documents hoch.
    </p>
    <a class="btn btn-primary" href="/projects/{pid}/documents" style="text-decoration:none">
      Zu Documents wechseln
    </a>
  </div>
</div>"""
    else:
        rows = ""
        for d in docs:
            rows += f"""
<tr>
  <td style="width:40px;text-align:center">
    <input type="checkbox" name="document_ids" value="{_e(d['document_id'])}" checked
      style="accent-color:var(--accent);width:14px;height:14px">
  </td>
  <td style="font-weight:600;color:var(--accent)">
    <span style="margin-right:6px">🏗</span>{_e(d['original_filename'])}
  </td>
  <td style="color:var(--muted);font-size:12px">{_e(d['file_extension'].upper().lstrip('.'))}</td>
  <td style="font-size:12px">{_fmt_size(d['file_size'])}</td>
  <td style="font-size:12px;color:var(--muted)">{_e(d.get('folder_path') or '— Root —')}</td>
  <td style="font-size:12px;color:var(--muted)">{_e(d.get('created_at','')[:10])}</td>
</tr>"""

        content = f"""
<div style="max-width:960px;margin:28px auto">
  <div style="background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:24px">
    <h2 style="font-size:18px;font-weight:600;margin-bottom:6px">Modelle aus Documents laden</h2>
    <p style="color:var(--muted);font-size:13px;margin-bottom:20px;line-height:1.5">
      Wähle die IFC/IFCZIP-Dokumente aus, die im Viewer-Cache geladen werden sollen.
      Die Originaldateien bleiben dauerhaft in Documents + R2 gespeichert.
    </p>
    <form method="POST" action="/projects/{pid}/viewer/load">
      <div style="overflow-x:auto">
        <table style="border-collapse:collapse;width:100%">
          <thead>
            <tr style="background:var(--surface2)">
              <th style="padding:10px 12px;text-align:left;border:1px solid var(--border);font-size:12px;color:#8ab;width:40px"></th>
              <th style="padding:10px 12px;text-align:left;border:1px solid var(--border);font-size:12px;color:#8ab">Dateiname</th>
              <th style="padding:10px 12px;text-align:left;border:1px solid var(--border);font-size:12px;color:#8ab">Typ</th>
              <th style="padding:10px 12px;text-align:left;border:1px solid var(--border);font-size:12px;color:#8ab">Größe</th>
              <th style="padding:10px 12px;text-align:left;border:1px solid var(--border);font-size:12px;color:#8ab">Ordner</th>
              <th style="padding:10px 12px;text-align:left;border:1px solid var(--border);font-size:12px;color:#8ab">Hochgeladen</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
      <div style="display:flex;gap:10px;margin-top:18px;align-items:center">
        <button type="submit" class="btn btn-primary">
          Ausgewählte Modelle in Viewer laden
        </button>
        <a class="btn" href="/projects/{pid}/viewer/" style="text-decoration:none">
          Abbrechen
        </a>
        <a class="btn" href="/projects/{pid}/documents" style="text-decoration:none;margin-left:auto">
          Documents öffnen
        </a>
      </div>
    </form>
  </div>
</div>"""

    body = f"""
{_topbar(project, sid, "viewer")}
<div style="padding:0 0 0 0">
  {error_html}
  {content}
</div>"""

    return _page_wrapper(f"{project['project_name']} – Viewer – Modelle auswählen", body)


# ─────────────────────────────────────────────────────────────────────────────
# Modelle laden (POST)
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.post("/projects/{project_id}/viewer/load")
def project_viewer_load(
    request: Request,
    project_id: str,
    document_ids: list[str] = Form(default=[]),
):
    """
    Lädt ausgewählte IFC-Dokumente aus Documents in den Viewer-Cache.
    Der Viewer-Cache (Session-Slots) ist ephemer; Documents + R2 sind die
    dauerhafte Quelle.
    """
    try:
        user, _project, sid = _load_context(request, project_id)
    except NotFoundError:
        return RedirectResponse("/", status_code=302)

    try:
        prepare_viewer_session_from_project_documents(
            user["user_id"],
            project_id,
            document_ids,
            session_id=sid,
        )
        return RedirectResponse(f"/projects/{_e(project_id)}/viewer/", status_code=303)
    except (ValidationError, StorageError, NotFoundError) as exc:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/viewer/select?error={urllib.parse.quote(str(exc))}",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/viewer/select?error={urllib.parse.quote('Unbekannter Fehler: ' + str(exc))}",
            status_code=303,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Slot entfernen
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.post("/projects/{project_id}/viewer/remove")
async def project_viewer_remove(
    request: Request,
    project_id: str,
    session_id: str = Form(...),
    slot: int = Form(...),
):
    """Entfernt einen Slot aus dem Viewer-Cache. Documents bleiben unberührt."""
    try:
        _load_context(request, project_id)  # Auth-Check
    except NotFoundError:
        return RedirectResponse("/", status_code=302)

    if session_exists(session_id):
        remove_ifc_slot(session_id, slot)
    return RedirectResponse(f"/projects/{_e(project_id)}/viewer/", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Datei ausliefern
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/viewer/file/")
def project_viewer_file(
    request: Request,
    project_id: str,
    session_id: str = Query(...),
    slot: int = Query(default=1),
):
    """
    Liefert die IFC-Datei aus dem Session-Cache aus.
    Wird vom Three.js-Frontend direkt aufgerufen.
    """
    try:
        _load_context(request, project_id)  # Auth-Check
    except NotFoundError:
        return Response(content="Projekt nicht gefunden.", status_code=404)

    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)
    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return Response(content=f"Slot {slot} nicht gefunden.", status_code=404)
    with open(path, "rb") as f:
        data = f.read()
    label = get_ifc_label(session_id, slot, f"model_{slot}.ifc")
    dl_name = label if label.lower().endswith(".ifc") else label.rsplit(".", 1)[0] + ".ifc"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Element-Eigenschaften aktualisieren
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.post("/projects/{project_id}/viewer/element/update/")
async def project_viewer_element_update(request: Request, project_id: str):
    """
    Aktualisiert Eigenschaften eines IFC-Elements im Session-Cache.
    Delegiert an die gleiche Implementierung wie viewer.py, aber
    unter projektsicherem Pfad.
    """
    try:
        _load_context(request, project_id)
    except NotFoundError:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)

    # Vollständige Implementierung übernommen aus viewer.py:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiger JSON-Body."}, status_code=400)

    session_id = body.get("session_id", "")
    slot       = int(body.get("slot", 1))
    express_id = int(body.get("express_id", 0))
    changes    = body.get("changes", {})

    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return JSONResponse({"error": f"Slot {slot} nicht gefunden."}, status_code=404)

    try:
        import ifcopenshell
        import ifcopenshell.api
        import ifcopenshell.util.element

        model  = ifcopenshell.open(path)
        elem   = model.by_id(express_id)
        if elem is None:
            return JSONResponse({"error": f"Element #{express_id} nicht gefunden."}, status_code=404)

        ifc_type = elem.is_a()

        simple_attrs = ["Name", "ObjectType", "Description", "Tag", "PredefinedType"]
        for attr in simple_attrs:
            if attr in changes:
                val = changes[attr]
                try:
                    setattr(elem, attr, val if val != "" else None)
                except Exception:
                    pass

        pset_changes = changes.get("psets", {})
        for pset_name, props in pset_changes.items():
            existing_pset = None
            try:
                all_psets = ifcopenshell.util.element.get_psets(elem, psets_only=True)
                if pset_name in all_psets:
                    for rel in model.by_type("IfcRelDefinesByProperties"):
                        pdef = rel.RelatingPropertyDefinition
                        if (pdef.is_a("IfcPropertySet") and
                                pdef.Name == pset_name and
                                elem in rel.RelatedObjects):
                            existing_pset = pdef
                            break
            except Exception:
                pass

            if existing_pset is None:
                try:
                    existing_pset = ifcopenshell.api.run(
                        "pset.add_pset", model, product=elem, name=pset_name
                    )
                except Exception:
                    existing_pset = model.create_entity(
                        "IfcPropertySet",
                        GlobalId=ifcopenshell.guid.new(),
                        OwnerHistory=None,
                        Name=pset_name,
                        Description=None,
                        HasProperties=[],
                    )
                    model.create_entity(
                        "IfcRelDefinesByProperties",
                        GlobalId=ifcopenshell.guid.new(),
                        OwnerHistory=None,
                        Name=None,
                        Description=None,
                        RelatedObjects=[elem],
                        RelatingPropertyDefinition=existing_pset,
                    )

            for prop_name, prop_val in props.items():
                prop_name = (prop_name or "").strip()
                if not prop_name:
                    continue
                found_prop = None
                current_props = list(existing_pset.HasProperties or [])
                for p in current_props:
                    try:
                        if (p.is_a("IfcPropertySingleValue") and
                                str(getattr(p, "Name", "") or "") == prop_name):
                            found_prop = p
                            break
                    except Exception:
                        pass
                try:
                    nominal = model.create_entity("IfcText", str(prop_val))
                except Exception:
                    try:
                        nominal = model.create_entity("IfcLabel", str(prop_val))
                    except Exception:
                        nominal = str(prop_val)
                if found_prop is not None:
                    try:
                        found_prop.NominalValue = nominal
                    except Exception:
                        pass
                else:
                    try:
                        new_prop = model.create_entity(
                            "IfcPropertySingleValue",
                            Name=prop_name,
                            Description=None,
                            NominalValue=nominal,
                            Unit=None,
                        )
                        current_props.append(new_prop)
                        existing_pset.HasProperties = current_props
                    except Exception:
                        pass

        model.write(path)
        updated_psets = {}
        try:
            updated_psets = ifcopenshell.util.element.get_psets(elem) or {}
        except Exception:
            pass
        clean_psets = {}
        for pn, pv in updated_psets.items():
            if isinstance(pv, dict):
                clean_psets[pn] = {k: str(v) for k, v in pv.items() if k != "id"}

        return JSONResponse({
            "ok":          True,
            "ifc_type":    ifc_type,
            "name":        str(getattr(elem, "Name", "") or ""),
            "object_type": str(getattr(elem, "ObjectType", "") or ""),
            "description": str(getattr(elem, "Description", "") or ""),
            "tag":         str(getattr(elem, "Tag", "") or ""),
            "psets":       clean_psets,
        })
    except Exception as exc:
        return JSONResponse({"error": f"Fehler beim Aktualisieren: {exc}"}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Datei exportieren
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/viewer/export-ifc/")
def project_viewer_export_ifc(
    request: Request,
    project_id: str,
    session_id: str = Query(...),
    slot: int       = Query(default=1),
    fmt: str        = Query(default="ifc"),
):
    """Liefert die (ggf. bearbeitete) IFC-Datei aus dem Session-Cache als Download."""
    try:
        _load_context(request, project_id)
    except NotFoundError:
        return Response(content="Projekt nicht gefunden.", status_code=404)

    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)
    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return Response(content=f"Slot {slot} nicht gefunden.", status_code=404)

    label = get_ifc_label(session_id, slot, f"model_{slot}.ifc")
    base  = label.rsplit(".", 1)[0] if "." in label else label

    with open(path, "rb") as f:
        ifc_bytes = f.read()

    if fmt == "ifczip":
        buf = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(base + ".ifc", ifc_bytes)
        buf.seek(0)
        return Response(
            content=buf.read(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{base}_bearbeitet.ifczip"'},
        )
    return Response(
        content=ifc_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{base}_bearbeitet.ifc"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Viewer-Hauptseite
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/viewer/", response_class=HTMLResponse)
def project_viewer_main(
    request: Request,
    project_id: str,
    error: str = Query(default=""),
):
    """
    3D-Viewer als integriertes Projektmodul.
    Wenn keine Modelle im Cache sind, wird direkt zur Modell-Auswahl weitergeleitet.
    """
    try:
        user, project, sid = _load_context(request, project_id)
    except NotFoundError:
        return RedirectResponse("/", status_code=302)

    pid  = _e(project_id)
    sid_e = _e(sid)
    slots = get_session_slots(sid) if session_exists(sid) else []

    # Keine Modelle geladen → direkt zur Auswahl
    if not slots:
        return RedirectResponse(
            f"/projects/{pid}/viewer/select"
            + (f"?error={urllib.parse.quote(error)}" if error else ""),
            status_code=302,
        )

    # JS-Array für Three.js Loader
    labels = {s: _e(get_ifc_label(sid, s)) for s in slots}
    model_urls_js = ",\n".join(
        f'{{url:"/projects/{pid}/viewer/file/?session_id={sid_e}&slot={s}",'
        f'label:{repr(labels[s])},slot:{s},color:{repr(_slot_color(s))}}}'
        for s in slots
    )

    error_html = f'<div class="flash-err">⚠ {_e(error)}</div>' if error else ""
    load_txt   = "IFC-Dateien werden geladen …"

    model_cards = _sidebar_model_cards(project_id, sid, slots)
    sidebar_actions = _sidebar_actions(project_id)

    # Element-Update- und Export-Endpunkte für dieses Projekt
    element_update_url = f"/projects/{pid}/viewer/element/update/"
    export_ifc_url     = f"/projects/{pid}/viewer/export-ifc/"

    # Viewer-JS aus viewer.py importieren (keine Duplizierung)
    from app.viewer import _viewer_js, _ai_chat_widget

    body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">

  {_topbar(project, sid, "viewer")}
  {error_html}

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- Sidebar -->
    <div style="width:220px;min-width:220px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px">
        Modelle
      </div>
      {sidebar_actions}
      {model_cards}

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;margin-top:2px">
        <span>IFC-Struktur</span>
        <span>
          <button id="btn-cat-all"  style="font-size:10px;cursor:pointer;color:#6af;
            background:none;border:none;padding:0">Alle</button>&nbsp;
          <button id="btn-cat-none" style="font-size:10px;cursor:pointer;color:#6af;
            background:none;border:none;padding:0">Keine</button>
        </span>
      </div>
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0">
        <div style="padding:8px 10px;font-size:11px;color:var(--muted);font-style:italic">
          {load_txt}
        </div>
      </div>
    </div>

    <!-- Canvas -->
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden">
      <canvas id="three-canvas"
        style="width:100%!important;height:100%!important;display:block"></canvas>

      <!-- GlobalId-Suchfeld -->
      <div id="search-bar" style="position:absolute;top:8px;left:8px;z-index:10;width:320px">
        <div style="display:flex;gap:4px;align-items:center">
          <input id="gid-search" type="text" placeholder="🔍 GlobalId suchen …"
            style="flex:1;background:rgba(14,20,36,.92);border:1px solid var(--border);
            color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;
            outline:none;backdrop-filter:blur(4px)"
            autocomplete="off" spellcheck="false">
          <button id="search-clear" style="display:none;background:rgba(14,20,36,.92);
            border:1px solid var(--border);color:var(--muted);border-radius:6px;
            padding:5px 8px;font-size:12px;cursor:pointer" title="Suche leeren">✕</button>
        </div>
        <div id="search-results" style="display:none;margin-top:4px;
          background:rgba(14,20,36,.97);border:1px solid var(--border);
          border-radius:6px;max-height:260px;overflow-y:auto;
          backdrop-filter:blur(4px)"></div>
      </div>

      <!-- Overlay-Buttons -->
      <div style="position:absolute;top:8px;right:8px;display:flex;gap:4px;z-index:6">
        <button id="btn-fit"   class="btn" style="font-size:11px;padding:4px 9px">⊡ Einpassen</button>
        <button id="btn-reset" class="btn" style="font-size:11px;padding:4px 9px">⟳ Kamera</button>
        <button id="btn-show-all" class="btn btn-danger"
          style="font-size:11px;padding:4px 9px;display:none">👁 Alle einblenden</button>
        <span id="hidden-count"
          style="font-size:11px;color:var(--accent2);display:none;align-self:center"></span>
      </div>
      <div style="position:absolute;bottom:8px;right:8px;font-size:10px;
        color:#445;pointer-events:none">
        LMB Drehen · MMB Pan · Rad Zoom · Leertaste: ausblenden
      </div>

      <!-- Lade-Overlay -->
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:rgba(14,14,26,.93);z-index:20">
        <div style="width:40px;height:40px;border:4px solid #0f3460;
          border-top-color:var(--accent2);border-radius:50%;
          animation:spin .7s linear infinite;margin-bottom:12px"></div>
        <p id="load-txt" style="color:#889;font-size:13px">{load_txt}</p>
      </div>
    </div>

    <!-- Info-Panel -->
    <div id="info-panel" style="width:300px;min-width:300px;background:var(--surface);
      border-left:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <span>Element-Info</span>
        <span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px"
          title="Schließen">✕</span>
      </div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:10px;font-size:12px">
        <div style="color:var(--muted);font-style:italic">
          Klick auf ein Element für Details.
        </div>
      </div>
    </div>

  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_viewer_js(model_urls_js, session_id=sid, element_update_url=element_update_url, export_ifc_url=export_ifc_url)}
</script>

{_ai_chat_widget(sid, slots)}

<script>
/* Projekt-Session bleibt erhalten; kein automatisches Löschen beim Tab-Schliessen */
(function() {{
  const SESSION_ID = "{sid_e}";
  try {{ sessionStorage.setItem("bimpruef_session", SESSION_ID); }} catch(e) {{}}
}})();
</script>"""

    return _page_wrapper(f"{project['project_name']} – Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# Redirect-Kompatibilität: alte /projects/{id}/model → neuer Viewer
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/model", response_class=HTMLResponse)
def project_model_redirect(
    request: Request,
    project_id: str,
    mode: str  = Query(default=""),
    error: str = Query(default=""),
):
    """
    Rückwärtskompatibilität: /projects/{id}/model leitet auf den neuen Viewer um.
    Der mode=select-Parameter wird auf /viewer/select abgebildet.
    """
    pid = _e(project_id)
    if mode == "select":
        dest = f"/projects/{pid}/viewer/select"
        if error:
            dest += f"?error={urllib.parse.quote(error)}"
    else:
        dest = f"/projects/{pid}/viewer/"
        if error:
            dest += f"?error={urllib.parse.quote(error)}"
    return RedirectResponse(dest, status_code=302)


@project_viewer_router.post("/projects/{project_id}/model/load")
def project_model_load_redirect(request: Request, project_id: str):
    """Rückwärtskompatibilität: POST model/load → viewer/load."""
    return RedirectResponse(
        f"/projects/{_e(project_id)}/viewer/load",
        status_code=307,  # 307 erhält die POST-Methode
    )
