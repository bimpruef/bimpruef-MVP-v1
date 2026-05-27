"""
project_viewer.py – BIMPruef Direct Viewer

Direkter Viewer-Modul – liest IFC-Modelle direkt aus Documents/R2.
Keine Abhängigkeit zu Session/Slot-Cache.

Routen:
  GET  /projects/{project_id}/view               → Viewer-Hauptseite (direkt ohne Auswahlseite)
  GET  /projects/{project_id}/view/file/{doc_id} → IFC-Stream direkt aus R2
  POST /projects/{project_id}/view/load          → Modellauswahl ändern
"""

from __future__ import annotations

import html
import json
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_user
from app.document_storage import (
    get_document,
    list_project_ifc_documents,
)
from app.exceptions import NotFoundError, StorageError
from app.project_storage import get_project

try:
    from app.r2_storage import download_file_from_r2, r2_enabled
except Exception:
    download_file_from_r2 = None
    r2_enabled = lambda: False

project_viewer_router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s or ""))


def _account_from_request(request: Request) -> dict:
    user = require_user(request)
    return {"account_id": user["user_id"], "account_name": user["email"]}


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
    "#1E6FBF", "#D97706", "#059669", "#DC2626",
    "#7C3AED", "#0891B2", "#BE185D", "#65A30D",
]

def _slot_color(index: int) -> str:
    return SLOT_COLORS[index % len(SLOT_COLORS)]


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Datei Stream-Endpunkt
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/view/file/{document_id}")
def view_file_stream(request: Request, project_id: str, document_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]

    try:
        doc = get_document(account_id, project_id, document_id)
    except NotFoundError:
        return Response(content="Dokument nicht gefunden", status_code=404)

    if not doc.get("r2_key"):
        return Response(content="R2-Schlüssel fehlt", status_code=404)

    if not (r2_enabled() and download_file_from_r2):
        return Response(content="R2 nicht konfiguriert", status_code=503)

    suffix = doc.get("file_extension", ".ifc")
    fd, tmp_path = tempfile.mkstemp(prefix="bpview-", suffix=suffix)
    os.close(fd)

    try:
        download_file_from_r2(doc["r2_key"], tmp_path)
        ifc_bytes = _read_ifc_bytes(tmp_path, doc.get("file_extension", ""))

        return Response(
            content=ifc_bytes,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'inline; filename="{_e(doc["safe_filename"])}"',
                "Content-Length": str(len(ifc_bytes)),
                "Cache-Control": "private, max-age=300",
            },
        )
    except Exception as exc:
        return Response(content=f"Fehler beim Laden: {exc}", status_code=500)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _read_ifc_bytes(path: str, extension: str) -> bytes:
    import zipfile, io
    with open(path, "rb") as f:
        raw = f.read()
    ext = (extension or "").lower()
    if ext == ".ifczip":
        with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
            ifc_names = [n for n in zf.namelist() if n.lower().endswith(".ifc")]
            if not ifc_names:
                raise ValueError("Keine .ifc-Datei im IFCZIP-Archiv")
            return zf.read(ifc_names[0])
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-Viewer-Seite (direkter Einstieg ohne separate Auswahlseite)
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/view", response_class=HTMLResponse)
def view_main(
    request: Request,
    project_id: str,
    doc_ids: list[str] = Query(default=[]),
    error: str = Query(default=""),
):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    all_ifc_docs = list_project_ifc_documents(account_id, project_id)

    # ── CHANGE 1 ──────────────────────────────────────────────────────────────
    # Viewer starts empty on first open. Models are only loaded when the user
    # explicitly selects them and clicks "Laden" in the sidebar.
    # No automatic fallback to "load all" when doc_ids is empty.
    valid_docs = {d["document_id"]: d for d in all_ifc_docs}
    selected_docs = [valid_docs[did] for did in doc_ids if did in valid_docs]
    # ─────────────────────────────────────────────────────────────────────────

    # Viewer immer rendern (auch ohne Modelle – leerer Viewer)
    return _render_viewer_page(account, project, project_id, selected_docs, all_ifc_docs, error)


@project_viewer_router.post("/projects/{project_id}/view/load")
def view_load(
    request: Request,
    project_id: str,
    doc_ids: list[str] = Form(default=[]),
):
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    from urllib.parse import urlencode
    params = urlencode([("doc_ids", did) for did in doc_ids]) if doc_ids else ""
    url = f"/projects/{_e(project_id)}/view"
    if params:
        url += "?" + params
    return RedirectResponse(url, status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Viewer-Seite Rendering
# ─────────────────────────────────────────────────────────────────────────────

def _color_to_light_bg(hex_color: str) -> str:
    mapping = {
        "#1E6FBF": "#EFF6FF", "#D97706": "#FFFBEB", "#059669": "#ECFDF5",
        "#DC2626": "#FEF2F2", "#7C3AED": "#F5F3FF", "#0891B2": "#ECFEFF",
        "#BE185D": "#FDF2F8", "#65A30D": "#F7FEE7",
    }
    return mapping.get(hex_color, "#F5F6F8")


def _render_viewer_page(account, project, project_id, selected_docs, all_ifc_docs, error="") -> HTMLResponse:
    from app.projects import _page, _project_subnav, _topbar_global

    pid = _e(project_id)

    model_entries = []
    for i, doc in enumerate(selected_docs):
        color = _slot_color(i)
        url = f"/projects/{pid}/view/file/{_e(doc['document_id'])}"
        model_entries.append({
            "url": url,
            "label": doc["original_filename"],
            "color": color,
            "documentId": doc["document_id"],
        })

    model_urls_js = ",\n".join(
        f'{{url:{json.dumps(m["url"])},label:{json.dumps(m["label"])},color:{json.dumps(m["color"])},documentId:{json.dumps(m["documentId"])}}}'
        for m in model_entries
    )

    # Sidebar Dokumentenliste mit Checkboxen
    selected_ids = {d["document_id"] for d in selected_docs}
    select_rows = ""
    for i, doc in enumerate(all_ifc_docs):
        checked = "checked" if doc["document_id"] in selected_ids else ""
        col = _slot_color(i)
        col_light = _color_to_light_bg(col)
        ext_badge = _e(doc['file_extension'].upper().lstrip('.'))
        select_rows += f"""
        <label style="display:flex;align-items:center;gap:9px;padding:8px 12px;
          border-radius:7px;cursor:pointer;font-size:12px;
          background:{'rgba(30,111,191,0.06)' if checked else 'transparent'};
          transition:background 0.12s;border:1px solid {'rgba(30,111,191,0.2)' if checked else 'transparent'}"
          id="lbl-{_e(doc['document_id'])}"
          onmouseenter="if(!this.querySelector('input').checked)this.style.background='rgba(0,0,0,0.03)'"
          onmouseleave="if(!this.querySelector('input').checked)this.style.background='transparent'">
          <input type="checkbox" name="doc_ids" value="{_e(doc['document_id'])}" {checked}
            style="width:14px;height:14px;accent-color:{col};flex-shrink:0;cursor:pointer"
            onchange="onDocToggle('{_e(doc['document_id'])}',this.checked)">
          <div style="width:28px;height:28px;border-radius:6px;background:{col_light};
            display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid {col}22">
            <span style="font-size:9px;font-weight:700;color:{col}">{ext_badge}</span>
          </div>
          <div style="flex:1;min-width:0">
            <div style="font-weight:600;font-size:12px;color:#0D1B2A;
              overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
              title="{_e(doc['original_filename'])}">{_e(doc['original_filename'])}</div>
            <div style="font-size:10px;color:#8896A5;margin-top:1px">{_fmt_size(doc.get('file_size',0))}</div>
          </div>
          <span style="width:8px;height:8px;border-radius:50%;background:{col};flex-shrink:0;opacity:{'1' if checked else '0.3'}"></span>
        </label>"""

    error_html = f"""
    <div style="position:absolute;top:0;left:0;right:0;z-index:30;
      padding:10px 16px;background:#FEE2E2;color:#991B1B;font-size:12px;
      border-bottom:1px solid rgba(153,27,27,0.2);display:flex;align-items:center;gap:8px">
      <svg width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
        <circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/>
      </svg>
      {_e(error)}
    </div>""" if error else ""

    no_models_hint = ""
    if not all_ifc_docs:
        no_models_hint = f"""
        <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
          text-align:center;z-index:10;pointer-events:none">
          <div style="background:rgba(255,255,255,0.95);border:1px solid #E2E8F0;
            border-radius:12px;padding:32px 40px;box-shadow:0 4px 20px rgba(13,27,42,0.1)">
            <div style="font-size:32px;margin-bottom:12px">📂</div>
            <div style="font-size:15px;font-weight:600;color:#0D1B2A;margin-bottom:6px">
              Keine IFC-Modelle vorhanden
            </div>
            <div style="font-size:13px;color:#4A5568">
              Bitte laden Sie zunächst IFC-Dateien im Documents-Modul hoch.
            </div>
          </div>
        </div>"""

    # ── CHANGE 2 ──────────────────────────────────────────────────────────────
    # "Laden" button is always visible so user can load models on first open.
    # Previously it was hidden (display:none) and only shown after a checkbox
    # change, which meant the button never appeared on a fresh empty viewer.
    laden_btn_display = "inline"
    # ─────────────────────────────────────────────────────────────────────────

    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "model")}

