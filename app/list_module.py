"""
list_module.py – BIMPruef Element-Listen-Modul

Eigenständiges Projektmodul – gleichrangig mit Documents, Issues, Clash, To-do, Checking.

Routen:
  GET  /projects/{project_id}/list   → Haupt-UI: Such-Manager + Spaltenauswahl + Tabellenvorschau
  GET  /viewer/list/data/            → JSON-API: gefilterte Elementdaten (technisch, bleibt unter /viewer/)
  GET  /viewer/list/export/          → Excel-Download der gefilterten + konfigurierten Liste

  Legacy (Redirect):
  GET  /viewer/list/                 → wird auf /projects/{project_id}/list weitergeleitet
                                       (nur noch für sessionbasierte Aufrufe ohne project_id)
"""

import html as _html
import json
import io
from typing import Optional, List

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.storage import get_session_slots, get_ifc_label, session_exists
from app.extractors import (
    apply_filters as _apply_filters_from_extractors,
    extract_element_data,
    flatten_psets as _flatten_psets_from_extractors,
    get_candidate_products,
)

list_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return _html.escape(str(s or ""))


def _load_all_elements(session_id: str, slots: List[int]) -> list:
    """Lädt alle Elemente aus den angegebenen Slots und gibt eine flache Liste zurück."""
    import ifcopenshell
    from app.storage import get_ifc_path
    import os

    all_elements = []
    for slot in slots:
        path = get_ifc_path(session_id, slot)
        if not os.path.exists(path):
            continue
        label = get_ifc_label(session_id, slot)
        try:
            model = ifcopenshell.open(path)
            for elem in get_candidate_products(model):
                data = extract_element_data(elem, file_label=label)
                data["slot"] = slot
                all_elements.append(data)
        except Exception:
            continue
    return all_elements


def _flatten_psets(psets: dict) -> dict:
    """Delegates to extractors.flatten_psets – single source of truth."""
    return _flatten_psets_from_extractors(psets)


def _apply_filters(elements: list, filters: list) -> list:
    """Delegates to extractors.apply_filters – single source of truth."""
    return _apply_filters_from_extractors(elements, filters)


def _collect_all_pset_keys(elements: list) -> list:
    """Gibt sortierte Liste aller vorhandenen Pset-Schlüssel zurück."""
    keys = set()
    for elem in elements:
        for k in _flatten_psets(elem.get("psets", {})).keys():
            keys.add(f"pset:{k}")
    return sorted(keys)


def _collect_all_pset_values(elements: list, pset_key: str) -> list:
    """Gibt sortierte Liste aller eindeutigen Werte für einen Pset-Schlüssel zurück."""
    key = pset_key[5:] if pset_key.startswith("pset:") else pset_key
    values = set()
    for elem in elements:
        flat = _flatten_psets(elem.get("psets", {}))
        v = flat.get(key)
        if v is not None and str(v).strip():
            values.add(str(v))
    return sorted(values)


# ─────────────────────────────────────────────────────────────────────────────
# JSON-Daten-API (für dynamisches Frontend)
# ─────────────────────────────────────────────────────────────────────────────

