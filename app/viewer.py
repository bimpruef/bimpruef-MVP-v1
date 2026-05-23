"""
viewer.py  –  BIMPruef 3D-Viewer

Routen:
  GET  /viewer/                    → Viewer-Hauptseite (3D, ohne sichtbaren Upload im Model-Modul)
  POST /viewer/upload/             → Legacy-Upload-Route (nicht mehr im Model-UI verlinkt)
  POST /viewer/remove/             → Einzelne Datei aus Session entfernen
  GET  /viewer/file/               → Rohe IFC-Datei ausliefern (immer .ifc)
  GET  /viewer/clash/              → Clash-Analyse (zwei Modelle wählen)
  GET  /viewer/clash/detail/       → Clash-Detail: nur Clash-Elemente hervorgehoben
  GET  /viewer/clash/bcf/          → Alle Clashes als BCF-ZIP
  GET  /viewer/clash/bcf-single/   → Einzelnen Clash als BCF-ZIP
  POST /viewer/element/update/     → Element-Eigenschaften (Name, Typ, PSet) aktualisieren
  GET  /viewer/export-ifc/         → Modifizierte IFC-Datei herunterladen
"""


import html
import io
import json
import os
import urllib.parse

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.storage import (
    ALLOWED_EXTENSIONS,
    MAX_FILES_PER_SESSION,
    create_upload_session,
    get_ifc_label,
    get_ifc_path,
    get_session_slots,
    load_clash_cache,
    remove_ifc_slot,
    save_clash_cache,
    save_ifc_file,
    session_exists,
)
from app.templates import build_page, render_error

router = APIRouter()


MAX_FILE_SIZE_MB    = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
BCF_CLASH_LIMIT     = int(os.environ.get("BCF_CLASH_LIMIT", "500"))


def _e(s) -> str:
    return html.escape(str(s or ""))


# ─────────────────────────────────────────────────────────────────────────────
# Slot-Farbpalette
# ─────────────────────────────────────────────────────────────────────────────
SLOT_COLORS = [
    "#4fc3f7", "#ef9a9a", "#a5d6a7", "#ffcc80",
    "#ce93d8", "#80cbc4", "#f48fb1", "#ffab40",
    "#80deea", "#bcaaa4",
]

def _slot_color(slot: int) -> str:
    return SLOT_COLORS[(slot - 1) % len(SLOT_COLORS)]


# ─────────────────────────────────────────────────────────────────────────────
# URL-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _project_url(project_id: str, area: str = "viewer", extra: str = "") -> str:
    """Return the project-scoped UI URL for a viewer-related area."""
    pid = _e(project_id)
    base_map = {
        "dashboard": f"/projects/{pid}",
        "viewer":    f"/projects/{pid}/model",
        "clash":     f"/projects/{pid}/model/clash",
        "list":      f"/projects/{pid}/list",
    }
    base = base_map.get(area, base_map["viewer"])
    if extra:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{extra}"
    return base