<div style="display:flex;flex-direction:column;height:calc(100vh - 94px);overflow:hidden;
  position:relative;background:#F0F2F5">
  {error_html}

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- ─── Sidebar ──────────────────────────────────────────────────────── -->
    <div id="sidebar" style="width:272px;min-width:272px;background:#FFFFFF;
      border-right:1px solid #E2E8F0;display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0;box-shadow:2px 0 8px rgba(13,27,42,0.04)">

      <!-- Modell-Bereich Header -->
      <div style="padding:10px 14px;font-size:10px;font-weight:700;background:#F8FAFC;
        color:#64748B;text-transform:uppercase;letter-spacing:.9px;
        border-bottom:1px solid #E2E8F0;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>📁 IFC-Modelle</span>
        <button onclick="applyDocSelection()" id="btn-apply-select"
          style="display:{laden_btn_display};font-size:10px;padding:3px 10px;background:#1E6FBF;
          border:none;color:#fff;border-radius:5px;cursor:pointer;font-weight:600;
          transition:opacity 0.15s" title="Auswahl übernehmen">
          ✓ Laden
        </button>
      </div>

      <!-- Dokumentenliste -->
      <div style="padding:6px 8px;border-bottom:1px solid #E2E8F0;flex-shrink:0;
        max-height:{'180px' if all_ifc_docs else '60px'};overflow-y:auto">
        <form id="model-select-form" method="GET" action="/projects/{pid}/view">
          {select_rows if select_rows else '<div style="padding:8px 4px;font-size:11px;color:#8896A5;font-style:italic">Keine Modelle im Documents-Modul.</div>'}
        </form>
        {f'<div style="padding:6px 4px;display:flex;gap:6px"><button type="button" onclick="toggleAllDocs(true)" style="flex:1;font-size:10px;padding:3px 0;background:#F1F5F9;border:1px solid #E2E8F0;border-radius:4px;cursor:pointer;color:#64748B">Alle</button><button type="button" onclick="toggleAllDocs(false)" style="flex:1;font-size:10px;padding:3px 0;background:#F1F5F9;border:1px solid #E2E8F0;border-radius:4px;cursor:pointer;color:#64748B">Keine</button></div>' if all_ifc_docs else ''}
      </div>

      <!-- IFC-Struktur Header -->
      <div style="padding:8px 14px;font-size:10px;font-weight:700;background:#F8FAFC;
        color:#64748B;text-transform:uppercase;letter-spacing:.9px;
        border-bottom:1px solid #E2E8F0;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>🏗 IFC-Struktur</span>
        <span style="display:flex;gap:8px">
          <button id="btn-cat-all" style="font-size:10px;cursor:pointer;color:#1E6FBF;
            background:none;border:none;padding:0;font-weight:600;opacity:0.7"
            onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.7'">Alle</button>
          <button id="btn-cat-none" style="font-size:10px;cursor:pointer;color:#1E6FBF;
            background:none;border:none;padding:0;font-weight:600;opacity:0.7"
            onmouseover="this.style.opacity='1'" onmouseout="this.style.opacity='0.7'">Keine</button>
        </span>
      </div>

      <!-- Kategorie-Baum -->
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:4px 0;background:#FAFBFC">
        <div style="padding:16px;font-size:12px;color:#8896A5;font-style:italic;text-align:center">
          Wird geladen…
        </div>
      </div>

      <!-- Status-Zeile -->
      <div id="load-status" style="padding:6px 14px;font-size:11px;color:#64748B;
        border-top:1px solid #E2E8F0;flex-shrink:0;background:#F8FAFC;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
        min-height:28px;display:flex;align-items:center"></div>
    </div>

    <!-- ─── Canvas-Bereich ──────────────────────────────────────────────── -->
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden;background:#F0F2F5">
      {no_models_hint}

      <canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>

      <!-- GlobalId-Suche -->
      <div id="search-bar" style="position:absolute;top:14px;left:14px;z-index:10;width:310px">
        <div style="display:flex;gap:5px">
          <div style="flex:1;position:relative">
            <svg style="position:absolute;left:10px;top:50%;transform:translateY(-50%);pointer-events:none"
              width="13" height="13" fill="none" stroke="#8896A5" stroke-width="2" viewBox="0 0 24 24">
              <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <input id="gid-search" type="text" placeholder="GlobalId suchen…"
              style="width:100%;background:rgba(255,255,255,0.97);border:1px solid #E2E8F0;
              color:#0D1B2A;padding:8px 10px 8px 30px;border-radius:9px;font-size:12px;
              outline:none;box-shadow:0 2px 10px rgba(13,27,42,0.08);
              font-family:'Inter',system-ui,sans-serif"
              autocomplete="off">
          </div>
          <button id="search-clear" style="display:none;background:rgba(255,255,255,0.97);
            border:1px solid #E2E8F0;color:#8896A5;border-radius:9px;
            padding:7px 11px;cursor:pointer;font-size:12px;
            box-shadow:0 2px 10px rgba(13,27,42,0.08)">✕</button>
        </div>
        <div id="search-results" style="display:none;margin-top:5px;
          background:rgba(255,255,255,0.98);border:1px solid #E2E8F0;
          border-radius:9px;max-height:280px;overflow-y:auto;
          box-shadow:0 6px 20px rgba(13,27,42,0.12)"></div>
      </div>

      <!-- Overlay-Buttons -->
      <div style="position:absolute;top:14px;right:14px;display:flex;gap:7px;z-index:6">
        <button id="btn-fit" style="font-size:12px;padding:7px 13px;background:rgba(255,255,255,0.97);
          border:1px solid #E2E8F0;border-radius:8px;cursor:pointer;color:#0D1B2A;
          box-shadow:0 2px 8px rgba(13,27,42,0.08);transition:all 0.12s;font-family:inherit">
          ⊡ Einpassen
        </button>
        <button id="btn-reset" style="font-size:12px;padding:7px 13px;background:rgba(255,255,255,0.97);
          border:1px solid #E2E8F0;border-radius:8px;cursor:pointer;color:#0D1B2A;
          box-shadow:0 2px 8px rgba(13,27,42,0.08);transition:all 0.12s;font-family:inherit">
          ⟳ Kamera
        </button>
        <button id="btn-show-all" style="font-size:12px;padding:7px 13px;display:none;
          background:rgba(254,242,242,0.97);border:1px solid rgba(220,38,38,0.3);
          border-radius:8px;cursor:pointer;color:#DC2626;
          box-shadow:0 2px 8px rgba(13,27,42,0.08);font-family:inherit">
          👁 Alle
        </button>
        <span id="hidden-count" style="font-size:11px;color:#DC2626;display:none;
          align-self:center;background:rgba(254,242,242,0.97);
          padding:5px 9px;border-radius:7px;border:1px solid rgba(220,38,38,0.2)"></span>
      </div>

      <!-- Steuerung-Hinweis -->
      <div style="position:absolute;bottom:14px;right:14px;font-size:10px;color:#8896A5;
        background:rgba(255,255,255,0.88);padding:5px 11px;border-radius:7px;
        border:1px solid #E2E8F0;pointer-events:none;backdrop-filter:blur(4px)">
        LMB Drehen · MMB Verschieben · Scroll Zoom · Leertaste Ausblenden
      </div>

      <!-- Lade-Overlay -->
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:rgba(240,242,245,0.96);z-index:20">
        <div style="width:46px;height:46px;border:3px solid #BFDBFE;
          border-top-color:#1E6FBF;border-radius:50%;
          animation:spin .7s linear infinite;margin-bottom:18px"></div>
        <p id="load-txt" style="color:#4A5568;font-size:13px;margin:0;font-family:inherit">
          Verbindung zu R2 wird hergestellt…
        </p>
        <div id="load-progress" style="margin-top:14px;width:240px;height:3px;
          background:#E2E8F0;border-radius:2px;overflow:hidden">
          <div id="load-bar" style="width:0%;height:100%;background:#1E6FBF;
            transition:width .4s ease;border-radius:2px"></div>
        </div>
        <p id="load-sub" style="color:#8896A5;font-size:11px;margin:8px 0 0;font-family:inherit"></p>
      </div>
    </div>

    <!-- ─── Info-Panel ──────────────────────────────────────────────────── -->
    <div id="info-panel" style="width:300px;min-width:300px;background:#FFFFFF;
      border-left:1px solid #E2E8F0;display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0;box-shadow:-2px 0 8px rgba(13,27,42,0.04);
      transition:width 0.2s ease">
      <div style="padding:10px 14px;font-size:10px;font-weight:700;background:#F8FAFC;
        color:#64748B;text-transform:uppercase;letter-spacing:.9px;
        border-bottom:1px solid #E2E8F0;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>Element-Info</span>
        <span id="info-close" style="cursor:pointer;color:#8896A5;font-size:16px;
          padding:0 4px;line-height:1" title="Schließen">✕</span>
      </div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:14px;font-size:12px">
        <div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0;line-height:1.6">
          Klicken Sie auf ein Element<br>für Details.
        </div>
      </div>
    </div>

  </div>