@list_router.get("/viewer/list/data/")
def list_data_api(
    session_id: str   = Query(...),
    slots: str        = Query(default=""),       # kommagetrennte Slot-Nummern, "" = alle
    filters_json: str = Query(default="[]"),     # JSON-Array von Filter-Dicts
    columns_json: str = Query(default="[]"),     # JSON-Array der gewünschten Spalten
):
    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    all_slots = get_session_slots(session_id)
    if slots.strip():
        try:
            selected_slots = [int(s) for s in slots.split(",") if s.strip()]
        except ValueError:
            selected_slots = all_slots
    else:
        selected_slots = all_slots

    try:
        filters = json.loads(filters_json)
    except Exception:
        filters = []
    try:
        columns = json.loads(columns_json)
    except Exception:
        columns = []

    elements = _load_all_elements(session_id, selected_slots)
    filtered = _apply_filters(elements, filters)

    # Alle verfügbaren Felder für die UI ermitteln
    pset_keys = _collect_all_pset_keys(elements)

    # Zeilendaten aufbauen
    rows = []
    for elem in filtered:
        flat_psets = _flatten_psets(elem.get("psets", {}))
        row = {
            "file_label":      elem.get("file_label", ""),
            "slot":            elem.get("slot", ""),
            "express_id":      elem.get("express_id", ""),
            "type":            elem.get("type", ""),
            "name":            elem.get("name", ""),
            "global_id":       elem.get("global_id", ""),
            "object_type":     elem.get("object_type", ""),
            "predefined_type": elem.get("predefined_type", ""),
        }
        for k in flat_psets:
            row[f"pset:{k}"] = flat_psets[k]
        rows.append(row)

    return JSONResponse({
        "total":     len(elements),
        "filtered":  len(filtered),
        "pset_keys": pset_keys,
        "rows":      rows,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Excel-Export
# ─────────────────────────────────────────────────────────────────────────────

@list_router.get("/viewer/list/export/")
def list_export_excel(
    session_id: str   = Query(...),
    slots: str        = Query(default=""),
    filters_json: str = Query(default="[]"),
    columns_json: str = Query(default="[]"),
):
    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)

    try:
        import openpyxl
        from openpyxl.styles import (
            Font, PatternFill, Alignment, Border, Side
        )
        from openpyxl.utils import get_column_letter
    except ImportError:
        return Response(
            content="openpyxl nicht installiert. Bitte 'pip install openpyxl' ausführen.",
            status_code=500,
        )

    all_slots = get_session_slots(session_id)
    if slots.strip():
        try:
            selected_slots = [int(s) for s in slots.split(",") if s.strip()]
        except ValueError:
            selected_slots = all_slots
    else:
        selected_slots = all_slots

    try:
        filters = json.loads(filters_json)
    except Exception:
        filters = []
    try:
        columns = json.loads(columns_json)
    except Exception:
        columns = []

    elements = _load_all_elements(session_id, selected_slots)
    filtered = _apply_filters(elements, filters)

    # Spalten bestimmen
    BASE_COLUMNS = [
        ("file_label",      "Datei"),
        ("slot",            "Slot"),
        ("express_id",      "Express-ID"),
        ("type",            "IFC-Typ"),
        ("name",            "Name"),
        ("global_id",       "GlobalId"),
        ("object_type",     "ObjectType"),
        ("predefined_type", "PredefinedType"),
    ]
    base_col_keys = {k for k, _ in BASE_COLUMNS}

    if columns:
        # Nur ausgewählte Spalten – Reihenfolge beibehalten
        export_cols = []
        for col_key in columns:
            if col_key in base_col_keys:
                label = next((lbl for k, lbl in BASE_COLUMNS if k == col_key), col_key)
                export_cols.append((col_key, label))
            elif col_key.startswith("pset:"):
                export_cols.append((col_key, col_key[5:]))
    else:
        export_cols = BASE_COLUMNS

    # Workbook erstellen
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BIMPruef Elementliste"

    # Stile
    header_fill   = PatternFill("solid", fgColor="0F2040")
    header_font   = Font(name="Calibri", bold=True, color="4FC3F7", size=11)
    header_align  = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side     = Side(style="thin", color="1E3A6E")
    cell_border   = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    alt_fill      = PatternFill("solid", fgColor="1A2A4A")
    normal_fill   = PatternFill("solid", fgColor="16213E")
    cell_font     = Font(name="Calibri", color="D0DCE8", size=10)
    cell_align    = Alignment(vertical="top", wrap_text=False)

    # Titelzeile
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(export_cols), 1))
    title_cell = ws.cell(row=1, column=1)
    title_cell.value  = f"BIMPruef – Elementliste  |  {len(filtered)} Elemente  |  Session: {session_id[:8]}…"
    title_cell.font   = Font(name="Calibri", bold=True, color="4FC3F7", size=13)
    title_cell.fill   = PatternFill("solid", fgColor="0E0E1A")
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 24

    # Header-Zeile (Zeile 2)
    for col_idx, (col_key, col_label) in enumerate(export_cols, start=1):
        cell = ws.cell(row=2, column=col_idx)
        cell.value     = col_label
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = cell_border
    ws.row_dimensions[2].height = 22

    # Datenspalten befüllen
    for row_idx, elem in enumerate(filtered, start=3):
        flat_psets = _flatten_psets(elem.get("psets", {}))
        fill = alt_fill if row_idx % 2 == 0 else normal_fill
        for col_idx, (col_key, _) in enumerate(export_cols, start=1):
            if col_key.startswith("pset:"):
                value = flat_psets.get(col_key[5:], "")
            else:
                value = elem.get(col_key, "")
            cell = ws.cell(row=row_idx, column=col_idx)
            cell.value     = str(value) if value is not None else ""
            cell.font      = cell_font
            cell.fill      = fill
            cell.alignment = cell_align
            cell.border    = cell_border

    # Spaltenbreiten automatisch anpassen
    for col_idx, (col_key, col_label) in enumerate(export_cols, start=1):
        letter = get_column_letter(col_idx)
        max_len = len(col_label)
        for row_idx in range(3, min(3 + len(filtered), 203)):  # max 200 Zeilen prüfen
            v = ws.cell(row=row_idx, column=col_idx).value or ""
            max_len = max(max_len, len(str(v)))
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 50)

    # Freeze panes
    ws.freeze_panes = "A3"

    # Auto-Filter
    ws.auto_filter.ref = (
        f"A2:{get_column_letter(len(export_cols))}2"
        if export_cols else "A2:A2"
    )

    # In Bytes umwandeln
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="bimpruef_elementliste.xlsx"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Eigenständige UI-Helfer – keine Abhängigkeit zu viewer.py
# ─────────────────────────────────────────────────────────────────────────────