def _viewer_url(session_id: str, area: str = "viewer", project_id: str = "", extra: str = "") -> str:
    """Build a UI URL. Prefer project-scoped routes when project_id is known."""
    if project_id:
        return _project_url(project_id, area, extra)
    sid = _e(session_id)
    base_map = {
        "viewer":    f"/viewer/?session_id={sid}",
        "clash":     f"/viewer/clash/?session_id={sid}",
        "list":      f"/viewer/list/?session_id={sid}",
    }
    base = base_map.get(area, base_map["viewer"])
    if extra:
        base += "&" + extra
    return base


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Datei ausliefern (immer .ifc, IFCZIP wurde beim Upload bereits entpackt)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/file/")
def viewer_file(session_id: str = Query(...), slot: int = Query(default=1)):
    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)
    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return Response(content=f"Slot {slot} nicht gefunden.", status_code=404)
    with open(path, "rb") as f:
        data = f.read()
    label = get_ifc_label(session_id, slot, f"model_{slot}.ifc")
    # Dateiname für Download immer als .ifc (auch wenn Original .ifczip war)
    dl_name = label if label.lower().endswith(".ifc") else label.rsplit(".", 1)[0] + ".ifc"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upload
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/upload/")
async def viewer_upload(
    session_id: str = Form(default=""),
    project_id: str = Form(default=""),
    files: list[UploadFile] = File(...),
):
    # Project-scoped uploads are no longer allowed through Model/Viewer.
    # Documents is the only permanent upload entry point.
    if project_id:
        return RedirectResponse(
            url=f"/projects/{_e(project_id)}/documents?error="
                + urllib.parse.quote("Uploads erfolgen jetzt ausschließlich im Documents-Modul."),
            status_code=303,
        )

    errors = []

    if not session_id or not session_exists(session_id):
        session_id = create_upload_session()

    existing_slots = get_session_slots(session_id)
    next_slot      = max(existing_slots, default=0) + 1

    for uf in files:
        fname = uf.filename or ""
        lower = fname.lower()

        if not any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            errors.append(f"'{_e(fname)}': nur .ifc und .ifczip erlaubt.")
            continue

        if next_slot > MAX_FILES_PER_SESSION:
            errors.append(f"Maximale Anzahl von {MAX_FILES_PER_SESSION} Modellen erreicht.")
            break

        data = await uf.read()

        if len(data) > MAX_FILE_SIZE_BYTES:
            errors.append(f"'{_e(fname)}' überschreitet {MAX_FILE_SIZE_MB} MB.")
            continue

        try:
            save_ifc_file(session_id, next_slot, data, fname)
            next_slot += 1
        except ValueError as exc:
            errors.append(f"'{_e(fname)}': {_e(str(exc))}")
        except Exception as exc:
            errors.append(f"'{_e(fname)}': Speicherfehler – {_e(str(exc))}")

    target_url = _viewer_url(session_id, "viewer", project_id)
    if errors:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}error={urllib.parse.quote('; '.join(errors))}"
    response = RedirectResponse(url=target_url, status_code=303)
    # Kein bimpruef_session-Cookie mehr setzen – die Session-ID wird
    # ausschließlich im sessionStorage des Browsers verwaltet.
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Datei entfernen
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/remove/")
async def viewer_remove(session_id: str = Form(...), slot: int = Form(...), project_id: str = Form(default="")):
    if session_exists(session_id):
        remove_ifc_slot(session_id, slot)
    return RedirectResponse(url=_viewer_url(session_id, "viewer", project_id), status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Element-Eigenschaften aktualisieren (JSON API)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/element/update/")
async def viewer_element_update(request: Request):
    """
    Aktualisiert Eigenschaften eines IFC-Elements (Name, ObjectType, Description,
    Tag, PredefinedType und beliebige Property Sets) und speichert die
    geänderte Datei zurück in den Session-Slot.

    Request-Body (JSON):
    {
      "session_id": "...",
      "slot": 1,
      "express_id": 42,
      "changes": {
        "Name": "Neuer Name",
        "ObjectType": "...",
        "Description": "...",
        "Tag": "...",
        "PredefinedType": "...",
        "psets": {
          "Pset_WallCommon": {"LoadBearing": "true", "IsExternal": "false"},
          "MeinNeuesPset":   {"Eigenschaft1": "Wert1"}
        }
      }
    }
    """
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
        import ifcopenshell.util.element
        import ifcopenshell.api

        model  = ifcopenshell.open(path)
        elem   = model.by_id(express_id)
        if elem is None:
            return JSONResponse({"error": f"Element #{express_id} nicht gefunden."}, status_code=404)

        ifc_type = elem.is_a()

        # ── Einfache Attribute ──────────────────────────────────────────────
        # IFC-Typ (is_a) ist unveränderlich – wird hier bewusst ausgelassen.
        simple_attrs = ["Name", "ObjectType", "Description", "Tag", "PredefinedType"]
        for attr in simple_attrs:
            if attr in changes:
                val = changes[attr]
                try:
                    setattr(elem, attr, val if val != "" else None)
                except Exception:
                    pass  # Attribut existiert nicht in diesem IFC-Typ → ignorieren

        # ── Property Sets ───────────────────────────────────────────────────
        pset_changes = changes.get("psets", {})
        for pset_name, props in pset_changes.items():
            # Vorhandenes PSet suchen
            existing_pset = None
            try:
                all_psets = ifcopenshell.util.element.get_psets(elem, psets_only=True)
                if pset_name in all_psets:
                    # PSet-Objekt aus dem Modell holen
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
                # Neues PSet anlegen – erst manuell die Entität erzeugen,
                # dann mit IfcRelDefinesByProperties verknüpfen.
                try:
                    existing_pset = ifcopenshell.api.run(
                        "pset.add_pset", model, product=elem, name=pset_name
                    )
                except Exception:
                    import uuid as _uuid
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

            # Eigenschaften im PSet setzen.
            # Leere Property-Namen überspringen.
            for prop_name, prop_val in props.items():
                prop_name = (prop_name or "").strip()
                if not prop_name:
                    continue

                # Vorhandene Property suchen
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

                # Nominalwert als IfcText-Entity verpacken
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

        # ── Geändertes Modell speichern ─────────────────────────────────────
        model.write(path)

        # Aktualisierte Daten für die UI zurückgeben
        updated_psets = {}
        try:
            updated_psets = ifcopenshell.util.element.get_psets(elem) or {}
        except Exception:
            pass

        # Convert pset values to strings for JSON
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
# Modifizierte IFC-Datei exportieren
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/export-ifc/")
def viewer_export_ifc(
    session_id: str = Query(...),
    slot: int       = Query(default=1),
    fmt: str        = Query(default="ifc"),   # "ifc" oder "ifczip"
):
    """Liefert die (ggf. bearbeitete) IFC-Datei als Download."""
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
    else:
        return Response(
            content=ifc_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{base}_bearbeitet.ifc"'},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Viewer-Hauptseite
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/", response_class=HTMLResponse)
def viewer_main(request: Request, session_id: str = Query(default=""), error: str = Query(default=""), project_id: str = Query(default="")):

    # Keine Cookie-Session-Wiederverwendung mehr.
    # Falls session_id fehlt oder ungültig → neue Session erstellen.
    if not session_id or not session_exists(session_id):
        session_id = create_upload_session()

    sid    = _e(session_id)
    slots  = get_session_slots(session_id)
    labels = {s: _e(get_ifc_label(session_id, s)) for s in slots}

    # JS-Array mit Modell-URLs
    model_urls_js = ",\n".join(
        f'{{url:"/viewer/file/?session_id={sid}&slot={s}",'
        f'label:{repr(labels[s])},slot:{s},color:{repr(_slot_color(s))}}}'
        for s in slots
    )

    # Hinweis-Banner wenn leer – Upload läuft jetzt ausschließlich über Documents.
    if slots:
        notice_html = ""
    elif project_id:
        notice_html = (
            f'<div class="bp-viewer-notice bp-viewer-notice--info">'
            f'Keine IFC-Modelle im Viewer-Cache geladen. Lade Modelle aus Documents.'
            f'<a class="bp-btn bp-btn--primary bp-btn--sm" '
            f'href="/projects/{_e(project_id)}/model?mode=select">Modelle auswählen</a>'
            f'<a class="bp-btn bp-btn--secondary bp-btn--sm" '
            f'href="/projects/{_e(project_id)}/documents">Zu Documents</a>'
            f'</div>'
        )
    else:
        notice_html = (
            '<div class="bp-viewer-notice bp-viewer-notice--info">'
            'Keine Modelle geladen.'
            '</div>'
        )

    error_html = (
        f'<div class="bp-viewer-notice bp-viewer-notice--error">'
        f'<svg width="14" height="14" fill="none" stroke="currentColor" viewBox="0 0 24 24">'
        f'<circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/>'
        f'<line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
        f' {_e(error)}'
        f'</div>'
    ) if error else ""

    # Sidebar: Modell-Karten
    model_cards = ""
    for s in slots:
        col = _slot_color(s)
        lbl = labels[s]
        escaped_lbl = lbl.replace("'", "\\'").replace("\\", "\\\\")
        model_cards += (
            f'<div class="bp-viewer-model-card">'
            f'<label class="bp-viewer-model-label">'
            f'<input type="checkbox" class="chk-model" data-slot="{s}" checked '
            f'style="accent-color:{col};width:13px;height:13px;flex-shrink:0">'
            f'<span class="bp-viewer-model-dot" style="border-color:{col}"></span>'
            f'<span class="bp-viewer-model-name" title="{lbl}">{lbl}</span>'
            f'<form method="post" action="/viewer/remove/" style="display:inline;margin:0" '
            f'onsubmit="return confirm(\'Datei \\'{escaped_lbl}\\' wirklich schließen?\')">'
            f'<input type="hidden" name="session_id" value="{sid}">'
            f'<input type="hidden" name="project_id" value="{_e(project_id)}">'
            f'<input type="hidden" name="slot" value="{s}">'
            f'<button type="submit" class="bp-viewer-remove-btn" title="Entfernen">✕</button>'
            f'</form>'
            f'</label>'
            f'</div>'
        )

    # Sidebar: Upload / Navigation-Bereich
    if project_id:
        upload_html = (
            f'<div class="bp-viewer-sidebar-actions">'
            f'<a class="bp-btn bp-btn--primary bp-btn--sm bp-w-full" '
            f'href="/projects/{_e(project_id)}/model?mode=select">Modelle aus Documents laden</a>'
            f'<a class="bp-btn bp-btn--secondary bp-btn--sm bp-w-full" '
            f'href="/projects/{_e(project_id)}/documents">Zu Documents</a>'
            f'<p class="bp-viewer-sidebar-hint">Uploads erfolgen ausschließlich im Documents-Modul.</p>'
            f'</div>'
        )
    else:
        upload_html = ""

    load_txt = "IFC-Dateien werden geladen …" if slots else "Keine Modelle aus Documents geladen."

    # Das gesamte Viewer-Body ist eine vollflächige Shell ohne bp-container.
    # build_page() wird mit container=False aufgerufen.
    body = f"""
<div class="bp-viewer-shell">

  {error_html}{notice_html}

  <div class="bp-viewer-workspace">

    <!-- Sidebar links -->
    <aside class="bp-viewer-sidebar">

      <div class="bp-viewer-sidebar-section-head">
        <span>Modelle</span>
      </div>
      {upload_html}
      {model_cards}

      <div class="bp-viewer-sidebar-section-head bp-mt-sm">
        <span>IFC-Struktur</span>
        <span class="bp-viewer-cat-controls">
          <button id="btn-cat-all"  class="bp-viewer-cat-btn">Alle</button>
          <button id="btn-cat-none" class="bp-viewer-cat-btn">Keine</button>
        </span>
      </div>
      <div id="cat-scroll" class="bp-viewer-cat-scroll">
        <div class="bp-viewer-cat-empty">{load_txt}</div>
      </div>
    </aside>

    <!-- Canvas-Bereich -->
    <div id="canvas-wrap" class="bp-viewer-canvas-wrap">
      <canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>

      <!-- GlobalId-Suchfeld -->
      <div id="search-bar" class="bp-viewer-search-bar">
        <div class="bp-viewer-search-row">
          <input id="gid-search" type="text" placeholder="🔍 GlobalId suchen …"
            class="bp-viewer-search-input"
            autocomplete="off" spellcheck="false">
          <button id="search-clear" class="bp-viewer-search-clear" style="display:none" title="Suche leeren">✕</button>
        </div>
        <div id="search-results" class="bp-viewer-search-results" style="display:none"></div>
      </div>

      <!-- Overlay-Buttons -->
      <div class="bp-viewer-overlay-btns">
        <button id="btn-fit"   class="btn bp-viewer-overlay-btn">⊡ Einpassen</button>
        <button id="btn-reset" class="btn bp-viewer-overlay-btn">⟳ Kamera</button>
        <button id="btn-show-all" class="btn btn-danger bp-viewer-overlay-btn" style="display:none">👁 Alle einblenden</button>
        <span id="hidden-count" class="bp-viewer-hidden-count" style="display:none"></span>
      </div>
      <div class="bp-viewer-hint">LMB Drehen · MMB Pan · Rad Zoom · Leertaste: ausblenden</div>

      <!-- Lade-Overlay -->
      <div id="loading" class="bp-viewer-loading-overlay">
        <div class="bp-viewer-spinner"></div>
        <p id="load-txt" class="bp-viewer-loading-txt">{load_txt}</p>
      </div>
    </div>

    <!-- Info-Panel rechts -->
    <div id="info-panel" class="bp-viewer-info-panel">
      <div class="bp-viewer-panel-head">
        <span>Element-Info</span>
        <span id="info-close" class="bp-viewer-panel-close" title="Schließen">✕</span>
      </div>
      <div id="info-body" class="bp-viewer-panel-body">
        <div class="bp-viewer-panel-placeholder">Klick auf ein Element für Details.</div>
      </div>
    </div>

  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_viewer_js(model_urls_js, session_id=session_id)}
</script>

<script>
/* ── Session-Isolierung: Nur anonyme Legacy-Sessions beim Schließen löschen.
   Projektbezogene Model-Sessions sind Viewer-Caches für Documents und dürfen
   beim Navigieren zu Clash/Liste/Rulecheck nicht automatisch gelöscht werden. */
(function() {{
  const SESSION_ID = "{_e(session_id)}";
  const PROJECT_ID = "{_e(project_id)}";
  if (PROJECT_ID) {{
    try {{ sessionStorage.setItem("bimpruef_session", SESSION_ID); }} catch(e) {{}}
    return;
  }}

  try {{ sessionStorage.setItem("bimpruef_session", SESSION_ID); }} catch(e) {{}}

  function deleteSession() {{
    const url = "/session/delete/";
    const body = JSON.stringify({{session_id: SESSION_ID}});
    if (navigator.sendBeacon) {{
      const blob = new Blob([body], {{type: "application/json"}});
      navigator.sendBeacon(url, blob);
    }} else {{
      try {{
        const xhr = new XMLHttpRequest();
        xhr.open("DELETE", url, false);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.send(body);
      }} catch(e) {{}}
    }}
  }}

  window.addEventListener("pagehide", function(e) {{
    if (!e.persisted) {{ deleteSession(); }}
  }});
  window.addEventListener("beforeunload", function() {{
    deleteSession();
  }});
}})();
</script>"""

    return build_page(
        title="3D-Viewer",
        body_html=body,
        active_nav="projects",
        container=False,
        extra_head='<style>.bp-main{padding:0!important;overflow:hidden}</style>',
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsamer 3D-Viewer JavaScript-Block (unverändert)
# ─────────────────────────────────────────────────────────────────────────────

def _viewer_js(model_urls_js: str, highlight_gids: list = None, session_id: str = "") -> str:
    """
    Vollständiger JS-Block für den 3D-Viewer.
    model_urls_js: komma-getrennter JS-Array-Inhalt mit {url, label, slot, color}
    highlight_gids: wenn gesetzt → alle anderen Elemente werden auf 5 % Opacity gedimmt
    session_id: wird als globale JS-Variable SESSION_ID eingebettet
    """

    if highlight_gids:
        gids_json = "[" + ",".join(f'"{g}"' for g in highlight_gids) + "]"
        highlight_block = f"""
const HIGHLIGHT_GIDS = new Set({gids_json});
function applyClashHighlight() {{
  // Kollisionselemente: Element 0 = leuchtendes Rot, Element 1 = leuchtendes Gelb
  const _clashArr = Array.from(HIGHLIGHT_GIDS);
  const _clashColorMap = {{}};
  _clashArr.forEach((gid, i) => {{
    _clashColorMap[gid] = i === 0 ? new THREE.Color(0xff3333) : new THREE.Color(0xffcc00);
  }});

  const sceneMeshes = [];
  scene.traverse(obj => {{ if (obj.isMesh) sceneMeshes.push(obj); }});

  for (const m of sceneMeshes) {{
    const gid = m.userData && m.userData.globalId;
    if (gid && HIGHLIGHT_GIDS.has(gid)) {{
      m.material.color.set(_clashColorMap[gid] || new THREE.Color(0xff3333));
      m.material.opacity = 1.0;
      m.material.transparent = false;
      m.material.wireframe = false;
      m.material.needsUpdate = true;
      m.visible = true;
    }} else {{
      if (m.userData && m.userData.globalId !== undefined) {{
        m.material.opacity = 0.04;
        m.material.transparent = true;
        m.material.wireframe = false;
        m.material.needsUpdate = true;
        m.visible = true;
      }}
    }}
  }}
  const box = new THREE.Box3();
  for (const m of sceneMeshes) {{
    if (m.userData && HIGHLIGHT_GIDS.has(m.userData.globalId)) box.expandByObject(m);
  }}
  if (!box.isEmpty()) {{
    const center = box.getCenter(new THREE.Vector3());
    const size   = box.getSize(new THREE.Vector3());
    orb.tgt.copy(center);
    orb.sph.radius = Math.max(size.x, size.y, size.z) * 4;
    applyOrb();
  }}
}}"""
    else:
        highlight_block = "function applyClashHighlight() {}"

    sid_js = repr(session_id)
    return f"const SESSION_ID = {sid_js};\n" + r"""
// ═══════════════════════════════════════════════════════════════════════════
// IFC-Farbpalette
// ═══════════════════════════════════════════════════════════════════════════
const TYPE_COLOR = {
  IfcWall:0xc8a057, IfcWallStandardCase:0xc8a057, IfcCurtainWall:0xf0c040,
  IfcColumn:0x2e6b9e, IfcColumnStandardCase:0x2e6b9e,
  IfcBeam:0x5b9bd5, IfcBeamStandardCase:0x5b9bd5,
  IfcSlab:0x7aaec8, IfcSlabStandardCase:0x7aaec8,
  IfcRoof:0x8b5de5, IfcDoor:0xe07040, IfcWindow:0x70d8f0,
  IfcStair:0xc87050, IfcStairFlight:0xc87050,
  IfcRamp:0xd4a030, IfcRailing:0x607080,
  IfcPlate:0x90b8d0, IfcMember:0x4898b0, IfcCovering:0x88b878,
  IfcFooting:0x2a5070, IfcPile:0x1e3850, IfcChimney:0x8b5e3c,
  IfcBuildingElementProxy:0x888888,
  IfcFurnishingElement:0xe08860, IfcFurniture:0xe08860,
  IfcReinforcingBar:0x708090,
  IfcSite:0x48a048, IfcBuilding:0x60b060,
  IfcBuildingStorey:0x80c880, IfcSpace:0xa8d8a8,
  IfcPipeSegment:0x188880, IfcDuctSegment:0x607080,
  IfcCableSegment:0xe09810, IfcPump:0x3040a0,
  IfcFan:0x4858a8, IfcValve:0x50b898, IfcSensor:0xe080a8,
};
const TYPE_FALLBACK = [
  [/^IfcWall/,0xc8a057],[/^IfcSlab/,0x7aaec8],[/^IfcColumn/,0x2e6b9e],
  [/^IfcBeam/,0x5b9bd5],[/^IfcStair/,0xc87050],[/^IfcRoof/,0x8b5de5],
  [/^IfcDoor/,0xe07040],[/^IfcWindow/,0x70d8f0],[/^IfcPipe/,0x188880],
  [/^IfcDuct/,0x607080],[/^IfcCable/,0xe09810],[/^IfcPump/,0x3040a0],
  [/^IfcFan/,0x4858a8],[/^IfcFurnish/,0xe08860],[/^IfcFurniture/,0xe08860],
  [/^IfcElectric/,0xe0b830],[/^IfcSanitary/,0x30a8d0],[/^IfcBoiler/,0xe05828],
];
const TYPE_COLOR_LOWER = {};
for (const k of Object.keys(TYPE_COLOR))
  TYPE_COLOR_LOWER[k.toLowerCase()] = TYPE_COLOR[k];

function getColor(t) {
  if (TYPE_COLOR[t] !== undefined) return new THREE.Color(TYPE_COLOR[t]);
  const l = t.toLowerCase();
  if (TYPE_COLOR_LOWER[l] !== undefined) return new THREE.Color(TYPE_COLOR_LOWER[l]);
  for (const [rx, hex] of TYPE_FALLBACK) if (rx.test(t)) return new THREE.Color(hex);
  return new THREE.Color(0x777788);
}

// ═══════════════════════════════════════════════════════════════════════════
// Three.js Setup
// ═══════════════════════════════════════════════════════════════════════════
const canvas    = document.getElementById("three-canvas");
const wrap      = document.getElementById("canvas-wrap");
const loadingEl = document.getElementById("loading");
const loadTxtEl = document.getElementById("load-txt");
const infoBody  = document.getElementById("info-body");
const infoPanel = document.getElementById("info-panel");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e0e1a);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);
camera.position.set(20, 20, 20);

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dirL = new THREE.DirectionalLight(0xffffff, 0.85);
dirL.position.set(60, 100, 60);
scene.add(dirL);
scene.add(new THREE.HemisphereLight(0xddeeff, 0x100c08, 0.3));

const gridHelper = new THREE.GridHelper(200, 40, 0x1a2a3a, 0x151f30);
scene.add(gridHelper);

function onResize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", onResize);
onResize();

// ── Orbit-Steuerung ─────────────────────────────────────────────────────────
const orb = {
  sph: new THREE.Spherical(80, Math.PI / 4, Math.PI / 4),
  tgt: new THREE.Vector3(),
  drag: false, pan: false, lx: 0, ly: 0,
};
function applyOrb() {
  camera.position.copy(
    new THREE.Vector3().setFromSpherical(orb.sph).add(orb.tgt)
  );
  camera.lookAt(orb.tgt);
}
applyOrb();

canvas.addEventListener("mousedown", e => {
  if (e.button === 0) orb.drag = true;
  if (e.button === 1) { orb.pan = true; e.preventDefault(); }
  orb.lx = e.clientX; orb.ly = e.clientY;
});
window.addEventListener("mouseup", () => { orb.drag = false; orb.pan = false; });
window.addEventListener("mousemove", e => {
  const dx = e.clientX - orb.lx, dy = e.clientY - orb.ly;
  orb.lx = e.clientX; orb.ly = e.clientY;
  if (orb.drag) {
    orb.sph.theta -= dx * 0.005;
    orb.sph.phi   = Math.max(0.05, Math.min(Math.PI - 0.05, orb.sph.phi + dy * 0.005));
    applyOrb();
  }
  if (orb.pan) {
    const right = new THREE.Vector3().crossVectors(
      camera.getWorldDirection(new THREE.Vector3()), camera.up
    ).normalize();
    const up = camera.up.clone().normalize();
    const f  = orb.sph.radius * 0.0012;
    orb.tgt.addScaledVector(right, -dx * f);
    orb.tgt.addScaledVector(up,     dy * f);
    applyOrb();
  }
});
canvas.addEventListener("wheel", e => {
  e.preventDefault();
  orb.sph.radius = Math.max(0.5, orb.sph.radius * (1 + e.deltaY * 0.001));
  applyOrb();
}, { passive: false });

// ── Render-Loop ──────────────────────────────────────────────────────────────
(function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); })();

// ── Touch-Steuerung ──────────────────────────────────────────────────────────
let _touches = [];
canvas.addEventListener("touchstart", e => {
  e.preventDefault();
  _touches = Array.from(e.touches);
}, { passive: false });
canvas.addEventListener("touchmove", e => {
  e.preventDefault();
  const t = Array.from(e.touches);
  if (t.length === 1 && _touches.length === 1) {
    const dx = t[0].clientX - _touches[0].clientX;
    const dy = t[0].clientY - _touches[0].clientY;
    orb.sph.theta -= dx * 0.005;
    orb.sph.phi = Math.max(0.05, Math.min(Math.PI - 0.05, orb.sph.phi + dy * 0.005));
    applyOrb();
  } else if (t.length === 2 && _touches.length === 2) {
    const d0 = Math.hypot(_touches[0].clientX - _touches[1].clientX, _touches[0].clientY - _touches[1].clientY);
    const d1 = Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
    orb.sph.radius = Math.max(0.5, orb.sph.radius * (d0 / d1));
    applyOrb();
  }
  _touches = t;
}, { passive: false });
canvas.addEventListener("touchend", e => { _touches = Array.from(e.touches); }, { passive: false });

// ═══════════════════════════════════════════════════════════════════════════
// IFC-Lader (web-ifc)
// ═══════════════════════════════════════════════════════════════════════════
let WEBIFC = null;
let ifcApi = null;

async function initWebIfc() {
  if (typeof WebIFC === "undefined") {
    await new Promise((res, rej) => {
      const s = document.createElement("script");
      s.src = "https://cdn.jsdelivr.net/npm/web-ifc@0.0.44/web-ifc-api.js";
      s.onload = res; s.onerror = rej;
      document.head.appendChild(s);
    });
  }
  WEBIFC = WebIFC;
  ifcApi = new WEBIFC.IfcAPI();
  ifcApi.SetWasmPath("https://cdn.jsdelivr.net/npm/web-ifc@0.0.44/");
  await ifcApi.Init();
}

// ── Element-Index ────────────────────────────────────────────────────────────
const elementIndex = [];   // {globalId, name, ifcType, slotColor}
const modelMeshes  = {};   // slot → THREE.Group

function allMeshes() {
  const out = [];
  for (const g of Object.values(modelMeshes))
    g.traverse(o => { if (o.isMesh) out.push(o); });
  return out;
}

// ── Modell laden ─────────────────────────────────────────────────────────────
async function loadModel(cfg) {
  if (loadTxtEl) loadTxtEl.textContent = `Lade ${cfg.label} …`;

  const res  = await fetch(cfg.url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const buf  = await res.arrayBuffer();
  const data = new Uint8Array(buf);

  const modelID = ifcApi.OpenModel(data);
  const group   = new THREE.Group();
  modelMeshes[cfg.slot] = group;
  scene.add(group);

  const flatMeshes = ifcApi.LoadAllGeometry(modelID);
  const tmpMat = new THREE.MeshPhongMaterial({ side: THREE.DoubleSide, transparent: true, opacity: 0.95 });

  for (let i = 0; i < flatMeshes.size(); i++) {
    const fm    = flatMeshes.get(i);
    const geoms = fm.geometries;
    const gid   = String(fm.expressID);
    let ifc_type = "";
    let elem_name = "";
    let globalId = gid;
    try {
      const line = ifcApi.GetLine(modelID, fm.expressID, false);
      ifc_type  = line ? ifcApi.GetNameFromTypeCode(line.type) : "";
      elem_name = line && line.Name && line.Name.value ? String(line.Name.value) : "";
      globalId  = line && line.GlobalId && line.GlobalId.value ? String(line.GlobalId.value) : gid;
    } catch {}

    const mat = tmpMat.clone();
    mat.color = getColor(ifc_type || "");

    for (let g = 0; g < geoms.size(); g++) {
      const geomData = geoms.get(g);
      const geom = new THREE.BufferGeometry();
      const vb   = geomData.geometryExpressID >= 0
        ? ifcApi.GetGeometry(modelID, geomData.geometryExpressID)
        : null;
      if (!vb) continue;
      const verts  = ifcApi.GetVertexArray(vb.GetVertexData(), vb.GetVertexDataSize());
      const idx    = ifcApi.GetIndexArray(vb.GetIndexData(), vb.GetIndexDataSize());
      const pos    = new Float32Array(verts.length / 2);
      const nrm    = new Float32Array(verts.length / 2);
      for (let k = 0; k < verts.length; k += 6) {
        const off = k / 2;
        pos[off]   = verts[k]; pos[off+1] = verts[k+1]; pos[off+2] = verts[k+2];
        nrm[off]   = verts[k+3]; nrm[off+1] = verts[k+4]; nrm[off+2] = verts[k+5];
      }
      geom.setAttribute("position", new THREE.BufferAttribute(pos, 3));
      geom.setAttribute("normal",   new THREE.BufferAttribute(nrm, 3));
      geom.setIndex(new THREE.BufferAttribute(idx, 1));

      const m4 = geomData.flatTransformation;
      const matrix = new THREE.Matrix4().fromArray([
        m4.x[0],m4.x[1],m4.x[2],m4.x[3],
        m4.y[0],m4.y[1],m4.y[2],m4.y[3],
        m4.z[0],m4.z[1],m4.z[2],m4.z[3],
        m4.w[0],m4.w[1],m4.w[2],m4.w[3],
      ]);

      const mesh = new THREE.Mesh(geom, mat.clone());
      mesh.applyMatrix4(matrix);
      mesh.userData = { slot: cfg.slot, expressID: fm.expressID, globalId, ifcType: ifc_type, name: elem_name, slotColor: cfg.color };
      group.add(mesh);

      if (g === 0) {
        elementIndex.push({ globalId, name: elem_name, ifcType: ifc_type, slotColor: cfg.color });
      }
    }
  }
  ifcApi.CloseModel(modelID);
}

// ═══════════════════════════════════════════════════════════════════════════
// Kategorie-UI
// ═══════════════════════════════════════════════════════════════════════════
function buildCategoryUI() {
  const catScroll = document.getElementById("cat-scroll");
  if (!catScroll) return;
  catScroll.innerHTML = "";

  const typeMap = {};
  for (const m of allMeshes()) {
    const t = m.userData.ifcType || "Unknown";
    if (!typeMap[t]) typeMap[t] = { count: 0, meshes: [], color: getColor(t) };
    typeMap[t].count++;
    typeMap[t].meshes.push(m);
  }

  const sorted = Object.entries(typeMap).sort((a, b) => b[1].count - a[1].count);
  for (const [type, info] of sorted) {
    const col = "#" + info.color.getHexString();
    const div = document.createElement("div");
    div.className = "cat-item";
    div.innerHTML =
      `<input type="checkbox" checked style="accent-color:${col};width:12px;height:12px;flex-shrink:0">` +
      `<span style="width:8px;height:8px;border-radius:50%;background:${col};flex-shrink:0"></span>` +
      `<span style="flex:1;font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(type)}</span>` +
      `<span style="font-size:10px;color:#4a6080;flex-shrink:0">${info.count}</span>`;
    const cb = div.querySelector("input");
    cb.addEventListener("change", () => {
      for (const m of info.meshes) m.visible = cb.checked;
    });
    catScroll.appendChild(div);
  }

  document.getElementById("btn-cat-all")?.addEventListener("click", () => {
    catScroll.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.checked = true;
      cb.dispatchEvent(new Event("change"));
    });
  });
  document.getElementById("btn-cat-none")?.addEventListener("click", () => {
    catScroll.querySelectorAll("input[type=checkbox]").forEach(cb => {
      cb.checked = false;
      cb.dispatchEvent(new Event("change"));
    });
  });
}

// ── Modell-Checkboxen ────────────────────────────────────────────────────────
document.querySelectorAll(".chk-model").forEach(cb => {
  cb.addEventListener("change", () => {
    const slot = parseInt(cb.dataset.slot);
    const g = modelMeshes[slot];
    if (g) g.visible = cb.checked;
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// Kamera-Steuerung
// ═══════════════════════════════════════════════════════════════════════════
function fitAll() {
  const box = new THREE.Box3();
  scene.traverse(obj => { if (obj.isMesh && obj.visible) box.expandByObject(obj); });
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size   = box.getSize(new THREE.Vector3());
  orb.tgt.copy(center);
  orb.sph.radius = Math.max(size.x, size.y, size.z) * 2.2;
  orb.sph.phi   = Math.PI / 4;
  orb.sph.theta = Math.PI / 4;
  applyOrb();
}

document.getElementById("btn-fit")?.addEventListener("click", fitAll);
document.getElementById("btn-reset")?.addEventListener("click", () => {
  orb.sph = new THREE.Spherical(80, Math.PI / 4, Math.PI / 4);
  orb.tgt.set(0, 0, 0);
  applyOrb();
});

// ═══════════════════════════════════════════════════════════════════════════
// Element-Auswahl & Info-Panel
// ═══════════════════════════════════════════════════════════════════════════
let selectedMesh = null;
let originalColor = null;
const HIGHLIGHT = new THREE.Color(0x00e5ff);

function esc(s) {
  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

function selectMesh(mesh) {
  if (selectedMesh && originalColor) {
    selectedMesh.material.color.copy(originalColor);
  }
  selectedMesh  = mesh;
  originalColor = mesh.material.color.clone();
  mesh.material.color.copy(HIGHLIGHT);
  showInfo(mesh);
}

function showInfo(mesh) {
  if (!infoBody) return;
  const ud = mesh.userData;
  const sid = SESSION_ID;

  let html = `<div class="bp-viewer-info-header">
    <span class="bp-badge bp-badge--accent" style="font-size:10px">${esc(ud.ifcType)}</span>
    <span class="bp-badge bp-badge--default" style="font-size:10px">Slot ${esc(ud.slot)}</span>
  </div>`;

  html += `<div class="bp-pset" style="margin-bottom:8px">
    <div class="bp-pset__title">Basisattribute</div>
    <table>
      <tr><td>Name</td><td id="attr-Name">${esc(ud.name)}</td></tr>
      <tr><td>GlobalId</td><td style="font-size:10px;font-family:monospace">${esc(ud.globalId)}</td></tr>
      <tr><td>ExpressID</td><td>${esc(ud.expressID)}</td></tr>
    </table>
  </div>`;

  html += `<details open class="bp-viewer-details">
    <summary class="bp-viewer-details-summary">Eigenschaften bearbeiten</summary>
    <div class="bp-viewer-edit-form" id="edit-form-${esc(ud.expressID)}">
      <div class="bp-field bp-mb-sm">
        <label class="bp-label">Name</label>
        <input class="bp-input bp-input--sm" id="edit-Name" value="${esc(ud.name)}">
      </div>
      <div class="bp-field bp-mb-sm">
        <label class="bp-label">ObjectType</label>
        <input class="bp-input bp-input--sm" id="edit-ObjectType" value="">
      </div>
      <div class="bp-field bp-mb-sm">
        <label class="bp-label">Description</label>
        <input class="bp-input bp-input--sm" id="edit-Description" value="">
      </div>
      <div class="bp-field bp-mb-sm">
        <label class="bp-label">Tag</label>
        <input class="bp-input bp-input--sm" id="edit-Tag" value="">
      </div>
      <div id="pset-edit-area"></div>
      <button class="bp-btn bp-btn--primary bp-btn--sm" id="btn-save-attrs">Speichern</button>
      <span id="save-status" style="font-size:11px;margin-left:6px"></span>
    </div>
  </details>`;

  infoBody.innerHTML = html;
  if (infoPanel) infoPanel.style.display = "flex";

  // Schließen-Button
  document.getElementById("info-close")?.addEventListener("click", () => {
    if (infoPanel) infoPanel.style.display = "none";
    if (selectedMesh && originalColor) selectedMesh.material.color.copy(originalColor);
    selectedMesh = null; originalColor = null;
  });

  // PSet-Bereich nachladen via element/update API (GET-ähnlich: wir senden leere changes)
  fetch("/viewer/element/update/", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sid, slot: ud.slot, express_id: ud.expressID, changes: {} }),
  })
  .then(r => r.json())
  .then(data => {
    if (!data.ok) return;
    const nameEl = document.getElementById("edit-Name");
    if (nameEl) nameEl.value = data.name || "";
    const otEl = document.getElementById("edit-ObjectType");
    if (otEl) otEl.value = data.object_type || "";
    const descEl = document.getElementById("edit-Description");
    if (descEl) descEl.value = data.description || "";
    const tagEl = document.getElementById("edit-Tag");
    if (tagEl) tagEl.value = data.tag || "";

    const psetArea = document.getElementById("pset-edit-area");
    if (psetArea) {
      let psetHtml = "";
      for (const [psetName, props] of Object.entries(data.psets || {})) {
        const safePs = esc(psetName);
        psetHtml += `<div class="bp-pset" style="margin-bottom:6px">
          <div class="bp-pset__title">${safePs}</div>
          <table>`;
        for (const [k, v] of Object.entries(props)) {
          const safeK = esc(k);
          const safeV = esc(String(v));
          const inputId = `pset__${safePs}__${safeK}`.replace(/[^a-zA-Z0-9_]/g, "_");
          psetHtml += `<tr>
            <td style="width:45%">${safeK}</td>
            <td><input class="bp-input bp-input--sm pset-prop-input"
              data-pset="${safePs}" data-prop="${safeK}"
              id="${inputId}" value="${safeV}"></td>
          </tr>`;
        }
        psetHtml += `</table></div>`;
      }
      psetArea.innerHTML = psetHtml;
    }
  })
  .catch(() => {});

  // Speichern-Handler
  document.getElementById("btn-save-attrs")?.addEventListener("click", () => {
    const changes = {
      Name:        (document.getElementById("edit-Name")?.value        ?? ""),
      ObjectType:  (document.getElementById("edit-ObjectType")?.value  ?? ""),
      Description: (document.getElementById("edit-Description")?.value ?? ""),
      Tag:         (document.getElementById("edit-Tag")?.value         ?? ""),
      psets: {},
    };
    document.querySelectorAll(".pset-prop-input").forEach(inp => {
      const ps = inp.dataset.pset;
      const pk = inp.dataset.prop;
      if (!changes.psets[ps]) changes.psets[ps] = {};
      changes.psets[ps][pk] = inp.value;
    });
    const statusEl = document.getElementById("save-status");
    if (statusEl) statusEl.textContent = "Speichern …";
    fetch("/viewer/element/update/", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sid, slot: ud.slot, express_id: ud.expressID, changes }),
    })
    .then(r => r.json())
    .then(data => {
      if (statusEl) statusEl.textContent = data.ok ? "✓ Gespeichert" : ("Fehler: " + (data.error || "?"));
      if (data.ok) {
        mesh.userData.name = data.name;
        const attrNameEl = document.getElementById("attr-Name");
        if (attrNameEl) attrNameEl.textContent = data.name;
      }
      setTimeout(() => { if (statusEl) statusEl.textContent = ""; }, 3000);
    })
    .catch(err => { if (statusEl) statusEl.textContent = "Fehler: " + err; });
  });
}

// ── Raycaster ────────────────────────────────────────────────────────────────
const raycaster = new THREE.Raycaster();
const mouse     = new THREE.Vector2();
let   _mouseHasMoved = false;

canvas.addEventListener("mousemove", () => { _mouseHasMoved = true; });
canvas.addEventListener("mousedown", () => { _mouseHasMoved = false; });
canvas.addEventListener("mouseup", e => {
  if (!_mouseHasMoved && e.button === 0) {
    const rect = canvas.getBoundingClientRect();
    mouse.set(
      ((e.clientX - rect.left) / rect.width)  * 2 - 1,
      -((e.clientY - rect.top) / rect.height) * 2 + 1
    );
    raycaster.setFromCamera(mouse, camera);
    const hits = raycaster.intersectObjects(allMeshes(), false);
    if (hits.length > 0) {
      selectMesh(hits[0].object);
    } else {
      if (selectedMesh && originalColor) selectedMesh.material.color.copy(originalColor);
      selectedMesh = null; originalColor = null;
      if (infoBody) infoBody.innerHTML = '<div class="bp-viewer-panel-placeholder">Klick auf ein Element für Details.</div>';
    }
  }
});

// ── Ausblenden per Leertaste ──────────────────────────────────────────────────
const hiddenMeshes = new Set();
const hiddenCountEl = document.getElementById("hidden-count");
const showAllBtn    = document.getElementById("btn-show-all");

function updateHiddenUI() {
  const n = hiddenMeshes.size;
  if (hiddenCountEl) {
    hiddenCountEl.textContent = n > 0 ? `${n} ausgeblendet` : "";
    hiddenCountEl.style.display = n > 0 ? "inline" : "none";
  }
  if (showAllBtn) showAllBtn.style.display = n > 0 ? "inline-flex" : "none";
}

window.addEventListener("keydown", e => {
  if (e.code === "Space" && selectedMesh) {
    e.preventDefault();
    selectedMesh.visible = false;
    hiddenMeshes.add(selectedMesh);
    if (originalColor) selectedMesh.material.color.copy(originalColor);
    selectedMesh = null; originalColor = null;
    if (infoBody) infoBody.innerHTML = '<div class="bp-viewer-panel-placeholder">Element ausgeblendet.</div>';
    updateHiddenUI();
  }
});

showAllBtn?.addEventListener("click", () => {
  for (const m of hiddenMeshes) m.visible = true;
  hiddenMeshes.clear();
  updateHiddenUI();
});

// ── Fly-to-GIDs ──────────────────────────────────────────────────────────────
function flyToGids(gids) {
  const box = new THREE.Box3();
  for (const m of allMeshes()) {
    if (gids.includes(m.userData.globalId)) box.expandByObject(m);
  }
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size   = box.getSize(new THREE.Vector3());
  orb.tgt.copy(center);
  orb.sph.radius = Math.max(size.x, size.y, size.z) * 3.5;
  applyOrb();
}

// ═══════════════════════════════════════════════════════════════════════════
// Suchfeld
// ═══════════════════════════════════════════════════════════════════════════
const searchInput   = document.getElementById("gid-search");
const searchResults = document.getElementById("search-results");
const searchClear   = document.getElementById("search-clear");
let   searchHighlighted = [];

function buildSearchIndex() {}  // Index ist bereits im elementIndex Array

function clearSearchHighlight() {
  for (const m of searchHighlighted) {
    if (m._origSearchColor) m.material.color.copy(m._origSearchColor);
    delete m._origSearchColor;
  }
  searchHighlighted = [];
}

function applySearchHighlight(gids) {
  clearSearchHighlight();
  const gidSet = new Set(gids);
  for (const m of allMeshes()) {
    if (gidSet.has(m.userData.globalId)) {
      m._origSearchColor = m.material.color.clone();
      m.material.color.set(0xffeb3b);
      searchHighlighted.push(m);
    }
  }
}

function renderSearchResults(q) {
  if (searchClear) searchClear.style.display = q ? "inline-flex" : "none";
  if (!q || q.length < 2) {
    if (searchResults) searchResults.style.display = "none";
    clearSearchHighlight();
    return;
  }
  q = q.toLowerCase();
  const hits = elementIndex.filter(el =>
    el.globalId.toLowerCase().includes(q) ||
    (el.name && el.name.toLowerCase().includes(q)) ||
    (el.ifcType && el.ifcType.toLowerCase().includes(q))
  );

  if (!searchResults) return;
  searchResults.innerHTML = "";

  if (hits.length === 0) {
    searchResults.style.display = "block";
    searchResults.innerHTML = '<div class="bp-viewer-search-empty">Keine Treffer</div>';
    clearSearchHighlight();
    return;
  }

  const header = document.createElement("div");
  header.className = "bp-viewer-search-header";
  header.innerHTML =
    `<span>${hits.length} Element${hits.length !== 1 ? "e" : ""} gefunden</span>` +
    `<span class="bp-viewer-search-header-note">${hits.length > 50 ? "ersten 50 angezeigt" : ""}</span>`;
  searchResults.appendChild(header);

  const visible50 = hits.slice(0, 50);
  const matchGids = hits.map(h => h.globalId);

  applySearchHighlight(matchGids);
  if (hits.length <= 5) flyToGids(matchGids);

  for (const el of visible50) {
    const tCol = "#" + getColor(el.ifcType).getHexString();
    const row  = document.createElement("div");
    row.className = "bp-viewer-search-row-item";
    row.innerHTML =
      `<div style="display:flex;align-items:center;gap:5px">` +
        `<span style="width:7px;height:7px;border-radius:50%;background:${tCol};flex-shrink:0"></span>` +
        `<span class="bp-viewer-search-item-name">${esc(el.name || el.ifcType)}</span>` +
        `<span class="bp-viewer-search-item-type" style="color:${el.slotColor || tCol}">${esc(el.ifcType)}</span>` +
      `</div>` +
      `<div class="bp-viewer-search-item-gid">${highlightMatch(el.globalId, q)}</div>`;
    row.addEventListener("mouseenter", () => row.classList.add("bp-viewer-search-row-item--hover"));
    row.addEventListener("mouseleave", () => row.classList.remove("bp-viewer-search-row-item--hover"));
    row.addEventListener("click", () => {
      const mesh = allMeshes().find(m => m.userData.globalId === el.globalId);
      if (mesh) { selectMesh(mesh); flyToGids([el.globalId]); }
    });
    searchResults.appendChild(row);
  }

  searchResults.style.display = "block";
}

function highlightMatch(text, q) {
  const idx = text.toLowerCase().indexOf(q);
  if (idx < 0) return esc(text);
  return (
    esc(text.slice(0, idx)) +
    `<span class="bp-viewer-search-match">${esc(text.slice(idx, idx + q.length))}</span>` +
    esc(text.slice(idx + q.length))
  );
}

if (searchInput) {
  searchInput.addEventListener("input", e => renderSearchResults(e.target.value));
  searchInput.addEventListener("keydown", e => {
    if (e.key === "Escape") { searchInput.value = ""; renderSearchResults(""); }
  });
  document.addEventListener("mousedown", e => {
    const bar = document.getElementById("search-bar");
    if (bar && !bar.contains(e.target) && searchResults) searchResults.style.display = "none";
  });
  searchInput.addEventListener("focus", () => {
    if (searchInput.value.trim() && searchResults) searchResults.style.display = "block";
  });
}
if (searchClear) {
  searchClear.addEventListener("click", () => {
    searchInput.value = "";
    renderSearchResults("");
    searchInput.focus();
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════════════
""" + f"const MODEL_URLS = [{model_urls_js}];\n" + highlight_block + r"""
;(async () => {
  if (MODEL_URLS.length === 0) {
    if (loadingEl) loadingEl.style.display = "none";
    return;
  }
  try {
    await initWebIfc();
    for (const cfg of MODEL_URLS) {
      try {
        await loadModel(cfg);
      } catch (err) {
        console.error("Ladefehler:", cfg.label, err);
        if (loadTxtEl) loadTxtEl.textContent = `⚠ Fehler: ${err.message}`;
      }
    }
    buildCategoryUI();
    buildSearchIndex();
    fitAll();
    applyClashHighlight();
  } catch (err) {
    if (loadTxtEl) loadTxtEl.textContent = "Initialisierungsfehler: " + err.message;
    console.error(err);
  } finally {
    if (loadingEl) loadingEl.style.display = "none";
  }
})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# Clash-Analyse-Seite (Legacy-Redirects)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/", response_class=HTMLResponse)
def viewer_clash_legacy_redirect(
    session_id: str = Query(default=""),
    project_id: str = Query(default=""),
):
    """Legacy route: Clash UI moved to the project-level Clash module."""
    if project_id:
        return RedirectResponse(f"/projects/{_e(project_id)}/clash", status_code=302)
    body = (
        '<div class="bp-page-header">'
        '<div class="bp-page-header__meta">'
        '<h1 class="bp-page-header__title">Clash-Analyse ist jetzt ein Projektmodul</h1>'
        '<p class="bp-page-header__subtitle">'
        'Die Clash-Analyse wird nicht mehr im Viewer gestartet. '
        'Öffne ein Projekt und nutze dort den Reiter Clash. '
        'IFC-Dateien werden aus Documents geladen; BCF-Export erfolgt im Issues-Modul.'
        '</p>'
        '</div>'
        '</div>'
        '<a class="bp-btn bp-btn--primary" href="/">Zu den Projekten</a>'
    )
    return build_page("Clash-Analyse verschoben", body, active_nav="projects")


@router.get("/viewer/clash/pset-keys/")
def viewer_clash_pset_keys_legacy():
    return JSONResponse(
        {"error": "Diese API wurde nach /projects/{project_id}/clash/pset-keys verschoben."},
        status_code=410,
    )


@router.post("/viewer/clash/run/")
async def viewer_clash_run_legacy():
    return JSONResponse(
        {"error": "Diese API wurde nach /projects/{project_id}/clash/run verschoben."},
        status_code=410,
    )


@router.get("/viewer/clash/detail/", response_class=HTMLResponse)
def viewer_clash_detail_legacy_redirect(
    project_id: str = Query(default=""),
    slot_a: int = Query(default=1),
    slot_b: int = Query(default=1),
    gid1: str = Query(default=""),
    gid2: str = Query(default=""),
):
    if project_id:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/clash/detail?slot_a={slot_a}&slot_b={slot_b}"
            f"&gid1={urllib.parse.quote(gid1)}&gid2={urllib.parse.quote(gid2)}",
            status_code=302,
        )
    body = (
        '<div class="bp-page-header">'
        '<div class="bp-page-header__meta">'
        '<h1 class="bp-page-header__title">Clash-Detail gehört jetzt zum Projektmodul</h1>'
        '<p class="bp-page-header__subtitle">Öffne die Clash-Liste innerhalb eines Projekts.</p>'
        '</div>'
        '</div>'
        '<a class="bp-btn bp-btn--primary" href="/">Zu den Projekten</a>'
    )
    return build_page("Clash-Detail verschoben", body, active_nav="projects")


@router.get("/viewer/clash/bcf/")
def viewer_clash_bcf_removed():
    return Response(
        content="BCF-Export wurde aus dem Clash-Modul entfernt. Bitte Issues-Modul verwenden.",
        status_code=410,
    )


@router.get("/viewer/clash/bcf-single/")
def viewer_clash_bcf_single_removed():
    return Response(
        content="BCF-Export wurde aus dem Clash-Modul entfernt. Bitte Clash-Zeile als Issue speichern und im Issues-Modul als BCF exportieren.",
        status_code=410,
    )