</div>

<style>
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
#btn-fit:hover, #btn-reset:hover {{
  background: rgba(255,255,255,1) !important;
  box-shadow: 0 3px 12px rgba(13,27,42,0.14) !important;
  transform: translateY(-1px);
}}
#cat-scroll::-webkit-scrollbar {{ width: 4px; }}
#cat-scroll::-webkit-scrollbar-thumb {{ background: #CBD5E1; border-radius: 2px; }}
#info-body::-webkit-scrollbar {{ width: 4px; }}
#info-body::-webkit-scrollbar-thumb {{ background: #CBD5E1; border-radius: 2px; }}
</style>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_direct_viewer_js(model_urls_js)}
</script>
"""
    return _page(f"{project['project_name']} – Direct Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Farbpalette und Kategorien
# ─────────────────────────────────────────────────────────────────────────────

def _direct_viewer_js(model_urls_js: str) -> str:
    return f"const MODEL_URLS = [{model_urls_js}];\n" + r"""

// ═══════════════════════════════════════════════════════════════════════════
// IFC-Typ → Farbe & Symbol Mapping (vollständig)
// ═══════════════════════════════════════════════════════════════════════════
const IFC_TYPE_STYLES = {
  // Tragende Struktur
  IfcWall:               { color: 0xa07840, lightColor: '#FEF3C7', icon: '▭', group: 'Tragwerk' },
  IfcWallStandardCase:   { color: 0xa07840, lightColor: '#FEF3C7', icon: '▭', group: 'Tragwerk' },
  IfcColumn:             { color: 0x2a5c8a, lightColor: '#DBEAFE', icon: '⬛', group: 'Tragwerk' },
  IfcColumnStandardCase: { color: 0x2a5c8a, lightColor: '#DBEAFE', icon: '⬛', group: 'Tragwerk' },
  IfcBeam:               { color: 0x3a78b8, lightColor: '#EFF6FF', icon: '━', group: 'Tragwerk' },
  IfcBeamStandardCase:   { color: 0x3a78b8, lightColor: '#EFF6FF', icon: '━', group: 'Tragwerk' },
  IfcSlab:               { color: 0x5a8ea8, lightColor: '#E0F2FE', icon: '▬', group: 'Tragwerk' },
  IfcSlabStandardCase:   { color: 0x5a8ea8, lightColor: '#E0F2FE', icon: '▬', group: 'Tragwerk' },
  IfcFooting:            { color: 0x1a4060, lightColor: '#0C4A6E22', icon: '⬜', group: 'Tragwerk' },
  IfcPile:               { color: 0x103050, lightColor: '#082F4922', icon: '↓', group: 'Tragwerk' },
  IfcMember:             { color: 0x3878a0, lightColor: '#E0F2FE', icon: '╱', group: 'Tragwerk' },
  IfcPlate:              { color: 0x6090a8, lightColor: '#E0F2FE', icon: '▱', group: 'Tragwerk' },

  // Hülle & Dach
  IfcRoof:               { color: 0x6b3db8, lightColor: '#EDE9FE', icon: '⌂', group: 'Hülle' },
  IfcCurtainWall:        { color: 0xc09030, lightColor: '#FEF9C3', icon: '⧉', group: 'Hülle' },
  IfcCovering:           { color: 0x608858, lightColor: '#DCFCE7', icon: '≡', group: 'Hülle' },
  IfcRailing:            { color: 0x405860, lightColor: '#F0F9FF', icon: '⁞', group: 'Hülle' },

  // Öffnungen
  IfcDoor:               { color: 0xb85030, lightColor: '#FEE2E2', icon: '🚪', group: 'Öffnungen' },
  IfcWindow:             { color: 0x40b8d0, lightColor: '#CFFAFE', icon: '⬡', group: 'Öffnungen' },
  IfcOpeningElement:     { color: 0xdddddd, lightColor: '#F8FAFC', icon: '○', group: 'Öffnungen' },

  // Erschließung
  IfcStair:              { color: 0x9a5030, lightColor: '#FEF2E2', icon: '𝌊', group: 'Erschließung' },
  IfcStairFlight:        { color: 0x9a5030, lightColor: '#FEF2E2', icon: '𝌊', group: 'Erschließung' },
  IfcRamp:               { color: 0xb88820, lightColor: '#FEFCE8', icon: '⟋', group: 'Erschließung' },

  // Ausbau & Innenraum
  IfcFurnishingElement:  { color: 0xb87050, lightColor: '#FEF2E2', icon: '⊡', group: 'Ausbau' },
  IfcFurniture:          { color: 0xb87050, lightColor: '#FEF2E2', icon: '⊡', group: 'Ausbau' },
  IfcSpace:              { color: 0x88c888, lightColor: '#F0FDF4', icon: '□', group: 'Ausbau' },

  // TGA
  IfcPipeSegment:        { color: 0x108870, lightColor: '#ECFDF5', icon: '⊃', group: 'TGA' },
  IfcDuctSegment:        { color: 0x506870, lightColor: '#F0F9FF', icon: '▷', group: 'TGA' },
  IfcCableSegment:       { color: 0xe09810, lightColor: '#FEFCE8', icon: '⌇', group: 'TGA' },
  IfcPump:               { color: 0x3040a0, lightColor: '#EFF6FF', icon: '⊕', group: 'TGA' },
  IfcFan:                { color: 0x4858a8, lightColor: '#EFF6FF', icon: '✺', group: 'TGA' },
  IfcValve:              { color: 0x50b898, lightColor: '#ECFDF5', icon: '⊗', group: 'TGA' },
  IfcSensor:             { color: 0xe080a8, lightColor: '#FDF2F8', icon: '◉', group: 'TGA' },
  IfcFlowTerminal:       { color: 0x40a880, lightColor: '#ECFDF5', icon: '⊸', group: 'TGA' },
  IfcFlowSegment:        { color: 0x508898, lightColor: '#E0F7FF', icon: '⊶', group: 'TGA' },

  // Sonstige
  IfcBuildingElementProxy: { color: 0x707080, lightColor: '#F8FAFC', icon: '?', group: 'Sonstiges' },
  IfcAnnotation:           { color: 0x888888, lightColor: '#F8FAFC', icon: '✎', group: 'Sonstiges' },
  IfcGrid:                 { color: 0xaaaaaa, lightColor: '#F8FAFC', icon: '⊞', group: 'Sonstiges' },
};