_DARK_STYLES = """\
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
  transition:background .15s,border-color .15s}
button:hover,.btn:hover{background:#223a5e;border-color:var(--accent)}
.btn-primary{background:var(--accent);color:#0a1a2e;border-color:var(--accent);font-weight:600}
.btn-primary:hover{background:#81d4fa}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:18px;margin-bottom:16px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--border);padding:8px 10px;text-align:left;vertical-align:top;font-size:12px}
th{background:var(--surface2);color:#8ab;font-weight:600;position:sticky;top:0;z-index:1}
tr:hover td{background:rgba(79,195,247,.04)}
.bp-tab{display:inline-block;padding:8px 16px;font-size:13px;color:var(--text);
  text-decoration:none;border-bottom:2px solid transparent;transition:color .15s,border-color .15s}
.bp-tab:hover{color:var(--accent);text-decoration:none}
.bp-tab--active{color:var(--accent);border-bottom:2px solid var(--accent)}
footer{text-align:center;padding:24px 0 12px;border-top:1px solid var(--border);
  color:var(--muted);font-size:12px;margin-top:40px}
</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_e(title)}</title>
{_DARK_STYLES}
<link rel="stylesheet" href="/static/bimpruef.css">
</head>
<body>
{body}
<footer>
  <p>BIMPruef Platform &nbsp;·&nbsp;
  <a href="/impressum">Impressum</a> &nbsp;·&nbsp;
  <a href="/datenschutz">Datenschutz</a></p>
</footer>
</body>
</html>""")


def _project_subnav(project_id: str) -> str:
    """Rendert die projektweite Subnav mit aktivem List-Tab.
    Spiegelt _project_subnav() aus projects.py – List ist hier immer aktiv."""
    pid = _e(project_id)
    items = [
        ("dashboard",  f"/projects/{pid}",             "Dashboard"),
        ("model",      f"/projects/{pid}/model",        "Model"),
        ("documents",  f"/projects/{pid}/documents",    "Documents"),
        ("clash",      f"/projects/{pid}/clash",        "Clash"),
        ("list",       f"/projects/{pid}/list",         "List"),
        ("issues",     f"/projects/{pid}/issues",       "Issues"),
        ("todo",       f"/projects/{pid}/todo",         "To-do"),
        ("checking",   f"/projects/{pid}/checking",     "Checking"),
        ("settings",   f"/projects/{pid}/settings",     "Settings"),
    ]
    links = []
    for key, href, label in items:
        cls = "bp-tab bp-tab--active" if key == "list" else "bp-tab"
        links.append(f'<a href="{href}" class="{cls}">{label}</a>')
    return (
        '<div style="background:var(--surface);border-bottom:1px solid var(--border);'
        'padding:0 16px">'
        + "".join(links) +
        '</div>'
    )


