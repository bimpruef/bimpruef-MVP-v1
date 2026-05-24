"""
viewer.py  –  BIMPruef 3D-Viewer
"""


import html
import io
import json
import os
import urllib.parse

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.templates import build_page

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

router = APIRouter()


MAX_FILE_SIZE_MB    = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
BCF_CLASH_LIMIT     = int(os.environ.get("BCF_CLASH_LIMIT", "1500"))

def _page(title: str, body: str, *, active_nav: str = "projects", container: bool = False) -> HTMLResponse:
    return build_page(
        title=title,
        body_html=body,
        active_nav=active_nav,
        container=container,
    )


def _e(s) -> str:
    return html.escape(str(s or ""))


# ─────────────────────────────────────────────────────────────────────────────
# Navigationsleiste
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
# Slot-Farbpalette  (اصلاح شد: رنگ‌های ملایم به جای رنگ‌های خالص)
# ─────────────────────────────────────────────────────────────────────────────
SLOT_COLORS = [
    "#4fc3f7", "#ef9a9a", "#a5d6a7", "#ffcc80",
    "#ce93d8", "#80cbc4", "#f48fb1", "#ffab40",
    "#80deea", "#bcaaa4",
]

def _slot_color(slot: int) -> str:
    return SLOT_COLORS[(slot - 1) % len(SLOT_COLORS)]


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
# Modifizierte IFC-Datei exportieren
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/export-ifc/")
def viewer_export_ifc(
    session_id: str = Query(...),
    slot: int       = Query(default=1),
    fmt: str        = Query(default="ifc"),
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

    if not session_id or not session_exists(session_id):
        session_id = create_upload_session()

    sid    = _e(session_id)
    slots  = get_session_slots(session_id)
    labels = {s: _e(get_ifc_label(session_id, s)) for s in slots}

    model_urls_js = ",\n".join(
        f'{{url:"/viewer/file/?session_id={sid}&slot={s}",'
        f'label:{repr(labels[s])},slot:{s},color:{repr(_slot_color(s))}}}'
        for s in slots
    )

    if slots:
        empty_hint = ""
    elif project_id:
        empty_hint = f"""
<div class="flash-ok" style="margin:8px 16px 0">
  Keine IFC-Modelle im Viewer-Cache geladen. Lade Modelle aus Documents.
  <a class="btn btn-primary" href="/projects/{_e(project_id)}/model?mode=select" style="margin-left:10px;text-decoration:none;font-size:11px;padding:3px 10px">Modelle auswählen</a>
  <a class="btn" href="/projects/{_e(project_id)}/documents" style="margin-left:6px;text-decoration:none;font-size:11px;padding:3px 10px">Zu Documents</a>
</div>"""
    else:
        empty_hint = """
<div class="flash-ok" style="margin:8px 16px 0">
  Keine Modelle geladen.
</div>"""

    error_html = f'<div class="flash-err" style="margin:8px 16px 0">⚠ {_e(error)}</div>' if error else ""

    model_cards = ""
    for s in slots:
        col = _slot_color(s)
        lbl = labels[s]
        escaped_lbl = lbl.replace("'", "\\'").replace("\\", "\\\\")
        model_cards += f"""
<div class="model-card">
  <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">
    <input type="checkbox" class="chk-model" data-slot="{s}" checked
      style="accent-color:{col};width:13px;height:13px;flex-shrink:0">
    <span style="width:10px;height:10px;border-radius:50%;border:2px solid {col};
      flex-shrink:0;display:inline-block"></span>
    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      title="{lbl}">{lbl}</span>
    <form method="post" action="/viewer/remove/" style="display:inline;margin:0"
      onsubmit="return confirm('Datei \\'{escaped_lbl}\\' wirklich schließen?')">
      <input type="hidden" name="session_id" value="{sid}">
      <input type="hidden" name="project_id" value="{_e(project_id)}">
      <input type="hidden" name="slot" value="{s}">
      <button type="submit" title="Entfernen"
        style="padding:1px 6px;font-size:10px;background:#2a0d14;
        border:1px solid #6e1a2e;color:#ff8080;border-radius:3px;cursor:pointer">✕</button>
    </form>
  </label>
</div>"""

    if project_id:
        upload_html = f"""
<div style="padding:8px 10px;border-bottom:1px solid var(--border)">
  <a class="btn btn-primary" href="/projects/{_e(project_id)}/model?mode=select"
    style="display:block;text-align:center;text-decoration:none;font-size:11px;padding:4px 8px;margin-bottom:6px">
    Modelle aus Documents laden
  </a>
  <a class="btn" href="/projects/{_e(project_id)}/documents"
    style="display:block;text-align:center;text-decoration:none;font-size:11px;padding:4px 8px">
    Zu Documents
  </a>
  <div style="font-size:10px;color:var(--muted);margin-top:6px">
    Uploads erfolgen ausschließlich im Documents-Modul.
  </div>
</div>"""
    else:
        upload_html = ""

    load_txt = "IFC-Dateien werden geladen …" if slots else "Keine Modelle aus Documents geladen."

    # اصلاح شد: _topbar حذف شد، context_label مستقیم در body جاسازی شد
    context_label = f"Projekt: {_e(project_id)[:8]}…" if project_id else f"Session: {sid[:8]}…"

    body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">

  {error_html}{empty_hint}

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- Sidebar -->
    <div style="width:220px;min-width:220px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between">
        <span>Modelle</span>
        <span style="font-size:10px;color:var(--muted);font-weight:400;text-transform:none;letter-spacing:0">{context_label}</span>
      </div>
      {upload_html}
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
{_viewer_js(model_urls_js, session_id=session_id)}
</script>

<script>
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
    if (!e.persisted) {{
      deleteSession();
    }}
  }});

  window.addEventListener("beforeunload", function() {{
    deleteSession();
  }});
}})();
</script>"""

    return _page("BIMPruef 3D-Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsamer 3D-Viewer JavaScript-Block
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
    orb.sph.phi = Math.max(0.04, Math.min(Math.PI - 0.04, orb.sph.phi + dy * 0.005));
    applyOrb();
  }
  if (orb.pan) {
    const r = new THREE.Vector3()
      .crossVectors(camera.getWorldDirection(new THREE.Vector3()), camera.up)
      .normalize();
    const sc = orb.sph.radius * 0.001;
    orb.tgt.addScaledVector(r, -dx * sc);
    orb.tgt.addScaledVector(camera.up.clone().normalize(), dy * sc);
    applyOrb();
  }
});
canvas.addEventListener("wheel", e => {
  orb.sph.radius = Math.max(0.5, Math.min(5000, orb.sph.radius * (1 + e.deltaY * 0.001)));
  applyOrb();
  e.preventDefault();
}, { passive: false });

// ═══════════════════════════════════════════════════════════════════════════
// Zustand
// ═══════════════════════════════════════════════════════════════════════════
const modelMeshes  = {};
const modelGroups  = {};
const catVisible   = {};
const hiddenIds    = new Set();
const catCounts    = {};
const catElements  = {};
const slotMeta     = {};

const FLAT_TYPES = new Set([
  "IfcOpeningElement","IfcVoidingFeature","IfcAnnotation",
  "IfcGrid","IfcGridAxis","IfcSpace",
]);

function catKey(slot, type) { return slot + ":" + type; }

function allMeshes() {
  return Object.values(modelMeshes).flat();
}

function applyVisibility() {
  for (const m of allMeshes()) {
    const key = catKey(m.userData.slot, m.userData.ifcType);
    const catOn = catVisible[key] !== false;
    const notHidden = !hiddenIds.has(m.userData.expressId);
    const isFlat = FLAT_TYPES.has(m.userData.ifcType);
    m.visible = catOn && notHidden;
    if (m.visible && isFlat) {
      m.material.color.set(0x000000);
      m.material.wireframe = true;
      m.material.opacity = 1.0;
    }
  }
  updateHiddenCount();
}

function updateHiddenCount() {
  const n   = hiddenIds.size;
  const el  = document.getElementById("hidden-count");
  const btn = document.getElementById("btn-show-all");
  if (n > 0) {
    el.textContent  = `${n} ausgeblendet`;
    el.style.display = "inline";
    if (btn) btn.style.display = "inline";
  } else {
    el.style.display = "none";
    if (btn) btn.style.display = "none";
  }
}

function buildCategoryUI() {
  const list = document.getElementById("cat-scroll");
  if (!list) return;
  list.innerHTML = "";

  const bySlot = {};
  for (const key of Object.keys(catCounts)) {
    const colonIdx = key.indexOf(":");
    const slot = parseInt(key.slice(0, colonIdx));
    const type = key.slice(colonIdx + 1);
    if (!bySlot[slot]) bySlot[slot] = {};
    bySlot[slot][type] = catCounts[key];
  }

  for (const slot of Object.keys(bySlot).map(Number).sort()) {
    const meta = slotMeta[slot] || {};
    const col  = meta.color || "#4fc3f7";
    const lbl  = meta.label || ("Slot " + slot);

    const fileRow = document.createElement("div");
    fileRow.style.cssText = "padding:4px 8px;background:#0d1f38;border-bottom:1px solid #1a2e50;" +
      "display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;flex-shrink:0";
    fileRow.innerHTML =
      `<span class="ftog" style="color:#6af;font-size:10px;font-family:monospace;width:10px;flex-shrink:0">▼</span>` +
      `<span style="width:9px;height:9px;border-radius:50%;border:2px solid ${col};flex-shrink:0;display:inline-block"></span>` +
      `<span style="font-size:11px;font-weight:600;color:${col};flex:1;overflow:hidden;` +
        `text-overflow:ellipsis;white-space:nowrap" title="${esc(lbl)}">${esc(lbl)}</span>`;
    list.appendChild(fileRow);

    const typeList = document.createElement("div");
    typeList.dataset.slotTree = slot;

    for (const type of Object.keys(bySlot[slot]).sort()) {
      const key    = catKey(slot, type);
      const vis    = catVisible[key] !== false;
      const typeCol = "#" + getColor(type).getHexString();
      const count  = bySlot[slot][type];
      const isFlat = FLAT_TYPES.has(type);
      const elems  = catElements[key] || [];

      const catRow = document.createElement("div");
      catRow.style.cssText = "display:flex;align-items:center;gap:5px;padding:3px 6px 3px 10px;" +
        "cursor:pointer;user-select:none;border-bottom:1px solid #111e35;" +
        `opacity:${vis ? "1" : ".45"};`;
      catRow.dataset.catKey = key;
      catRow.innerHTML =
        `<span class="ctog" style="color:#446;font-size:9px;font-family:monospace;width:10px;flex-shrink:0">▶</span>` +
        `<input class="cat-cb" type="checkbox" ${vis ? "checked" : ""}
           data-cat-key="${esc(key)}"
           style="width:12px;height:12px;accent-color:#4af;flex-shrink:0">` +
        `<span style="width:10px;height:10px;border-radius:50%;` +
          `background:${isFlat ? "#222" : typeCol};` +
          `border:${isFlat ? "1px solid #666" : "none"};` +
          `flex-shrink:0;display:inline-block"></span>` +
        `<span style="font-size:11px;color:${isFlat ? "#667" : "#bcd"};flex:1;overflow:hidden;` +
          `text-overflow:ellipsis;white-space:nowrap" title="${esc(type)}">${esc(type)}</span>` +
        `<span style="font-size:10px;color:#556;flex-shrink:0">${count}</span>`;
      typeList.appendChild(catRow);

      const elemList = document.createElement("div");
      elemList.style.display = "none";
      elemList.dataset.elemList = key;

      for (const el of elems) {
        const eRow = document.createElement("div");
        eRow.style.cssText = "padding:2px 8px 2px 34px;font-size:10px;color:#7a9ab8;" +
          "cursor:pointer;border-bottom:1px solid #0e1a2e;white-space:nowrap;" +
          "overflow:hidden;text-overflow:ellipsis;";
        eRow.title = el.name + (el.globalId ? " · " + el.globalId : "");
        eRow.dataset.expressId = el.expressId;
        eRow.textContent = el.name;
        eRow.addEventListener("mouseenter", () => eRow.style.background = "#162a48");
        eRow.addEventListener("mouseleave", () => eRow.style.background = "");
        eRow.addEventListener("click", e => {
          e.stopPropagation();
          const target = allMeshes().find(m => m.userData.expressId === el.expressId);
          if (target) {
            if (!target.visible) target.visible = true;
            selectMesh(target);
            const box = new THREE.Box3().setFromObject(target);
            if (!box.isEmpty()) {
              orb.tgt.copy(box.getCenter(new THREE.Vector3()));
              orb.sph.radius = Math.max(box.getSize(new THREE.Vector3()).length() * 2.5, 2);
              applyOrb();
            }
          }
        });
        elemList.appendChild(eRow);
      }
      typeList.appendChild(elemList);

      catRow.addEventListener("click", e => {
        if (e.target.classList.contains("cat-cb")) return;
        const tog = catRow.querySelector(".ctog");
        const collapsed = elemList.style.display === "none";
        elemList.style.display = collapsed ? "block" : "none";
        tog.style.color = collapsed ? "#6af" : "#446";
        tog.textContent = collapsed ? "▼" : "▶";
      });
    }
    list.appendChild(typeList);

    fileRow.addEventListener("click", () => {
      const tog = fileRow.querySelector(".ftog");
      const collapsed = typeList.style.display === "none";
      typeList.style.display = collapsed ? "" : "none";
      tog.textContent = collapsed ? "▼" : "▶";
    });
  }

  list.addEventListener("change", e => {
    if (!e.target.classList.contains("cat-cb")) return;
    const key = e.target.dataset.catKey;
    catVisible[key] = e.target.checked;
    const row = e.target.closest("[data-cat-key]");
    if (row) row.style.opacity = e.target.checked ? "1" : ".45";
    applyVisibility();
  });
}

function setCatAll(vis) {
  for (const key of Object.keys(catCounts)) catVisible[key] = vis;
  buildCategoryUI();
  applyVisibility();
}

const btnCatAll  = document.getElementById("btn-cat-all");
const btnCatNone = document.getElementById("btn-cat-none");
if (btnCatAll)  btnCatAll.addEventListener("click",  () => setCatAll(true));
if (btnCatNone) btnCatNone.addEventListener("click", () => setCatAll(false));

document.addEventListener("change", e => {
  if (!e.target.classList.contains("chk-model")) return;
  const slot = parseInt(e.target.dataset.slot);
  if (modelGroups[slot]) modelGroups[slot].visible = e.target.checked;
});

// ═══════════════════════════════════════════════════════════════════════════
// web-ifc laden
// ═══════════════════════════════════════════════════════════════════════════
let webIfc = null;

async function initWebIfc() {
  const mod = await import("https://esm.sh/web-ifc@0.0.57");
  webIfc = new mod.IfcAPI();
  webIfc.SetWasmPath("https://esm.sh/web-ifc@0.0.57/");
  await webIfc.Init();
}

// ═══════════════════════════════════════════════════════════════════════════
// IFC-Modell laden
// ═══════════════════════════════════════════════════════════════════════════
async function loadModel(cfg) {
  if (loadTxtEl) loadTxtEl.textContent = `${cfg.label} wird geladen …`;

  const resp = await fetch(cfg.url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} für ${cfg.label}`);
  const data = new Uint8Array(await resp.arrayBuffer());

  const modelId = webIfc.OpenModel(data, {
    COORDINATE_TO_ORIGIN: false,
    USE_FAST_BOOLS: false,
  });

  const elemIndex = {};
  const allLines  = webIfc.GetAllLines(modelId);
  for (let i = 0; i < allLines.size(); i++) {
    const id = allLines.get(i);
    try {
      const line = webIfc.GetLine(modelId, id, false);
      if (line) elemIndex[id] = line;
    } catch (_) {}
  }

  const _typeNameCache = {};
  function resolveTypeCode(code) {
    if (_typeNameCache[code] !== undefined) return _typeNameCache[code];
    let name = "Unknown";
    try {
      const raw = webIfc.GetNameFromTypeCode(code);
      if (raw && raw.length > 0) {
        if (raw.includes("_")) {
          const parts = raw.toLowerCase().replace(/^ifc_/, "").split("_");
          name = "Ifc" + parts.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join("");
        } else {
          const low = raw.toLowerCase();
          name = "Ifc" + low.slice(3).charAt(0).toUpperCase() + low.slice(4);
        }
      }
    } catch (_) {}
    _typeNameCache[code] = name;
    return name;
  }

  function typeName(line) {
    if (!line) return "Unknown";
    if (typeof line.type === "number")  return resolveTypeCode(line.type);
    if (typeof line.type === "string" && line.type.startsWith("Ifc")) return line.type;
    const cn = line.constructor?.name ?? "";
    if (cn.startsWith("Ifc") && cn.length > 5) return cn;
    return "Unknown";
  }

  function sv(v) {
    if (v == null) return "";
    if (typeof v === "object" && v.value !== undefined) return String(v.value);
    return String(v);
  }

  const relMap = {};
  for (const [id, line] of Object.entries(elemIndex)) {
    if (!typeName(line).toLowerCase().includes("reldefinesbyprop")) continue;
    const pref = line.RelatingPropertyDefinition;
    if (!pref) continue;
    const pid  = pref.value ?? pref;
    const rel  = line.RelatedObjects;
    if (!rel) continue;
    const ids  = Array.isArray(rel) ? rel : [rel];
    for (const r of ids) {
      const rid = r?.value ?? r;
      if (!relMap[rid]) relMap[rid] = [];
      relMap[rid].push(pid);
    }
  }

  function getPsets(eid) {
    const res = {};
    for (const pid of relMap[eid] ?? []) {
      const pset = elemIndex[pid];
      if (!pset) continue;
      const pn    = sv(pset.Name) || typeName(pset);
      const props = {};
      const hp    = pset.HasProperties;
      if (hp) {
        const list = Array.isArray(hp) ? hp : [hp];
        for (const ref of list) {
          const id   = ref?.value ?? ref;
          const prop = elemIndex[id];
          if (!prop) continue;
          props[sv(prop.Name) || String(id)] =
            prop.NominalValue != null ? sv(prop.NominalValue) : "–";
        }
      }
      res[pn] = props;
    }
    return res;
  }

  const group = new THREE.Group();
  group.name  = cfg.label;
  scene.add(group);
  modelGroups[cfg.slot] = group;
  modelMeshes[cfg.slot] = [];
  slotMeta[cfg.slot]    = { label: cfg.label, color: cfg.color };

  const fms        = webIfc.LoadAllGeometry(modelId);
  let   vertCount  = 0;
  const seenExpIds = new Set();

  for (let i = 0; i < fms.size(); i++) {
    const fm    = fms.get(i);
    const expId = fm.expressID;
    const line  = elemIndex[expId];
    const tName = typeName(line);
    const isFlat = FLAT_TYPES.has(tName);
    const tCol  = isFlat ? new THREE.Color(0x000000) : getColor(tName);
    const key   = catKey(cfg.slot, tName);

    if (!seenExpIds.has(expId)) {
      seenExpIds.add(expId);
      catCounts[key] = (catCounts[key] ?? 0) + 1;
      if (catVisible[key] === undefined) catVisible[key] = !isFlat;
      if (!catElements[key]) catElements[key] = [];
      const eName = sv(line?.Name) || sv(line?.GlobalId) || String(expId);
      catElements[key].push({ name: eName, expressId: expId, globalId: sv(line?.GlobalId) });
    }

    const meta = {
      expressId:   expId,
      ifcType:     tName,
      name:        sv(line?.Name),
      globalId:    sv(line?.GlobalId),
      objectType:  sv(line?.ObjectType),
      description: sv(line?.Description),
      tag:         sv(line?.Tag),
      slot:        cfg.slot,
      modelLabel:  cfg.label,
      slotColor:   cfg.color,
      psets:       getPsets(expId),
      isFlat:      isFlat,
    };

    const mat = new THREE.MeshLambertMaterial({
      color:       tCol.clone(),
      transparent: true,
      opacity:     isFlat ? 1.0 : 0.90,
      wireframe:   isFlat,
      side:        THREE.DoubleSide,
    });

    const pgs = fm.geometries;
    for (let j = 0; j < pgs.size(); j++) {
      const pg  = pgs.get(j);
      const gd  = webIfc.GetGeometry(modelId, pg.geometryExpressID);
      const vs  = webIfc.GetVertexArray(gd.GetVertexData(), gd.GetVertexDataSize());
      const idx = webIfc.GetIndexArray(gd.GetIndexData(), gd.GetIndexDataSize());
      if (!vs || vs.length === 0) { gd.delete(); continue; }

      const S  = 6;
      const pa = new Float32Array(vs.length / S * 3);
      const na = new Float32Array(vs.length / S * 3);
      for (let k = 0; k < vs.length / S; k++) {
        pa[k*3]   = vs[k*S];   pa[k*3+1] = vs[k*S+1]; pa[k*3+2] = vs[k*S+2];
        na[k*3]   = vs[k*S+3]; na[k*3+1] = vs[k*S+4]; na[k*3+2] = vs[k*S+5];
      }

      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(pa, 3));
      geo.setAttribute("normal",   new THREE.BufferAttribute(na, 3));
      geo.setIndex(new THREE.BufferAttribute(idx, 1));

      const mesh = new THREE.Mesh(geo, mat.clone());
      mesh.applyMatrix4(new THREE.Matrix4().fromArray(pg.flatTransformation));
      mesh.userData = Object.assign({}, meta);
      mesh.visible = !isFlat;

      group.add(mesh);
      modelMeshes[cfg.slot].push(mesh);
      vertCount += pa.length / 3;
      gd.delete();
    }
  }

  webIfc.CloseModel(modelId);
}

// ═══════════════════════════════════════════════════════════════════════════
// Einpassen
// ═══════════════════════════════════════════════════════════════════════════
function fitAll() {
  const box = new THREE.Box3();
  scene.traverse(o => { if (o.isMesh && o.visible) box.expandByObject(o); });
  if (box.isEmpty()) scene.traverse(o => { if (o.isMesh) box.expandByObject(o); });
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size   = box.getSize(new THREE.Vector3());
  orb.tgt.copy(center);
  orb.sph.radius = Math.max(size.x, size.y, size.z) * 1.9;
  applyOrb();
  gridHelper.position.y = box.min.y;
}

function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ═══════════════════════════════════════════════════════════════════════════
// Info-Panel
// ═══════════════════════════════════════════════════════════════════════════
let _editSlot      = null;
let _editExpressId = null;

function _inpStyle() {
  return `background:#0e1a30;border:1px solid #1e3a6e;color:#d0dce8;
    padding:3px 6px;border-radius:4px;font-size:11px;width:100%;
    box-sizing:border-box;font-family:inherit;outline:none;`;
}

function showInfo(mesh) {
  const d  = mesh.userData;
  _editSlot      = d.slot;
  _editExpressId = d.expressId;
  _renderInfoPanel(d);
  infoPanel.style.width    = "300px";
  infoPanel.style.minWidth = "300px";
}

function _renderInfoPanel(d) {
  const tc = "#" + getColor(d.ifcType).getHexString();

  let h = `
<div style="font-size:11px;font-weight:bold;color:${d.slotColor};margin-bottom:8px;
  display:flex;align-items:center;justify-content:space-between">
  <span>Slot ${d.slot} · ${esc(d.modelLabel)}</span>
  <span style="font-size:10px;color:#4a8;background:#0a2010;border:1px solid #1a5030;
    border-radius:4px;padding:1px 6px" id="edit-dirty-badge" style="display:none">✎ ungespeichert</span>
</div>`;

  h += `
<div style="margin-bottom:10px">
  <div style="display:flex;gap:5px;margin-bottom:3px;align-items:center">
    <span style="color:#667;min-width:86px;flex-shrink:0;font-size:11px">IFC-Typ</span>
    <span style="color:#cce;font-size:11px;display:flex;align-items:center;gap:4px">
      <span style="display:inline-block;width:9px;height:9px;border-radius:50%;
        background:${tc};flex-shrink:0"></span>${esc(d.ifcType || "–")}</span>
  </div>
  <div style="display:flex;gap:5px;margin-bottom:3px;align-items:center">
    <span style="color:#667;min-width:86px;flex-shrink:0;font-size:11px">GlobalId</span>
    <span style="color:#bbd;word-break:break-all;font-size:11px;font-family:monospace">${esc(d.globalId || "–")}</span>
  </div>
  <div style="display:flex;gap:5px;margin-bottom:3px;align-items:center">
    <span style="color:#667;min-width:86px;flex-shrink:0;font-size:11px">Express-ID</span>
    <span style="color:#bbd;font-size:11px">${esc(d.expressId)}</span>
  </div>
</div>`;

  h += `<div style="border-top:1px solid #0f3460;padding-top:8px;margin-bottom:4px;
    font-size:10px;font-weight:700;color:#6af;text-transform:uppercase;
    letter-spacing:.4px">Bearbeitbare Attribute</div>`;

  const editableAttrs = [
    { key: "name",        label: "Name",         field: "Name",         hint: "" },
    { key: "objectType",  label: "ObjectType",   field: "ObjectType",   hint: "" },
    { key: "tag",         label: "Tag",          field: "Tag",          hint: "" },
    { key: "description", label: "Beschreibung", field: "Description",  hint: "" },
  ];

  for (const a of editableAttrs) {
    const val = d[a.key] || "";
    h += `
<div style="margin-bottom:5px">
  <div style="color:#556;font-size:10px;margin-bottom:2px">${esc(a.label)}</div>
  <input type="text"
    data-edit-field="${esc(a.field)}"
    value="${esc(val)}"
    placeholder="${esc(a.hint || a.label)}"
    style="${_inpStyle()}"
    oninput="markDirty()">
</div>`;
  }

  const psets = d.psets ?? {};
  h += `
<div style="border-top:1px solid #0f3460;padding-top:8px;margin-top:6px;margin-bottom:4px;
  display:flex;align-items:center;justify-content:space-between">
  <span style="font-size:10px;font-weight:700;color:#6af;text-transform:uppercase;letter-spacing:.4px">
    Property Sets</span>
  <button onclick="addNewPset()" style="font-size:10px;padding:2px 7px;
    background:#0a2a40;border:1px solid #1e4a6e;color:#4fc3f7;
    border-radius:4px;cursor:pointer">+ PSet</button>
</div>
<div id="pset-container">`;

  for (const pn of Object.keys(psets)) {
    h += _renderPset(pn, psets[pn]);
  }
  h += `</div>`;

  h += `
<div style="border-top:1px solid #0f3460;padding-top:10px;margin-top:10px;
  display:flex;flex-direction:column;gap:6px">
  <div style="display:flex;gap:5px">
    <button id="btn-save-elem" onclick="saveElementChanges()"
      style="flex:1;padding:7px;background:#0a3a20;border:1px solid #1a6040;
      color:#4caf50;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
      💾 Änderungen speichern
    </button>
    <button onclick="discardElementChanges()"
      style="padding:7px 10px;background:#2a0a14;border:1px solid #6e1a2e;
      color:#ff8080;border-radius:6px;cursor:pointer;font-size:11px">
      ✕
    </button>
  </div>
  <div style="display:flex;gap:5px">
    <a href="/viewer/export-ifc/?session_id=${esc(SESSION_ID)}&slot=${esc(String(_editSlot))}&fmt=ifc"
      style="flex:1;padding:6px;background:#0a1a30;border:1px solid #1e3a6e;color:#7ab8e8;
      border-radius:6px;font-size:11px;text-align:center;text-decoration:none">
      ⬇ Export .ifc
    </a>
    <a href="/viewer/export-ifc/?session_id=${esc(SESSION_ID)}&slot=${esc(String(_editSlot))}&fmt=ifczip"
      style="flex:1;padding:6px;background:#0a1a30;border:1px solid #1e3a6e;color:#7ab8e8;
      border-radius:6px;font-size:11px;text-align:center;text-decoration:none">
      ⬇ Export .ifczip
    </a>
  </div>
  <div id="save-status" style="font-size:11px;min-height:18px;text-align:center"></div>
</div>`;

  infoBody.innerHTML = h;
}

function _renderPset(psetName, props) {
  const filteredProps = {};
  for (const [k, v] of Object.entries(props)) {
    if (k === "id") continue;
    filteredProps[k] = v;
  }

  let propsHtml = "";
  const propEntries = Object.keys(filteredProps);

  if (propEntries.length === 0) {
    propsHtml = _emptyPropRow();
  } else {
    for (const k of propEntries) {
      propsHtml += _propRowHtml(k, filteredProps[k]);
    }
  }

  return `
<div class="pset-edit-block" data-pset="${esc(psetName)}"
  style="background:#0c1a30;border:1px solid #1a2e50;border-radius:5px;
  margin-bottom:6px;overflow:hidden">
  <div style="display:flex;align-items:center;justify-content:space-between;
    padding:4px 7px;background:#0a1a2a;cursor:pointer"
    onclick="togglePsetBlock(this)">
    <span style="font-size:11px;font-weight:600;color:#4fc3f7">${esc(psetName)}</span>
    <div style="display:flex;gap:4px;align-items:center">
      <button onclick="event.stopPropagation();addPsetProp(this)"
        title="Eigenschaft hinzufügen"
        style="font-size:10px;padding:1px 7px;background:#0a2040;border:1px solid #1e4a6e;
        color:#4ab;border-radius:3px;cursor:pointer">+ Eigenschaft</button>
      <span class="pset-header-arrow" style="color:#446;font-size:12px">▼</span>
    </div>
  </div>
  <div class="pset-props" style="padding:5px 7px">${propsHtml}</div>
</div>`;
}

function _propRowHtml(k, v) {
  return `
<div class="prop-row" style="display:flex;gap:4px;margin-bottom:4px;align-items:center">
  <input type="text" data-prop-key value="${esc(k)}" placeholder="Eigenschaft"
    style="${_inpStyle()}flex:1;min-width:0" oninput="markDirty()">
  <span style="color:#446;flex-shrink:0">=</span>
  <input type="text" data-prop-val value="${esc(v)}" placeholder="Wert"
    style="${_inpStyle()}flex:1;min-width:0" oninput="markDirty()">
  <button onclick="removePropRow(this)" title="Zeile entfernen"
    style="padding:2px 5px;font-size:10px;background:#2a0a14;border:1px solid #6e1a2e;
    color:#f88;border-radius:3px;cursor:pointer;flex-shrink:0">✕</button>
</div>`;
}

function _emptyPropRow() { return _propRowHtml("", ""); }

function togglePsetBlock(header) {
  const propsDiv = header.nextElementSibling;
  const arrow = header.querySelector(".pset-header-arrow");
  const hidden = propsDiv.style.display === "none";
  propsDiv.style.display = hidden ? "" : "none";
  if (arrow) arrow.textContent = hidden ? "▼" : "▶";
}

function markDirty() {
  const badge = document.getElementById("edit-dirty-badge");
  if (badge) badge.style.display = "inline";
}

function addNewPset() {
  const name = prompt("Name des neuen Property Sets:", "Pset_Custom");
  if (!name || !name.trim()) return;
  const container = document.getElementById("pset-container");
  if (!container) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = _renderPset(name.trim(), {});
  const block = tmp.firstElementChild;
  container.appendChild(block);
  const firstKey = block.querySelector("[data-prop-key]");
  if (firstKey) firstKey.focus();
  markDirty();
}

function addPsetProp(btn) {
  const block = btn.closest(".pset-edit-block");
  if (!block) return;
  const propsDiv = block.querySelector(".pset-props");
  if (!propsDiv) return;
  if (propsDiv.style.display === "none") {
    propsDiv.style.display = "";
    const arrow = block.querySelector(".pset-header-arrow");
    if (arrow) arrow.textContent = "▼";
  }
  const tmp = document.createElement("div");
  tmp.innerHTML = _propRowHtml("", "");
  const row = tmp.firstElementChild;
  if (!row) return;
  propsDiv.appendChild(row);
  const keyInp = row.querySelector("[data-prop-key]");
  if (keyInp) keyInp.focus();
  markDirty();
}

function removePropRow(btn) {
  btn.closest(".prop-row")?.remove();
  markDirty();
}

function discardElementChanges() {
  if (!selectedMesh) return;
  _renderInfoPanel(selectedMesh.userData);
}

async function saveElementChanges() {
  if (!selectedMesh) return;
  const btn    = document.getElementById("btn-save-elem");
  const status = document.getElementById("save-status");
  if (btn)    btn.disabled = true;
  if (status) status.innerHTML = '<span style="color:#7ab">Speichern …</span>';

  const changes = {};
  for (const inp of infoBody.querySelectorAll("[data-edit-field]")) {
    changes[inp.dataset.editField] = inp.value;
  }

  const psets = {};
  for (const block of infoBody.querySelectorAll(".pset-edit-block")) {
    const psetName = block.dataset.pset;
    if (!psetName) continue;
    const props = {};
    for (const row of block.querySelectorAll(".prop-row")) {
      const keyInp = row.querySelector("[data-prop-key]");
      const valInp = row.querySelector("[data-prop-val]");
      const k = keyInp?.value?.trim();
      const v = valInp?.value ?? "";
      if (k) props[k] = v;
    }
    psets[psetName] = props;
  }
  changes.psets = psets;

  try {
    const resp = await fetch("/viewer/element/update/", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        session_id: SESSION_ID,
        slot:       _editSlot,
        express_id: _editExpressId,
        changes,
      }),
    });
    const data = await resp.json();

    if (data.error) {
      if (status) status.innerHTML = `<span style="color:#f88">⚠ ${esc(data.error)}</span>`;
    } else {
      for (const m of allMeshes()) {
        if (m.userData.expressId !== _editExpressId || m.userData.slot !== _editSlot) continue;
        m.userData.name        = data.name        ?? changes.Name ?? m.userData.name;
        m.userData.objectType  = data.object_type ?? changes.ObjectType ?? m.userData.objectType;
        m.userData.description = data.description ?? changes.Description ?? m.userData.description;
        m.userData.tag         = data.tag         ?? changes.Tag ?? m.userData.tag;
        m.userData.psets       = data.psets       ?? psets;
      }

      if (status) status.innerHTML = '<span style="color:#4caf50">✓ Gespeichert</span>';
      setTimeout(() => { if (status) status.innerHTML = ""; }, 2500);
      _renderInfoPanel(selectedMesh.userData);
    }
  } catch (err) {
    if (status) status.innerHTML = `<span style="color:#f88">⚠ Verbindungsfehler: ${esc(err.message)}</span>`;
  } finally {
    const b2 = document.getElementById("btn-save-elem");
    if (b2) b2.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Selektion via Raycasting
// ═══════════════════════════════════════════════════════════════════════════
const raycaster  = new THREE.Raycaster();
const mouse      = new THREE.Vector2();
const HIGHLIGHT  = new THREE.Color(0xffff00);
let selectedMesh = null;
let mouseMoved   = false;

function selectMesh(m) {
  if (selectedMesh && selectedMesh !== m)
    selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  if (!m.userData._origColor)
    m.userData._origColor = m.material.color.clone();
  m.material.color.copy(HIGHLIGHT);
  selectedMesh = m;
  showInfo(m);
}

function deselectAll() {
  if (selectedMesh) {
    selectedMesh.material.color.copy(selectedMesh.userData._origColor);
    selectedMesh = null;
  }
  infoBody.innerHTML =
    '<div style="color:var(--muted);font-style:italic">Klick auf ein Element für Details.</div>';
}

canvas.addEventListener("mousedown", () => { mouseMoved = false; });
canvas.addEventListener("mousemove", () => { mouseMoved = true; });
canvas.addEventListener("mouseup", e => {
  if (e.button !== 0 || mouseMoved) return;
  const rect = canvas.getBoundingClientRect();
  mouse.x =  ((e.clientX - rect.left) / rect.width)  * 2 - 1;
  mouse.y = -((e.clientY - rect.top)  / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(allMeshes().filter(m => m.visible), false);
  if (hits.length > 0) selectMesh(hits[0].object);
  else deselectAll();
});

window.addEventListener("keydown", e => {
  if (e.code !== "Space" || !selectedMesh) return;
  e.preventDefault();
  const id = selectedMesh.userData.expressId;
  hiddenIds.add(id);
  for (const m of allMeshes()) if (m.userData.expressId === id) m.visible = false;
  selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  selectedMesh = null;
  infoBody.innerHTML =
    '<div style="color:var(--muted);font-style:italic">Element ausgeblendet.</div>';
  updateHiddenCount();
});

const btnFit    = document.getElementById("btn-fit");
const btnReset  = document.getElementById("btn-reset");
const btnShowAll= document.getElementById("btn-show-all");
const infoClose = document.getElementById("info-close");

if (btnFit)    btnFit.addEventListener("click", fitAll);
if (btnReset)  btnReset.addEventListener("click", () => {
  orb.tgt.set(0, 0, 0);
  orb.sph.set(80, Math.PI / 4, Math.PI / 4);
  applyOrb();
});
if (btnShowAll) btnShowAll.addEventListener("click", () => {
  hiddenIds.clear();
  applyVisibility();
});
if (infoClose) infoClose.addEventListener("click", () => {
  infoPanel.style.width    = "0";
  infoPanel.style.minWidth = "0";
});

(function animate() {
  requestAnimationFrame(animate);
  renderer.render(scene, camera);
})();

""" + f"\nconst MODEL_URLS = [{model_urls_js}];\n" + highlight_block + r"""

// ═══════════════════════════════════════════════════════════════════════════
// GlobalId-Suche
// ═══════════════════════════════════════════════════════════════════════════
const searchInput   = document.getElementById("gid-search");
const searchResults = document.getElementById("search-results");
const searchClear   = document.getElementById("search-clear");

const searchIndex = [];
let searchHighlightActive = false;

function buildSearchIndex() {
  searchIndex.length = 0;
  const seen = new Set();
  for (const m of allMeshes()) {
    const gid = m.userData.globalId;
    if (!gid || seen.has(gid)) continue;
    seen.add(gid);
    searchIndex.push({
      globalId:   gid,
      expressId:  m.userData.expressId,
      name:       m.userData.name,
      ifcType:    m.userData.ifcType,
      slot:       m.userData.slot,
      modelLabel: m.userData.modelLabel,
      slotColor:  m.userData.slotColor,
    });
  }
}

function clearSearchHighlight() {
  if (!searchHighlightActive) return;
  for (const m of allMeshes()) {
    m.material.opacity = m.userData.isFlat ? 1.0 : 0.90;
  }
  applyVisibility();
  searchHighlightActive = false;
}

function applySearchHighlight(matchGids) {
  const gidSet = new Set(matchGids);
  searchHighlightActive = true;
  for (const m of allMeshes()) {
    if (gidSet.has(m.userData.globalId)) {
      m.material.opacity = 0.97;
      m.visible = true;
    } else {
      m.material.opacity = 0.05;
    }
  }
}

function flyToGids(matchGids) {
  const gidSet = new Set(matchGids);
  const box = new THREE.Box3();
  for (const m of allMeshes()) {
    if (gidSet.has(m.userData.globalId)) box.expandByObject(m);
  }
  if (!box.isEmpty()) {
    const center = box.getCenter(new THREE.Vector3());
    const size   = box.getSize(new THREE.Vector3());
    orb.tgt.copy(center);
    orb.sph.radius = Math.max(size.x, size.y, size.z) * 3.5;
    applyOrb();
  }
}

function renderSearchResults(query) {
  const q = query.trim().toLowerCase();
  if (q.length === 0) {
    searchResults.style.display = "none";
    searchClear.style.display   = "none";
    clearSearchHighlight();
    return;
  }
  searchClear.style.display = "inline";

  const hits = searchIndex.filter(el => el.globalId.toLowerCase().includes(q));
  searchResults.innerHTML = "";

  if (hits.length === 0) {
    searchResults.style.display = "block";
    searchResults.innerHTML =
      '<div style="padding:8px 12px;font-size:11px;color:var(--muted);font-style:italic">Keine Treffer</div>';
    clearSearchHighlight();
    return;
  }

  const header = document.createElement("div");
  header.style.cssText = "padding:5px 12px;font-size:10px;color:#6af;border-bottom:1px solid #1a2e50;" +
    "display:flex;align-items:center;justify-content:space-between";
  header.innerHTML =
    `<span>${hits.length} Element${hits.length !== 1 ? "e" : ""} gefunden</span>` +
    `<span style="color:var(--muted)">${hits.length > 50 ? "ersten 50 angezeigt" : ""}</span>`;
  searchResults.appendChild(header);

  const visible50 = hits.slice(0, 50);
  const matchGids = hits.map(h => h.globalId);

  applySearchHighlight(matchGids);
  if (hits.length <= 5) flyToGids(matchGids);

  for (const el of visible50) {
    const col  = el.slotColor || "#4fc3f7";
    const tCol = "#" + getColor(el.ifcType).getHexString();
    const row  = document.createElement("div");
    row.style.cssText = "padding:5px 10px;cursor:pointer;border-bottom:1px solid #0e1a2e;" +
      "display:flex;flex-direction:column;gap:1px";
    row.innerHTML =
      `<div style="display:flex;align-items:center;gap:5px">` +
        `<span style="width:7px;height:7px;border-radius:50%;background:${tCol};flex-shrink:0"></span>` +
        `<span style="font-size:11px;color:#cce;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">` +
          `${esc(el.name || el.ifcType)}</span>` +
        `<span style="font-size:10px;color:${col};flex-shrink:0">${esc(el.ifcType)}</span>` +
      `</div>` +
      `<div style="font-size:10px;color:#4a7090;font-family:monospace;padding-left:12px">` +
        highlightMatch(el.globalId, q) +
      `</div>`;
    row.addEventListener("mouseenter", () => row.style.background = "#162a48");
    row.addEventListener("mouseleave", () => row.style.background = "");
    row.addEventListener("click", () => {
      const mesh = allMeshes().find(m => m.userData.globalId === el.globalId);
      if (mesh) {
        selectMesh(mesh);
        flyToGids([el.globalId]);
      }
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
    `<span style="background:#1a4a6e;color:#7df;border-radius:2px;padding:0 1px">` +
    esc(text.slice(idx, idx + q.length)) +
    `</span>` +
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
    if (bar && !bar.contains(e.target)) searchResults.style.display = "none";
  });
  searchInput.addEventListener("focus", () => {
    if (searchInput.value.trim()) searchResults.style.display = "block";
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
(async () => {
  if (MODEL_URLS.length === 0) { loadingEl.style.display = "none"; return; }
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
    loadingEl.style.display = "none";
  }
})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# Clash-Analyse-Seite
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/", response_class=HTMLResponse)
def viewer_clash_legacy_redirect(
    session_id: str = Query(default=""),
    project_id: str = Query(default=""),
):
    """Legacy route: Clash UI moved to the project-level Clash module."""
    if project_id:
        return RedirectResponse(f"/projects/{_e(project_id)}/clash", status_code=302)
    return _page(
        "Clash-Analyse verschoben",
        '<div style="padding:40px;max-width:760px">'
        '<h2 style="font-size:20px;margin-bottom:10px">Clash-Analyse ist jetzt ein Projektmodul</h2>'
        '<p style="color:var(--muted);font-size:13px;margin-bottom:16px">'
        'Die Clash-Analyse wird nicht mehr im Viewer gestartet. Öffne ein Projekt und nutze dort den Reiter Clash.'
        '</p>'
        '<a class="btn btn-primary" href="/" style="text-decoration:none">Zu den Projekten</a>'
        '</div>'
    )


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
    return _page(
        "Clash-Detail verschoben",
        '<div style="padding:40px;max-width:760px">'
        '<h2 style="font-size:20px;margin-bottom:10px">Clash-Detail gehört jetzt zum Projektmodul</h2>'
        '<p style="color:var(--muted);font-size:13px;margin-bottom:16px">Öffne die Clash-Liste innerhalb eines Projekts.</p>'
        '<a class="btn btn-primary" href="/" style="text-decoration:none">Zu den Projekten</a>'
        '</div>'
    )


@router.get("/viewer/clash/bcf/")
def viewer_clash_bcf_removed():
    return Response(
        content="BCF-Export wurde aus dem Clash-Modul entfernt. Bitte Issues-Modul verwenden.",
        status_code=410,
    )


@router.get("/viewer/clash/bcf-single/")
def viewer_clash_bcf_single_removed():
    return Response(
        content="BCF-Export wurde aus dem Clash-Modul entfernt. Bitte Clash-Zeile als Issue speichern.",
        status_code=410,
    )