const FLAT_TYPES = new Set(["IfcOpeningElement","IfcAnnotation","IfcGrid","IfcSpace"]);

function getTypeStyle(t) {
  if (IFC_TYPE_STYLES[t]) return IFC_TYPE_STYLES[t];
  // Fallback-Gruppen
  if (/^IfcWall/.test(t))    return { color: 0xa07840, lightColor: '#FEF3C7', icon: '▭', group: 'Tragwerk' };
  if (/^IfcSlab/.test(t))    return { color: 0x5a8ea8, lightColor: '#E0F2FE', icon: '▬', group: 'Tragwerk' };
  if (/^IfcColumn/.test(t))  return { color: 0x2a5c8a, lightColor: '#DBEAFE', icon: '⬛', group: 'Tragwerk' };
  if (/^IfcBeam/.test(t))    return { color: 0x3a78b8, lightColor: '#EFF6FF', icon: '━', group: 'Tragwerk' };
  if (/^IfcDoor/.test(t))    return { color: 0xb85030, lightColor: '#FEE2E2', icon: '🚪', group: 'Öffnungen' };
  if (/^IfcWindow/.test(t))  return { color: 0x40b8d0, lightColor: '#CFFAFE', icon: '⬡', group: 'Öffnungen' };
  if (/^IfcStair/.test(t))   return { color: 0x9a5030, lightColor: '#FEF2E2', icon: '𝌊', group: 'Erschließung' };
  if (/^IfcRoof/.test(t))    return { color: 0x6b3db8, lightColor: '#EDE9FE', icon: '⌂', group: 'Hülle' };
  if (/^IfcPipe/.test(t))    return { color: 0x108870, lightColor: '#ECFDF5', icon: '⊃', group: 'TGA' };
  if (/^IfcDuct/.test(t))    return { color: 0x506870, lightColor: '#F0F9FF', icon: '▷', group: 'TGA' };
  if (/^IfcCable/.test(t))   return { color: 0xe09810, lightColor: '#FEFCE8', icon: '⌇', group: 'TGA' };
  if (/^IfcFurnish/.test(t)) return { color: 0xb87050, lightColor: '#FEF2E2', icon: '⊡', group: 'Ausbau' };
  if (/^IfcFlow/.test(t))    return { color: 0x40a880, lightColor: '#ECFDF5', icon: '⊸', group: 'TGA' };
  return { color: 0x607080, lightColor: '#F8FAFC', icon: '◆', group: 'Sonstiges' };
}

function getColor(t) {
  return new THREE.Color(getTypeStyle(t).color);
}

// ═══════════════════════════════════════════════════════════════════════════
// Three.js Setup (helles Theme)
// ═══════════════════════════════════════════════════════════════════════════
const canvas   = document.getElementById("three-canvas");
const wrap     = document.getElementById("canvas-wrap");
const loadEl   = document.getElementById("loading");
const loadTxt  = document.getElementById("load-txt");
const loadSub  = document.getElementById("load-sub");
const loadBar  = document.getElementById("load-bar");
const infoBody = document.getElementById("info-body");
const loadStatus = document.getElementById("load-status");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf0f2f5);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);

// Beleuchtung für helles Theme
scene.add(new THREE.AmbientLight(0xffffff, 0.72));
const dirL1 = new THREE.DirectionalLight(0xffffff, 0.75);
dirL1.position.set(60, 100, 60);
scene.add(dirL1);
const dirL2 = new THREE.DirectionalLight(0xffffff, 0.28);
dirL2.position.set(-40, 40, -40);
scene.add(dirL2);
scene.add(new THREE.HemisphereLight(0xeef4ff, 0xd4c8a0, 0.38));

// Raster
const gridHelper = new THREE.GridHelper(200, 40, 0xcccccc, 0xe0e0e0);
scene.add(gridHelper);

function onResize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", onResize);
onResize();