def _topbar_project(project_id: str) -> str:
    """Obere Navigationsleiste mit Logo und Zurück-Link zum Projekt-Dashboard."""
    pid = _e(project_id)
    return (
        '<div style="display:flex;align-items:center;gap:8px;padding:8px 16px;'
        'background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">'
        '<a href="/" style="font-size:12px;color:var(--muted);text-decoration:none">BIMPruef</a>'
        '<span style="color:var(--muted);font-size:12px">/</span>'
        f'<a href="/projects/{pid}" style="font-size:12px;color:var(--muted);text-decoration:none">Projekt</a>'
        '<span style="color:var(--muted);font-size:12px">/</span>'
        '<span style="font-size:12px;color:var(--text)">List</span>'
        '</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-Seite des List-Moduls  (neuer Einstiegspunkt: Projektmodul)
# ─────────────────────────────────────────────────────────────────────────────

@list_router.get("/projects/{project_id}/list", response_class=HTMLResponse)
def project_list(project_id: str, request=None):
    """Eigenständige List-Seite als Projektmodul.
    Holt die Session-ID aus dem Projekt und rendert die vollständige List-UI."""
    from app.auth import require_user
    from app.project_storage import get_project, get_or_create_project_session

    # Auth – require_user wirft AuthError wenn kein Cookie; FastAPI gibt 401.
    try:
        user = require_user(request)
        account_id = user["user_id"]
    except Exception:
        from fastapi.responses import RedirectResponse as _RR
        return _RR("/auth/login", status_code=302)

    project = get_project(account_id, project_id)
    if not project:
        from fastapi.responses import RedirectResponse as _RR
        return _RR("/", status_code=302)

    session_id = get_or_create_project_session(account_id, project_id)
    return _render_list_page(session_id=session_id, project_id=project_id)


@list_router.get("/viewer/list/", response_class=HTMLResponse)
def viewer_list(session_id: str = Query(...), project_id: str = Query(default="")):
    """Legacy-Einstiegspunkt – leitet bei bekannter project_id weiter, sonst
    rendert direkt (für nicht-Projekt-Sessions aus dem Legacy-Viewer-Workflow)."""
    if project_id:
        return RedirectResponse(f"/projects/{_e(project_id)}/list", status_code=302)
    # Ohne project_id: direkte Darstellung (Legacy-Pfad bleibt funktionsfähig)
    return _render_list_page(session_id=session_id, project_id="")


def _render_list_page(session_id: str, project_id: str) -> HTMLResponse:
    """Kern-Rendering der List-UI – unabhängig vom Einstiegspunkt."""
    if not session_exists(session_id):
        return _page("Fehler",
            '<div style="padding:40px;color:var(--accent2)">'
            '<h2>Session nicht gefunden</h2><a href="/">← Start</a></div>')

    sid   = _e(session_id)
    slots = get_session_slots(session_id)

    # Bestimme Navigations-Header je nach Kontext
    if project_id:
        nav_html = _topbar_project(project_id) + _project_subnav(project_id)
        model_url = f"/projects/{_e(project_id)}/model"
        nav_height = "94px"   # topbar (~41px) + subnav (~53px)
    else:
        # Legacy-Modus ohne Projekt: minimale Leiste
        nav_html = (
            '<div style="display:flex;align-items:center;gap:8px;padding:8px 16px;'
            'background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">'
            '<a href="/" style="font-size:12px;color:var(--muted);text-decoration:none">← BIMPruef</a>'
            '<span style="font-size:12px;color:var(--text)">Elementliste</span>'
            '</div>'
        )
        model_url = f"/viewer/?session_id={sid}"
        nav_height = "47px"

    if not slots:
        body = f"""
{nav_html}
<div style="padding:40px;text-align:center;color:var(--muted)">
  <p style="font-size:15px">Keine Modelle geladen. Bitte zuerst IFC-Dateien aus Documents laden.</p>
  <a href="{model_url}" class="btn btn-primary"
    style="margin-top:16px;display:inline-block;text-decoration:none">
    ← Zum Viewer
  </a>
</div>"""
        return _page("Liste – BIMPruef", body)

    # Slot-Checkboxen für den Filter
    slot_checkboxes = ""
    for s in slots:
        label = _e(get_ifc_label(session_id, s))
        slot_checkboxes += f"""
<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;
  padding:4px 8px;border:1px solid var(--border);border-radius:5px;background:var(--surface2)">
  <input type="checkbox" class="slot-chk" value="{s}" checked
    style="accent-color:var(--accent);width:13px;height:13px">
  <span style="color:var(--text)">{label}</span>
  <span style="color:var(--muted);font-size:10px">(Slot {s})</span>
</label>"""

    body = f"""
{nav_html}

<div style="display:flex;flex-direction:column;height:calc(100vh - {nav_height});overflow:hidden">

  <!-- Zweispaltiges Layout: Suchmanager links, Tabelle rechts -->
  <div style="display:flex;flex:1;overflow:hidden">

    <!-- ══════════════════════════════════════════════════════
         LINKE SPALTE: Such-Manager + Spaltenauswahl
         ══════════════════════════════════════════════════════ -->
    <div id="left-panel" style="width:360px;min-width:320px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <!-- Header -->
      <div style="padding:8px 12px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <span>🔍 Such-Manager</span>
        <button id="btn-run" class="btn btn-primary"
          style="font-size:11px;padding:3px 12px">▶ Anwenden</button>
      </div>

      <div style="flex:1;overflow-y:auto;padding:10px">

        <!-- Dateien / Slots -->
        <div class="card" style="margin-bottom:10px">
          <div style="font-size:11px;font-weight:600;color:#8ab;margin-bottom:8px;
            text-transform:uppercase;letter-spacing:.5px">📁 Dateien</div>
          <div style="display:flex;flex-direction:column;gap:5px">
            {slot_checkboxes}
          </div>
        </div>

        <!-- Filter-Regeln -->
        <div class="card" style="margin-bottom:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;
            margin-bottom:8px">
            <div style="font-size:11px;font-weight:600;color:#8ab;
              text-transform:uppercase;letter-spacing:.5px">⚙ Filter-Regeln</div>
            <button id="btn-add-filter" class="btn"
              style="font-size:10px;padding:2px 9px;color:var(--accent)">+ Hinzufügen</button>
          </div>
          <div id="filter-list" style="display:flex;flex-direction:column;gap:6px">
            <!-- Filter werden dynamisch ergänzt -->
          </div>
          <div id="no-filters-hint" style="font-size:11px;color:var(--muted);
            font-style:italic;padding:4px 0">
            Kein Filter aktiv – alle Elemente werden angezeigt.
          </div>
        </div>

        <!-- Spaltenauswahl -->
        <div class="card" style="margin-bottom:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;
            margin-bottom:8px">
            <div style="font-size:11px;font-weight:600;color:#8ab;
              text-transform:uppercase;letter-spacing:.5px">📊 Spalten</div>
            <div style="display:flex;gap:5px">
              <button id="btn-cols-all"  class="btn" style="font-size:10px;padding:2px 8px">Alle</button>
              <button id="btn-cols-none" class="btn" style="font-size:10px;padding:2px 8px">Keine</button>
            </div>
          </div>
          <div id="column-list" style="display:flex;flex-direction:column;gap:4px">
            <!-- Spalten werden dynamisch befüllt -->
            <div style="font-size:11px;color:var(--muted);font-style:italic">
              Lade Spaltenliste …
            </div>
          </div>
        </div>

      </div><!-- /scroll -->

      <!-- Export-Button -->
      <div style="padding:10px 12px;border-top:1px solid var(--border);flex-shrink:0">
        <button id="btn-export" class="btn btn-primary"
          style="width:100%;font-size:13px;padding:9px">
          ⬇ Als Excel herunterladen
        </button>
      </div>

    </div><!-- /left-panel -->

    <!-- ══════════════════════════════════════════════════════
         RECHTE SPALTE: Ergebnis-Tabelle
         ══════════════════════════════════════════════════════ -->
    <div style="flex:1;display:flex;flex-direction:column;overflow:hidden">

      <!-- Status-Bar -->
      <div id="status-bar" style="padding:6px 14px;background:#0f2040;font-size:11px;
        color:#8ab;border-bottom:1px solid var(--border);flex-shrink:0;
        display:flex;align-items:center;gap:12px">
        <span id="status-total">–</span>
        <span id="status-filtered" style="color:var(--accent)"></span>
        <span style="margin-left:auto;color:var(--muted);font-size:10px" id="status-cols"></span>
      </div>

      <!-- Tabellen-Wrapper -->
      <div id="table-wrap" style="flex:1;overflow:auto">
        <div style="display:flex;align-items:center;justify-content:center;
          height:100%;color:var(--muted);font-size:14px;font-style:italic" id="table-placeholder">
          Filter anwenden und auf <strong style="color:var(--accent);margin:0 4px">▶ Anwenden</strong>
          klicken, um die Tabelle zu laden.
        </div>
        <table id="result-table" style="display:none">
          <thead id="result-thead"></thead>
          <tbody id="result-tbody"></tbody>
        </table>
      </div>

    </div><!-- /right -->

  </div>
</div>

<script>
(function() {{

const SESSION_ID  = {repr(session_id)};
const API_BASE    = "/viewer/list/data/";
const EXPORT_BASE = "/viewer/list/export/";

// ─── Zustand ───────────────────────────────────────────────────────────────
let allElements    = [];    // rohe Zeilen vom Server
let psetKeys       = [];    // alle pset:-Schlüssel
let filters        = [];    // aktive Filter
let selectedCols   = [];    // ausgewählte Spalten
let colDefs        = [];    // [{{key, label}}]
let filterCounter  = 0;

const BASE_COLS = [
  {{key:"file_label",      label:"Datei"}},
  {{key:"slot",            label:"Slot"}},
  {{key:"express_id",      label:"Express-ID"}},
  {{key:"type",            label:"IFC-Typ"}},
  {{key:"name",            label:"Name"}},
  {{key:"global_id",       label:"GlobalId"}},
  {{key:"object_type",     label:"ObjectType"}},
  {{key:"predefined_type", label:"PredefinedType"}},
];

// ─── DOM-Refs ───────────────────────────────────────────────────────────────
const filterList    = document.getElementById("filter-list");
const noFiltersHint = document.getElementById("no-filters-hint");
const columnList    = document.getElementById("column-list");
const resultTable   = document.getElementById("result-table");
const resultThead   = document.getElementById("result-thead");
const resultTbody   = document.getElementById("result-tbody");
const tablePlaceholder = document.getElementById("table-placeholder");
const statusTotal   = document.getElementById("status-total");
const statusFiltered= document.getElementById("status-filtered");
const statusCols    = document.getElementById("status-cols");
const btnRun        = document.getElementById("btn-run");
const btnExport     = document.getElementById("btn-export");

// ─── Feld-Definitionen für Filter ───────────────────────────────────────────
const BASE_FIELD_OPTS = [
  {{key:"file_label",      label:"Datei"}},
  {{key:"type",            label:"IFC-Typ"}},
  {{key:"name",            label:"Name"}},
  {{key:"global_id",       label:"GlobalId"}},
  {{key:"object_type",     label:"ObjectType"}},
  {{key:"predefined_type", label:"PredefinedType"}},
];
const OPERATORS = [
  {{key:"contains",     label:"enthält"}},
  {{key:"not_contains", label:"enthält nicht"}},
  {{key:"equals",       label:"ist gleich"}},
  {{key:"not_equals",   label:"ist ungleich"}},
  {{key:"starts_with",  label:"beginnt mit"}},
  {{key:"ends_with",    label:"endet mit"}},
];

function buildFieldOptions(extraPsetKeys) {{
  let opts = BASE_FIELD_OPTS.map(f =>
    `<option value="${{f.key}}">${{esc(f.label)}}</option>`
  ).join("");
  if (extraPsetKeys.length) {{
    opts += `<optgroup label="Eigenschaften (Psets)">`;
    for (const k of extraPsetKeys) {{
      const lbl = k.startsWith("pset:") ? k.slice(5) : k;
      opts += `<option value="${{esc(k)}}">${{esc(lbl)}}</option>`;
    }}
    opts += `</optgroup>`;
  }}
  return opts;
}}

// ─── Filter hinzufügen ───────────────────────────────────────────────────────
function addFilter(fieldKey, operator, value) {{
  const id = ++filterCounter;
  const wrapper = document.createElement("div");
  wrapper.id = `filter-${{id}}`;
  wrapper.style.cssText = "display:grid;gap:4px;padding:8px;background:#0e1a30;" +
    "border:1px solid var(--border);border-radius:6px;position:relative";

  const fieldOpts = buildFieldOptions(psetKeys);
  const opOpts    = OPERATORS.map(o =>
    `<option value="${{o.key}}" ${{o.key===operator?"selected":""}}>${{o.label}}</option>`
  ).join("");

  wrapper.innerHTML = `
    <div style="display:flex;align-items:center;gap:4px">
      <select class="filter-field" data-id="${{id}}"
        style="flex:1;background:#0e1a30;border:1px solid var(--border);color:var(--text);
        padding:4px 6px;border-radius:5px;font-size:11px">
        ${{fieldOpts}}
      </select>
      <button class="btn-remove-filter" data-id="${{id}}"
        style="background:#2a0a10;border:1px solid #6e1a2e;color:#ff8080;
        padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;flex-shrink:0">✕</button>
    </div>
    <select class="filter-op" data-id="${{id}}"
      style="background:#0e1a30;border:1px solid var(--border);color:var(--text);
      padding:4px 6px;border-radius:5px;font-size:11px">
      ${{opOpts}}
    </select>
    <input class="filter-val" data-id="${{id}}" type="text"
      value="${{esc(value||"")}}"
      placeholder="Suchwert eingeben …"
      style="background:#0e1a30;border:1px solid var(--border);color:var(--text);
      padding:4px 8px;border-radius:5px;font-size:11px;width:100%;outline:none">
  `;

  // Feld vorab auswählen
  if (fieldKey) wrapper.querySelector(".filter-field").value = fieldKey;

  filterList.appendChild(wrapper);
  noFiltersHint.style.display = "none";

  // Entfernen-Button
  wrapper.querySelector(".btn-remove-filter").addEventListener("click", () => {{
    wrapper.remove();
    if (!filterList.children.length) noFiltersHint.style.display = "";
    syncFilters();
  }});

  // Auto-Vorschlag bei Pset-Feld: Value-Datalist
  const fieldSel = wrapper.querySelector(".filter-field");
  const valInput = wrapper.querySelector(".filter-val");

  function updateDatalist() {{
    const fk = fieldSel.value;
    const dlId = `dl-${{id}}`;
    const old  = document.getElementById(dlId);
    if (old) old.remove();
    if (fk.startsWith("pset:")) {{
      const vals = getPsetValues(fk);
      if (vals.length) {{
        const dl = document.createElement("datalist");
        dl.id = dlId;
        vals.forEach(v => {{ const opt = document.createElement("option"); opt.value = v; dl.appendChild(opt); }});
        document.body.appendChild(dl);
        valInput.setAttribute("list", dlId);
      }}
    }} else {{
      valInput.removeAttribute("list");
    }}
  }}
  fieldSel.addEventListener("change", updateDatalist);
  updateDatalist();

  syncFilters();
}}

function syncFilters() {{
  filters = [];
  document.querySelectorAll("#filter-list > div").forEach(row => {{
    const f  = row.querySelector(".filter-field");
    const o  = row.querySelector(".filter-op");
    const v  = row.querySelector(".filter-val");
    if (f && o && v) {{
      filters.push({{field: f.value, operator: o.value, value: v.value}});
    }}
  }});
}}

// Pset-Werte aus bereits geladenen Daten
function getPsetValues(psetKey) {{
  const key = psetKey.startsWith("pset:") ? psetKey.slice(5) : psetKey;
  const vals = new Set();
  allElements.forEach(row => {{
    const v = row[psetKey];
    if (v !== undefined && v !== null && String(v).trim()) vals.add(String(v));
  }});
  return [...vals].sort();
}}

// ─── Spalten-UI aufbauen ─────────────────────────────────────────────────────
function buildColumnUI() {{
  colDefs = [...BASE_COLS];
  psetKeys.forEach(k => {{
    const lbl = k.startsWith("pset:") ? k.slice(5) : k;
    colDefs.push({{key: k, label: lbl}});
  }});

  columnList.innerHTML = "";

  // Basis-Spalten
  const baseSection = document.createElement("div");
  baseSection.style.cssText = "margin-bottom:8px";
  baseSection.innerHTML = `<div style="font-size:10px;color:var(--muted);margin-bottom:4px;
    text-transform:uppercase;letter-spacing:.4px">Basis-Felder</div>`;
  BASE_COLS.forEach(col => {{
    baseSection.appendChild(makeColRow(col));
  }});
  columnList.appendChild(baseSection);

  // Pset-Spalten
  if (psetKeys.length) {{
    const psetSection = document.createElement("div");
    psetSection.innerHTML = `<div style="font-size:10px;color:var(--muted);margin-bottom:4px;
      text-transform:uppercase;letter-spacing:.4px">Eigenschaften (Psets)</div>`;
    psetKeys.forEach(k => {{
      const lbl = k.startsWith("pset:") ? k.slice(5) : k;
      psetSection.appendChild(makeColRow({{key:k, label:lbl}}));
    }});
    columnList.appendChild(psetSection);
  }}

  syncColumns();
}}

function makeColRow(col) {{
  const div = document.createElement("label");
  div.style.cssText = "display:flex;align-items:center;gap:6px;cursor:pointer;" +
    "padding:3px 4px;border-radius:4px;font-size:11px";
  div.innerHTML = `
    <input type="checkbox" class="col-chk" value="${{esc(col.key)}}" checked
      style="accent-color:var(--accent);width:12px;height:12px;flex-shrink:0">
    <span style="color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      title="${{esc(col.label)}}">${{esc(col.label)}}</span>
  `;
  div.addEventListener("mouseenter", () => div.style.background = "rgba(79,195,247,.06)");
  div.addEventListener("mouseleave", () => div.style.background = "");
  div.querySelector(".col-chk").addEventListener("change", syncColumns);
  return div;
}}

function syncColumns() {{
  selectedCols = [...document.querySelectorAll(".col-chk:checked")].map(c => c.value);
  statusCols.textContent = selectedCols.length + " Spalten ausgewählt";
}}

// ─── Daten laden & Tabelle rendern ───────────────────────────────────────────
async function loadData() {{
  syncFilters();
  syncColumns();

  const selectedSlots = [...document.querySelectorAll(".slot-chk:checked")].map(c => c.value);
  btnRun.disabled = true;
  btnRun.textContent = "⏳ …";

  const params = new URLSearchParams({{
    session_id:   SESSION_ID,
    slots:        selectedSlots.join(","),
    filters_json: JSON.stringify(filters),
    columns_json: JSON.stringify(selectedCols),
  }});

  try {{
    const resp = await fetch(API_BASE + "?" + params.toString());
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    allElements = data.rows;
    if (!psetKeys.length && data.pset_keys.length) {{
      psetKeys = data.pset_keys;
      buildColumnUI();
      // Update filter-selects
      document.querySelectorAll(".filter-field").forEach(sel => {{
        const cur = sel.value;
        sel.innerHTML = buildFieldOptions(psetKeys);
        sel.value = cur;
      }});
    }}

    statusTotal.textContent   = `Gesamt: ${{data.total}} Elemente`;
    statusFiltered.textContent = data.filtered < data.total
      ? `Gefiltert: ${{data.filtered}} angezeigt` : "";
    syncColumns();

    renderTable(data.rows);
  }} catch(e) {{
    statusTotal.textContent = "Fehler: " + e.message;
    tablePlaceholder.style.display = "flex";
    tablePlaceholder.innerHTML = `<span style="color:var(--accent2)">⚠ ${{esc(e.message)}}</span>`;
    resultTable.style.display = "none";
  }} finally {{
    btnRun.disabled = false;
    btnRun.textContent = "▶ Anwenden";
  }}
}}

function renderTable(rows) {{
  const activeCols = colDefs.filter(c => selectedCols.includes(c.key));
  if (!activeCols.length) {{
    tablePlaceholder.style.display = "flex";
    tablePlaceholder.innerHTML = "<span>Bitte mindestens eine Spalte auswählen.</span>";
    resultTable.style.display = "none";
    return;
  }}

  // Nur Spalten anzeigen, die in den aktuellen Zeilen mindestens einen Wert haben
  // (leere/fehlende Werte in allen Zeilen => Spalte ausblenden).
  const visibleCols = activeCols.filter(c => {{
    return rows.some(row => {{
      const val = row[c.key];
      return val !== undefined && val !== null && String(val).trim() !== "";
    }});
  }});

  if (!visibleCols.length) {{
    tablePlaceholder.style.display = "flex";
    tablePlaceholder.innerHTML = "<span>Für die ausgewählten Spalten sind keine Werte vorhanden.</span>";
    resultTable.style.display = "none";
    return;
  }}

  // Header
  let thead = "<tr>";
  thead += `<th style="min-width:40px;width:40px">#</th>`;
  visibleCols.forEach(c => {{
    thead += `<th style="min-width:80px">${{esc(c.label)}}</th>`;
  }});
  thead += "</tr>";
  resultThead.innerHTML = thead;

  // Rows
  const MAX_PREVIEW = 2000;
  let tbody = "";
  const shown = rows.slice(0, MAX_PREVIEW);
  shown.forEach((row, idx) => {{
    tbody += `<tr>`;
    tbody += `<td style="color:var(--muted);text-align:right">${{idx+1}}</td>`;
    visibleCols.forEach(c => {{
      const val = row[c.key] !== undefined ? row[c.key] : "";
      tbody += `<td title="${{esc(String(val))}}">${{esc(String(val))}}</td>`;
    }});
    tbody += `</tr>`;
  }});
  if (rows.length > MAX_PREVIEW) {{
    tbody += `<tr><td colspan="${{visibleCols.length+1}}" style="text-align:center;
      color:var(--muted);font-style:italic;padding:10px">
      … ${{rows.length - MAX_PREVIEW}} weitere Zeilen (im Excel-Export enthalten)</td></tr>`;
  }}

  resultTbody.innerHTML = tbody;
  tablePlaceholder.style.display = "none";
  resultTable.style.display = "";
}}

// ─── Excel-Export ─────────────────────────────────────────────────────────────
function doExport() {{
  syncFilters();
  syncColumns();
  const selectedSlots = [...document.querySelectorAll(".slot-chk:checked")].map(c => c.value);
  const params = new URLSearchParams({{
    session_id:   SESSION_ID,
    slots:        selectedSlots.join(","),
    filters_json: JSON.stringify(filters),
    columns_json: JSON.stringify(selectedCols),
  }});
  window.location.href = EXPORT_BASE + "?" + params.toString();
}}

// ─── Alles/Keine-Knöpfe ───────────────────────────────────────────────────────
document.getElementById("btn-cols-all").addEventListener("click", () => {{
  document.querySelectorAll(".col-chk").forEach(c => {{ c.checked = true; }});
  syncColumns();
}});
document.getElementById("btn-cols-none").addEventListener("click", () => {{
  document.querySelectorAll(".col-chk").forEach(c => {{ c.checked = false; }});
  syncColumns();
}});

// ─── Events ──────────────────────────────────────────────────────────────────
document.getElementById("btn-add-filter").addEventListener("click", () => addFilter("type","contains",""));
btnRun.addEventListener("click", loadData);
btnExport.addEventListener("click", doExport);

// Enter in Filterwert-Feldern triggert Anwenden
document.addEventListener("keydown", e => {{
  if (e.key === "Enter" && e.target.classList.contains("filter-val")) loadData();
}});

// ─── Escape-Funktion ──────────────────────────────────────────────────────────
function esc(s) {{
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

// ─── Bootstrap ───────────────────────────────────────────────────────────────
// Beim ersten Laden direkt Daten holen (nur Meta – noch keine Filter)
buildColumnUI();
loadData();

}})();
</script>"""

    return _page("Liste – BIMPruef", body)