// ── Orbit-Steuerung ────────────────────────────────────────────────────────
const orb = {
  sph: new THREE.Spherical(80, Math.PI/4, Math.PI/4),
  tgt: new THREE.Vector3(),
  drag: false, pan: false, lx: 0, ly: 0,
};
function applyOrb() {
  camera.position.copy(new THREE.Vector3().setFromSpherical(orb.sph).add(orb.tgt));
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
    const r = new THREE.Vector3().crossVectors(
      camera.getWorldDirection(new THREE.Vector3()), camera.up).normalize();
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
const modelMeshes = {};   // docId → Mesh[]
const modelGroups = {};   // docId → THREE.Group
const catVisible  = {};   // "docId:type" → bool
const catCounts   = {};   // "docId:type" → count
const catElements = {};   // "docId:type" → [{name, expressId, globalId}]
const hiddenIds   = new Set();
const docMeta     = {};   // docId → {label, color}

function catKey(docId, type) { return docId + "::" + type; }
function allMeshes() { return Object.values(modelMeshes).flat(); }

// ── Sichtbarkeit ────────────────────────────────────────────────────────────
function applyVisibility() {
  for (const m of allMeshes()) {
    const key = catKey(m.userData.docId, m.userData.ifcType);
    const catOn = catVisible[key] !== false;
    const notHidden = !hiddenIds.has(m.userData.expressId);
    m.visible = catOn && notHidden;
  }
  updateHiddenCount();
}

function updateHiddenCount() {
  const n = hiddenIds.size;
  const el = document.getElementById("hidden-count");
  const btn = document.getElementById("btn-show-all");
  if (n > 0) {
    el.textContent = `${n} ausgeblendet`;
    el.style.display = "inline";
    if (btn) btn.style.display = "inline";
  } else {
    el.style.display = "none";
    if (btn) btn.style.display = "none";
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// IFC-Kategorie-Baum (nach Gruppe → Typ → Elemente)
// ═══════════════════════════════════════════════════════════════════════════
function buildCategoryUI() {
  const list = document.getElementById("cat-scroll");
  if (!list) return;
  list.innerHTML = "";

  // Daten strukturieren: docId → Gruppe → Typ → Count
  const byDoc = {};
  for (const key of Object.keys(catCounts)) {
    const sep = key.indexOf("::");
    const docId = key.slice(0, sep);
    const type  = key.slice(sep + 2);
    if (!byDoc[docId]) byDoc[docId] = {};
    byDoc[docId][type] = catCounts[key];
  }

  for (const docId of Object.keys(byDoc)) {
    const meta = docMeta[docId] || {};
    const col  = meta.color || "#1E6FBF";
    const lbl  = meta.label || docId.slice(0, 12);

    // ── Ebene 1: Datei-Header ──────────────────────────────────────────────
    const fileHeader = document.createElement("div");
    fileHeader.style.cssText = `
      padding:7px 12px;background:linear-gradient(135deg,${col}18,${col}08);
      border-bottom:1px solid ${col}22;border-top:1px solid ${col}22;
      display:flex;align-items:center;gap:7px;cursor:pointer;user-select:none;
      flex-shrink:0;position:sticky;top:0;z-index:2;margin-top:2px`;
    fileHeader.innerHTML =
      `<span class="ftog" style="color:${col};font-size:9px;width:10px;flex-shrink:0;transition:transform 0.15s">▼</span>` +
      `<span style="width:10px;height:10px;border-radius:3px;background:${col};flex-shrink:0"></span>` +
      `<span style="font-size:11px;font-weight:700;color:#0D1B2A;flex:1;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap" title="${esc(lbl)}">${esc(lbl)}</span>` +
      `<span style="font-size:9px;color:${col};font-weight:600;background:${col}18;
        padding:1px 6px;border-radius:10px;flex-shrink:0">${Object.values(byDoc[docId]).reduce((a,b)=>a+b,0)}</span>`;
    list.appendChild(fileHeader);

    // Typen nach Gruppe sortieren
    const groupedTypes = {};
    for (const type of Object.keys(byDoc[docId]).sort()) {
      const style = getTypeStyle(type);
      const grp = style.group || 'Sonstiges';
      if (!groupedTypes[grp]) groupedTypes[grp] = [];
      groupedTypes[grp].push(type);
    }

    const docBody = document.createElement("div");
    docBody.style.cssText = "padding:4px 0 6px";

    for (const groupName of Object.keys(groupedTypes).sort()) {
      const typesInGroup = groupedTypes[groupName];

      // ── Ebene 2: Gruppen-Header ────────────────────────────────────────
      const groupHeader = document.createElement("div");
      groupHeader.style.cssText = `
        padding:5px 12px 3px 22px;font-size:9px;font-weight:700;
        color:#94A3B8;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;
        margin-top:4px`;

      const groupIcon = _getGroupIcon(groupName);
      groupHeader.innerHTML =
        `<span class="gtog" style="font-size:8px;color:#CBD5E1;transition:transform 0.15s">▼</span>` +
        `<span>${groupIcon}</span>` +
        `<span style="flex:1">${esc(groupName)}</span>` +
        `<span style="font-size:9px;color:#CBD5E1;font-weight:500">
          ${typesInGroup.reduce((a,t)=>a+byDoc[docId][t],0)}
        </span>`;
      docBody.appendChild(groupHeader);

      const groupBody = document.createElement("div");
      groupBody.style.cssText = "padding:0";

      for (const type of typesInGroup) {
        const key    = catKey(docId, type);
        const vis    = catVisible[key] !== false;
        const style  = getTypeStyle(type);
        const tColHex = "#" + new THREE.Color(style.color).getHexString();
        const count  = byDoc[docId][type];
        const isFlat = FLAT_TYPES.has(type);
        const elems  = catElements[key] || [];

        // ── Ebene 3: Typ-Zeile ───────────────────────────────────────────
        const typeRow = document.createElement("div");
        typeRow.style.cssText = `
          display:flex;align-items:center;gap:6px;padding:4px 10px 4px 28px;
          cursor:pointer;border-bottom:1px solid #F1F5F9;
          opacity:${vis ? "1" : ".4"};transition:all 0.12s;user-select:none`;
        typeRow.dataset.catKey = key;

        const shortName = type.replace(/^Ifc/, '');
        typeRow.innerHTML =
          `<span class="ttog" style="font-size:8px;color:#CBD5E1;width:8px;flex-shrink:0">▶</span>` +
          `<input class="cat-cb" type="checkbox" ${vis ? "checked" : ""}
             data-cat-key="${esc(key)}"
             style="width:12px;height:12px;accent-color:${tColHex};flex-shrink:0;cursor:pointer">` +
          `<span style="width:10px;height:10px;border-radius:${isFlat ? '0' : '50%'};
             background:${isFlat ? 'transparent' : tColHex};
             border:${isFlat ? '1.5px dashed #CBD5E1' : '2px solid ' + tColHex + '33'};
             flex-shrink:0;display:inline-block"></span>` +
          `<span style="font-size:11px;color:${isFlat ? '#94A3B8' : '#1E293B'};
             flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:500"
             title="Ifc${esc(shortName)}">${esc(shortName)}</span>` +
          `<span style="font-size:10px;color:#94A3B8;flex-shrink:0;
             background:#F1F5F9;padding:1px 5px;border-radius:8px">${count}</span>`;

        groupBody.appendChild(typeRow);

        // ── Ebene 4: Element-Liste (eingeklappt) ─────────────────────────
        const elemList = document.createElement("div");
        elemList.style.display = "none";
        elemList.style.cssText += "padding:0;background:#FAFBFD;border-bottom:1px solid #E2E8F0";

        for (const el of elems.slice(0, 100)) {
          const eRow = document.createElement("div");
          eRow.style.cssText = `
            padding:3px 10px 3px 44px;font-size:10px;color:#475569;
            cursor:pointer;border-bottom:1px solid #F1F5F9;
            white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
            display:flex;align-items:center;gap:5px`;
          eRow.title = (el.name || el.ifcType) + (el.globalId ? ' · ' + el.globalId : '');
          eRow.dataset.expressId = el.expressId;
          eRow.innerHTML =
            `<span style="color:${tColHex};font-size:8px;flex-shrink:0">${style.icon || '◆'}</span>` +
            `<span style="overflow:hidden;text-overflow:ellipsis">${esc(el.name || '(Kein Name)')}</span>`;
          eRow.addEventListener("mouseenter", () => eRow.style.background = "#EFF6FF");
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
                orb.sph.radius = Math.max(box.getSize(new THREE.Vector3()).length() * 2.8, 2);
                applyOrb();
              }
            }
          });
          elemList.appendChild(eRow);
        }
        if (elems.length > 100) {
          const moreRow = document.createElement("div");
          moreRow.style.cssText = "padding:4px 44px;font-size:10px;color:#94A3B8;font-style:italic";
          moreRow.textContent = `… ${elems.length - 100} weitere Elemente`;
          elemList.appendChild(moreRow);
        }
        groupBody.appendChild(elemList);

        // Typ-Zeile: Auf-/Zuklappen der Element-Liste
        typeRow.addEventListener("click", e => {
          if (e.target.classList.contains("cat-cb")) return;
          const tog = typeRow.querySelector(".ttog");
          const collapsed = elemList.style.display === "none";
          elemList.style.display = collapsed ? "block" : "none";
          tog.style.color = collapsed ? "#64748B" : "#CBD5E1";
          tog.textContent = collapsed ? "▼" : "▶";
          typeRow.style.background = collapsed ? "#F0F7FF" : "";
        });

        // Hover
        typeRow.addEventListener("mouseenter", () => {
          if (elemList.style.display === "none") typeRow.style.background = "#F8FAFC";
        });
        typeRow.addEventListener("mouseleave", () => {
          if (elemList.style.display === "none") typeRow.style.background = "";
        });
      }

      docBody.appendChild(groupBody);

      // Gruppen-Header: Auf-/Zuklappen
      groupHeader.addEventListener("click", () => {
        const tog = groupHeader.querySelector(".gtog");
        const collapsed = groupBody.style.display === "none";
        groupBody.style.display = collapsed ? "" : "none";
        tog.style.transform = collapsed ? "" : "rotate(-90deg)";
      });
    }

    list.appendChild(docBody);

    // Datei-Header: Auf-/Zuklappen
    fileHeader.addEventListener("click", () => {
      const tog = fileHeader.querySelector(".ftog");
      const collapsed = docBody.style.display === "none";
      docBody.style.display = collapsed ? "" : "none";
      tog.style.transform = collapsed ? "" : "rotate(-90deg)";
    });
  }

  // Checkbox-Delegation
  list.addEventListener("change", e => {
    if (!e.target.classList.contains("cat-cb")) return;
    const key = e.target.dataset.catKey;
    catVisible[key] = e.target.checked;
    const row = e.target.closest("[data-cat-key]");
    if (row) row.style.opacity = e.target.checked ? "1" : ".4";
    applyVisibility();
  });
}

function _getGroupIcon(group) {
  const icons = {
    'Tragwerk': '🏗', 'Hülle': '🏠', 'Öffnungen': '🚪',
    'Erschließung': '🔼', 'Ausbau': '🪑', 'TGA': '⚙',
    'Sonstiges': '📦',
  };
  return icons[group] || '📦';
}

function setCatAll(v) {
  Object.keys(catCounts).forEach(k => catVisible[k] = v);
  buildCategoryUI();
  applyVisibility();
}

document.getElementById("btn-cat-all").addEventListener("click",  () => setCatAll(true));
document.getElementById("btn-cat-none").addEventListener("click", () => setCatAll(false));

// ═══════════════════════════════════════════════════════════════════════════
// web-ifc
// ═══════════════════════════════════════════════════════════════════════════
let webIfc = null;

async function initWebIfc() {
  const mod = await import("https://esm.sh/web-ifc@0.0.57");
  webIfc = new mod.IfcAPI();
  webIfc.SetWasmPath("https://esm.sh/web-ifc@0.0.57/");
  await webIfc.Init();
}

// ═══════════════════════════════════════════════════════════════════════════
// Modell laden
// ═══════════════════════════════════════════════════════════════════════════
async function loadModel(cfg, index, total) {
  if (loadTxt) loadTxt.textContent = `Lade ${cfg.label}…`;
  if (loadSub) loadSub.textContent = `Modell ${index} von ${total}`;
  if (loadBar) loadBar.style.width = `${((index-1)/total)*80}%`;

  const resp = await fetch(cfg.url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} für ${cfg.label}`);
  const data = new Uint8Array(await resp.arrayBuffer());

  if (loadBar) loadBar.style.width = `${((index-1)/total)*80 + 60/total}%`;

  const modelId = webIfc.OpenModel(data, { COORDINATE_TO_ORIGIN: false, USE_FAST_BOOLS: false });

  // Alle Linien indexieren
  const elemIndex = {};
  const allLines = webIfc.GetAllLines(modelId);
  for (let i = 0; i < allLines.size(); i++) {
    const id = allLines.get(i);
    try { const l = webIfc.GetLine(modelId, id, false); if (l) elemIndex[id] = l; } catch(_) {}
  }

  // Typnamen auflösen
  const _tnc = {};
  function resolveTypeCode(code) {
    if (_tnc[code] !== undefined) return _tnc[code];
    let name = "Unknown";
    try {
      const raw = webIfc.GetNameFromTypeCode(code);
      if (raw) {
        const low = raw.toLowerCase().replace(/^ifc_/, "");
        const parts = low.split("_");
        name = "Ifc" + parts.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join("");
      }
    } catch(_) {}
    _tnc[code] = name; return name;
  }

  function typeName(line) {
    if (!line) return "Unknown";
    if (typeof line.type === "number") return resolveTypeCode(line.type);
    if (typeof line.type === "string" && line.type.startsWith("Ifc")) return line.type;
    const cn = line.constructor?.name ?? "";
    if (cn.startsWith("Ifc")) return cn;
    return "Unknown";
  }

  function sv(v) {
    if (v == null) return "";
    if (typeof v === "object" && v.value !== undefined) return String(v.value);
    return String(v);
  }

  // RelDefinesByProperties → PSets
  const relMap = {};
  for (const [id, line] of Object.entries(elemIndex)) {
    if (!typeName(line).toLowerCase().includes("reldefinesbyprop")) continue;
    const pref = line.RelatingPropertyDefinition; if (!pref) continue;
    const pid  = pref.value ?? pref;
    const rels = line.RelatedObjects; if (!rels) continue;
    const ids  = Array.isArray(rels) ? rels : [rels];
    for (const r of ids) {
      const rid = r?.value ?? r;
      if (!relMap[rid]) relMap[rid] = [];
      relMap[rid].push(pid);
    }
  }

  function getPsets(eid) {
    const res = {};
    for (const pid of relMap[eid] ?? []) {
      const pset = elemIndex[pid]; if (!pset) continue;
      const pn = sv(pset.Name) || typeName(pset);
      const props = {};
      const hp = pset.HasProperties;
      if (hp) {
        const list = Array.isArray(hp) ? hp : [hp];
        for (const ref of list) {
          const id = ref?.value ?? ref; const prop = elemIndex[id]; if (!prop) continue;
          props[sv(prop.Name) || String(id)] = prop.NominalValue != null ? sv(prop.NominalValue) : "–";
        }
      }
      res[pn] = props;
    }
    return res;
  }

  const docId = cfg.documentId;
  const group = new THREE.Group();
  group.name = cfg.label;
  scene.add(group);
  modelGroups[docId] = group;
  modelMeshes[docId] = [];
  docMeta[docId] = { label: cfg.label, color: cfg.color };

  const fms = webIfc.LoadAllGeometry(modelId);
  let vertCount = 0;
  const seen = new Set();

  for (let i = 0; i < fms.size(); i++) {
    const fm = fms.get(i);
    const expId = fm.expressID;
    const line = elemIndex[expId];
    const tName = typeName(line);
    const typeStyle = getTypeStyle(tName);
    const isFlat = FLAT_TYPES.has(tName);
    const tCol = isFlat ? new THREE.Color(0xcccccc) : new THREE.Color(typeStyle.color);
    const key = catKey(docId, tName);

    if (!seen.has(expId)) {
      seen.add(expId);
      catCounts[key] = (catCounts[key] ?? 0) + 1;
      if (catVisible[key] === undefined) catVisible[key] = !isFlat;
      if (!catElements[key]) catElements[key] = [];
      catElements[key].push({
        name: sv(line?.Name) || sv(line?.GlobalId) || String(expId),
        expressId: expId,
        globalId: sv(line?.GlobalId),
        ifcType: tName,
      });
    }

    const meta = {
      expressId: expId, ifcType: tName,
      name: sv(line?.Name), globalId: sv(line?.GlobalId),
      objectType: sv(line?.ObjectType), description: sv(line?.Description),
      tag: sv(line?.Tag), docId: docId, modelLabel: cfg.label,
      slotColor: cfg.color, psets: getPsets(expId), isFlat,
      typeStyle: typeStyle,
    };

    const mat = new THREE.MeshLambertMaterial({
      color: tCol.clone(), transparent: true,
      opacity: isFlat ? 0.28 : 0.88,
      wireframe: isFlat, side: THREE.DoubleSide,
    });

    const pgs = fm.geometries;
    for (let j = 0; j < pgs.size(); j++) {
      const pg = pgs.get(j);
      const gd = webIfc.GetGeometry(modelId, pg.geometryExpressID);
      const vs = webIfc.GetVertexArray(gd.GetVertexData(), gd.GetVertexDataSize());
      const idx = webIfc.GetIndexArray(gd.GetIndexData(), gd.GetIndexDataSize());
      if (!vs || vs.length === 0) { gd.delete(); continue; }
      const S = 6;
      const pa = new Float32Array(vs.length/S*3);
      const na = new Float32Array(vs.length/S*3);
      for (let k = 0; k < vs.length/S; k++) {
        pa[k*3]=vs[k*S]; pa[k*3+1]=vs[k*S+1]; pa[k*3+2]=vs[k*S+2];
        na[k*3]=vs[k*S+3]; na[k*3+1]=vs[k*S+4]; na[k*3+2]=vs[k*S+5];
      }
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(pa, 3));
      geo.setAttribute("normal",   new THREE.BufferAttribute(na, 3));
      geo.setIndex(new THREE.BufferAttribute(idx, 1));
      const mesh = new THREE.Mesh(geo, mat.clone());
      mesh.applyMatrix4(new THREE.Matrix4().fromArray(pg.flatTransformation));
      mesh.userData = { ...meta };
      mesh.visible = !isFlat;
      group.add(mesh);
      modelMeshes[docId].push(mesh);
      vertCount += pa.length / 3;
      gd.delete();
    }
  }
  webIfc.CloseModel(modelId);
  if (loadStatus) loadStatus.textContent = `✓ ${cfg.label}: ${vertCount.toLocaleString()} Punkte`;
  return vertCount;
}

// ═══════════════════════════════════════════════════════════════════════════
// Fit / Reset
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

// ═══════════════════════════════════════════════════════════════════════════
// Info-Panel (helles Theme)
// ═══════════════════════════════════════════════════════════════════════════
let selectedMesh = null;
let mouseMoved   = false;

function showInfo(m) {
  const d = m.userData;
  const ts = d.typeStyle || getTypeStyle(d.ifcType);
  const tColHex = "#" + new THREE.Color(ts.color).getHexString();
  const tColLight = ts.lightColor || '#F8FAFC';

  let h = `
<div style="font-size:11px;font-weight:700;color:${d.slotColor};margin-bottom:12px;
  padding-bottom:10px;border-bottom:1px solid #E2E8F0;
  display:flex;align-items:center;gap:6px">
  <span style="width:8px;height:8px;border-radius:50%;background:${d.slotColor};flex-shrink:0"></span>
  ${esc(d.modelLabel)}
</div>

<div style="background:${tColLight};border:1px solid ${tColHex}33;border-radius:8px;
  padding:8px 10px;margin-bottom:10px;display:flex;align-items:center;gap:8px">
  <span style="font-size:16px">${ts.icon || '◆'}</span>
  <div>
    <div style="font-size:12px;font-weight:700;color:${tColHex}">${esc(d.ifcType.replace(/^Ifc/,''))}</div>
    <div style="font-size:10px;color:#64748B">${ts.group || 'Sonstiges'}</div>
  </div>
</div>

<div style="display:flex;flex-direction:column;gap:5px;margin-bottom:10px">`;

  const fields = [
    ["GlobalId",   d.globalId],
    ["Name",       d.name],
    ["Express-ID", String(d.expressId)],
    ["ObjectType", d.objectType],
  ];
  for (const [label, value] of fields) {
    if (!value) continue;
    h += `
<div style="display:flex;gap:6px;align-items:flex-start">
  <span style="color:#94A3B8;min-width:80px;flex-shrink:0;font-size:10px;
    padding-top:1px;font-weight:500">${esc(label)}</span>
  <span style="color:#0D1B2A;font-size:11px;word-break:break-all;
    font-family:${label==='GlobalId'?'monospace':'inherit'}">${esc(value)}</span>
</div>`;
  }
  h += `</div>`;

  const psets = d.psets || {};
  if (Object.keys(psets).length) {
    h += `<div style="border-top:1px solid #E2E8F0;padding-top:10px;margin-top:4px;
      font-size:10px;font-weight:700;color:#64748B;text-transform:uppercase;
      letter-spacing:.5px;margin-bottom:8px">Eigenschaften</div>`;
    for (const [pn, props] of Object.entries(psets)) {
      const filteredProps = Object.entries(props).filter(([k]) => k !== "id");
      if (!filteredProps.length) continue;
      h += `<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:7px;
        margin-bottom:6px;overflow:hidden">
        <div style="padding:5px 10px;background:#F1F5F9;font-size:10px;font-weight:700;
          color:#475569;border-bottom:1px solid #E2E8F0">${esc(pn)}</div>`;
      for (const [k, v] of filteredProps) {
        h += `<div style="display:flex;gap:6px;padding:4px 10px;border-bottom:1px solid #F1F5F9">
          <span style="color:#94A3B8;font-size:10px;min-width:100px;flex-shrink:0">${esc(k)}</span>
          <span style="color:#374151;font-size:10px;word-break:break-all">${esc(String(v))}</span>
        </div>`;
      }
      h += `</div>`;
    }
  }

  infoBody.innerHTML = h;
}

// ── Raycasting ─────────────────────────────────────────────────────────────
const raycaster = new THREE.Raycaster();
const mouse     = new THREE.Vector2();
const HIGHLIGHT = new THREE.Color(0xff6600);

canvas.addEventListener("mousedown", () => { mouseMoved = false; });
canvas.addEventListener("mousemove", () => { mouseMoved = true; });
canvas.addEventListener("mouseup", e => {
  if (e.button !== 0 || mouseMoved) return;
  const rect = canvas.getBoundingClientRect();
  mouse.x =  ((e.clientX - rect.left) / rect.width)  * 2 - 1;
  mouse.y = -((e.clientY - rect.top)  / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(allMeshes().filter(m => m.visible), false);
  if (hits.length > 0) {
    const m = hits[0].object;
    if (selectedMesh && selectedMesh !== m)
      selectedMesh.material.color.copy(selectedMesh.userData._origColor);
    if (!m.userData._origColor) m.userData._origColor = m.material.color.clone();
    m.material.color.copy(HIGHLIGHT);
    selectedMesh = m;
    showInfo(m);
    const panel = document.getElementById("info-panel");
    panel.style.width = "300px";
    panel.style.minWidth = "300px";
  } else {
    if (selectedMesh) {
      selectedMesh.material.color.copy(selectedMesh.userData._origColor);
      selectedMesh = null;
    }
    infoBody.innerHTML = '<div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0;line-height:1.6">Klicken Sie auf ein Element<br>für Details.</div>';
  }
});

function selectMesh(m) {
  if (selectedMesh && selectedMesh !== m)
    selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  if (!m.userData._origColor) m.userData._origColor = m.material.color.clone();
  m.material.color.copy(HIGHLIGHT);
  selectedMesh = m;
  showInfo(m);
  const panel = document.getElementById("info-panel");
  panel.style.width = "300px";
  panel.style.minWidth = "300px";
}

// Leertaste → Element ausblenden
window.addEventListener("keydown", e => {
  if (e.code !== "Space" || !selectedMesh) return;
  e.preventDefault();
  const id = selectedMesh.userData.expressId;
  hiddenIds.add(id);
  for (const m of allMeshes()) if (m.userData.expressId === id) m.visible = false;
  selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  selectedMesh = null;
  infoBody.innerHTML = '<div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0">Element ausgeblendet.</div>';
  updateHiddenCount();
});

// ── Buttons ──────────────────────────────────────────────────────────────
document.getElementById("btn-fit").addEventListener("click", fitAll);
document.getElementById("btn-reset").addEventListener("click", () => {
  orb.tgt.set(0,0,0); orb.sph.set(80, Math.PI/4, Math.PI/4); applyOrb();
});
document.getElementById("btn-show-all").addEventListener("click", () => {
  hiddenIds.clear(); applyVisibility();
});
document.getElementById("info-close").addEventListener("click", () => {
  const p = document.getElementById("info-panel");
  p.style.width = "0"; p.style.minWidth = "0";
});

// ═══════════════════════════════════════════════════════════════════════════
// Modellauswahl in der Sidebar
// ═══════════════════════════════════════════════════════════════════════════
function onDocToggle(docId, checked) {
  // "Laden" button is always visible; just update the label styling
  const lbl = document.getElementById(`lbl-${docId}`);
  if (lbl) {
    lbl.style.background = checked ? "rgba(30,111,191,0.06)" : "transparent";
    lbl.style.borderColor = checked ? "rgba(30,111,191,0.2)" : "transparent";
    const dot = lbl.querySelector("span:last-child");
    if (dot) dot.style.opacity = checked ? "1" : "0.3";
  }
}

function applyDocSelection() {
  const form = document.getElementById("model-select-form");
  if (!form) return;
  const checked = [...form.querySelectorAll("input[name=doc_ids]:checked")].map(i => i.value);
  const url = new URL(window.location.href);
  url.searchParams.delete("doc_ids");
  checked.forEach(id => url.searchParams.append("doc_ids", id));
  window.location.href = url.toString();
}

function toggleAllDocs(v) {
  const form = document.getElementById("model-select-form");
  if (!form) return;
  form.querySelectorAll("input[name=doc_ids]").forEach(c => {
    c.checked = v;
    const docId = c.value;
    onDocToggle(docId, v);
  });
}

document.getElementById("btn-apply-select")?.addEventListener("click", applyDocSelection);

// ═══════════════════════════════════════════════════════════════════════════
// GlobalId-Suche
// ═══════════════════════════════════════════════════════════════════════════
const searchInput   = document.getElementById("gid-search");
const searchResults = document.getElementById("search-results");
const searchClear   = document.getElementById("search-clear");
const searchIndex   = [];

function buildSearchIndex() {
  searchIndex.length = 0;
  const seen = new Set();
  for (const m of allMeshes()) {
    const gid = m.userData.globalId;
    if (!gid || seen.has(gid)) continue;
    seen.add(gid);
    searchIndex.push({
      globalId: gid, expressId: m.userData.expressId,
      name: m.userData.name, ifcType: m.userData.ifcType,
      docId: m.userData.docId, modelLabel: m.userData.modelLabel,
      slotColor: m.userData.slotColor, typeStyle: m.userData.typeStyle,
    });
  }
}

function renderSearch(q) {
  q = q.trim().toLowerCase();
  if (!q) {
    searchResults.style.display = "none";
    searchClear.style.display = "none";
    return;
  }
  searchClear.style.display = "inline";
  const hits = searchIndex.filter(e => e.globalId.toLowerCase().includes(q)).slice(0, 50);

  if (!hits.length) {
    searchResults.innerHTML = '<div style="padding:10px 14px;font-size:12px;color:#94A3B8">Keine Ergebnisse</div>';
    searchResults.style.display = "block";
    return;
  }

  let html = `<div style="padding:6px 14px;font-size:10px;color:#1E6FBF;font-weight:600;
    border-bottom:1px solid #E2E8F0">${hits.length} Treffer</div>`;

  html += hits.map(el => {
    const col = el.slotColor || "#1E6FBF";
    const ts = el.typeStyle || getTypeStyle(el.ifcType);
    const tColHex = "#" + new THREE.Color(ts.color).getHexString();
    const gidHl = el.globalId.toLowerCase().replace(q,
      `<span style="background:#FEF3C7;color:#92400E;border-radius:2px">${esc(q)}</span>`);
    return `<div class="s-row" data-gid="${esc(el.globalId)}"
      style="padding:8px 14px;cursor:pointer;border-bottom:1px solid #F1F5F9">
      <div style="display:flex;align-items:center;gap:5px;margin-bottom:2px">
        <span style="width:7px;height:7px;border-radius:50%;background:${tColHex};flex-shrink:0"></span>
        <span style="font-size:12px;color:#0D1B2A;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${esc(el.name || el.ifcType)}</span>
        <span style="font-size:10px;color:${col};flex-shrink:0">${esc(el.ifcType.replace(/^Ifc/,''))}</span>
      </div>
      <div style="font-size:10px;color:#64748B;font-family:monospace;padding-left:12px">${gidHl}</div>
    </div>`;
  }).join("");

  searchResults.innerHTML = html;
  searchResults.style.display = "block";

  searchResults.querySelectorAll(".s-row").forEach(row => {
    row.addEventListener("mouseenter", () => row.style.background = "#F8FAFC");
    row.addEventListener("mouseleave", () => row.style.background = "");
    row.addEventListener("click", () => {
      const gid = row.dataset.gid;
      const mesh = allMeshes().find(m => m.userData.globalId === gid);
      if (!mesh) return;
      selectMesh(mesh);
      const box = new THREE.Box3().setFromObject(mesh);
      if (!box.isEmpty()) {
        orb.tgt.copy(box.getCenter(new THREE.Vector3()));
        orb.sph.radius = Math.max(box.getSize(new THREE.Vector3()).length() * 2.5, 2);
        applyOrb();
      }
      searchResults.style.display = "none";
    });
  });
}

searchInput.addEventListener("input", e => renderSearch(e.target.value));
searchInput.addEventListener("keydown", e => {
  if (e.key === "Escape") { searchInput.value = ""; renderSearch(""); }
});
searchClear.addEventListener("click", () => {
  searchInput.value = ""; renderSearch(""); searchInput.focus();
});
document.addEventListener("mousedown", e => {
  const bar = document.getElementById("search-bar");
  if (bar && !bar.contains(e.target)) searchResults.style.display = "none";
});

// ── Render-Loop ─────────────────────────────────────────────────────────────
(function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); })();

// ── Hilfsfunktion ───────────────────────────────────────────────────────────
function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ═══════════════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════════════
(async () => {
  if (!MODEL_URLS.length) {
    if (loadEl) loadEl.style.display = "none";
    if (loadStatus) loadStatus.textContent = "Kein Modell ausgewählt.";
    return;
  }
  try {
    await initWebIfc();
    let total = 0;
    for (let i = 0; i < MODEL_URLS.length; i++) {
      try {
        const v = await loadModel(MODEL_URLS[i], i + 1, MODEL_URLS.length);
        total += v;
      } catch(err) {
        console.error("Ladefehler:", MODEL_URLS[i].label, err);
        if (loadStatus) loadStatus.textContent = `⚠ ${err.message}`;
      }
    }
    if (loadBar) loadBar.style.width = "100%";
    buildCategoryUI();
    buildSearchIndex();
    fitAll();
    if (loadStatus) loadStatus.textContent = `✓ ${MODEL_URLS.length} Modell(e) · ${total.toLocaleString()} Punkte`;
  } catch(err) {
    if (loadTxt) loadTxt.textContent = "Fehler: " + err.message;
    console.error(err);
  } finally {
    if (loadEl) loadEl.style.display = "none";
  }
})();
"""
