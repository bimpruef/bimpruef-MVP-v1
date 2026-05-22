"""
list_module.py – BIMPruef Element-Listen-Modul

Eigenständiges Projektmodul – gleichrangig mit Documents, Issues, Clash.

IFC-Quelle: ausschließlich das Documents-Modul (identisches Muster wie
project_clash.py). Viewer-Upload-Sessions werden NICHT mehr direkt verwendet.

Routen:
  GET  /projects/{project_id}/list          → Haupt-UI
  POST /projects/{project_id}/list/load     → Modelle aus Documents in Cache laden
  GET  /viewer/list/data/                   → JSON-API: gefilterte Elementdaten
  GET  /viewer/list/export/                 → Excel-Download

  Legacy (Redirect):
  GET  /viewer/list/                        → leitet auf /projects/{project_id}/list
"""

import html as _html
import json
import io
import os
from typing import Any, List
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.document_storage import (
    list_project_ifc_documents,
    prepare_viewer_session_from_project_documents,
)
from app.extractors import (
    apply_filters as _apply_filters_from_extractors,
    extract_element_data,
    flatten_psets as _flatten_psets_from_extractors,
    get_candidate_products,
)
from app.project_storage import get_or_create_project_session, get_project
from app.project_ifc_cache import (
    document_index_by_slot,
    ensure_document_ifc_cache,
    get_project_ifc_index,
)
from app.storage import get_ifc_label, get_ifc_path, get_session_slots, session_exists

list_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return _html.escape(str(s or ""))


def _flatten_psets(psets: dict) -> dict:
    return _flatten_psets_from_extractors(psets)


def _apply_filters(elements: list, filters: list) -> list:
    return _apply_filters_from_extractors(elements, filters)


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


BASE_ELEMENT_KEYS = {
    "file_label",
    "slot",
    "express_id",
    "type",
    "name",
    "global_id",
    "object_type",
    "predefined_type",
}


def _parse_slots_param(session_id: str, slots: str) -> list[int]:
    all_slots = get_session_slots(session_id)
    if not slots.strip():
        return all_slots
    try:
        selected = [int(s) for s in slots.split(",") if s.strip()]
        return [s for s in selected if s in all_slots]
    except ValueError:
        return all_slots


def _parse_json_list(raw: str) -> list:
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except Exception:
        return []


def _requires_psets(filters: list, columns: list) -> bool:
    for col in columns:
        if isinstance(col, str) and col.startswith("pset:"):
            return True

    for flt in filters:
        if isinstance(flt, dict) and str(flt.get("field", "")).startswith("pset:"):
            return True

    return False


def _safe_attr(obj: Any, name: str, default: str = "") -> Any:
    try:
        value = getattr(obj, name, default)
        return default if value is None else value
    except Exception:
        return default


def _extract_element_base_data(elem: Any, file_label: str, slot: int) -> dict:
    try:
        express_id = elem.id()
    except Exception:
        express_id = ""

    try:
        ifc_type = elem.is_a()
    except Exception:
        ifc_type = ""

    return {
        "file_label": file_label,
        "slot": slot,
        "express_id": express_id,
        "type": ifc_type,
        "name": _safe_attr(elem, "Name", ""),
        "global_id": _safe_attr(elem, "GlobalId", ""),
        "object_type": _safe_attr(elem, "ObjectType", ""),
        "predefined_type": _safe_attr(elem, "PredefinedType", ""),
    }


def _load_all_elements(session_id: str, slots: List[int], include_psets: bool = False) -> list:
    """
    Lädt Elemente aus den angegebenen Slots.

    Wichtig:
    - include_psets=False lädt nur Basisdaten und vermeidet get_psets().
    - include_psets=True lädt zusätzlich alle Psets für Pset-Filter/-Spalten.
    """
    import ifcopenshell

    all_elements = []
    for slot in slots:
        path = get_ifc_path(session_id, slot)
        if not os.path.exists(path):
            continue

        label = get_ifc_label(session_id, slot)

        try:
            model = ifcopenshell.open(path)
            for elem in get_candidate_products(model):
                if include_psets:
                    data = extract_element_data(elem, file_label=label)
                    data["slot"] = slot
                else:
                    data = _extract_element_base_data(elem, file_label=label, slot=slot)
                all_elements.append(data)
        except Exception:
            continue

    return all_elements


def _collect_all_pset_keys(elements: list) -> list:
    keys = set()
    for elem in elements:
        for k in _flatten_psets(elem.get("psets", {})).keys():
            keys.add(f"pset:{k}")
    return sorted(keys)


def _collect_all_pset_values(elements: list, pset_key: str) -> list:
    key = pset_key[5:] if pset_key.startswith("pset:") else pset_key
    values = set()
    for elem in elements:
        flat = _flatten_psets(elem.get("psets", {}))
        v = flat.get(key)
        if v is not None and str(v).strip():
            values.add(str(v))
    return sorted(values)


# ─────────────────────────────────────────────────────────────────────────────
# Auth helper
# ─────────────────────────────────────────────────────────────────────────────

def _account_from_request(request: Request) -> dict:
    from app.auth import require_user
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
        return None, None, None
    session_id = get_or_create_project_session(account["account_id"], project_id)
    return account, project, session_id


# ─────────────────────────────────────────────────────────────────────────────
# Documents-Panel
# ─────────────────────────────────────────────────────────────────────────────

def _load_documents_panel(
    account: dict, project_id: str, session_id: str,
    saved: str = "", error: str = ""
) -> str:
    """Rendert die Dokumentenliste für den List-Modul-Cache."""
    docs = list_project_ifc_documents(account["account_id"], project_id)
    try:
        pidx = get_project_ifc_index(account["account_id"], project_id)
        cached_docs = pidx.get("documents", []) or []
    except Exception:
        cached_docs = []

    slot_hint = "Project IFC Index noch nicht aufgebaut."
    if cached_docs:
        slot_hint = "Project IFC Index: " + ", ".join(
            f"{i+1}: {_e(d.get('original_filename', ''))}"
            for i, d in enumerate(cached_docs)
        )

    flash = ""
    if error:
        flash = f'<div class="flash-err" style="margin-bottom:10px">⚠ {_e(error)}</div>'
    elif saved:
        flash = '<div class="flash-ok" style="margin-bottom:10px">✓ Modelle wurden aus Documents für die Elementliste geladen.</div>'

    if not docs:
        return f"""
        {flash}
        <div class="card" style="border-color:var(--accent2)">
          <h3 style="font-size:15px;margin-bottom:8px">Keine IFC-Dateien im Documents-Modul</h3>
          <p style="color:var(--muted);font-size:13px;margin-bottom:12px">
            Das List-Modul liest ausschließlich aus dem Documents-Modul.
            Lade zuerst .ifc- oder .ifczip-Dateien dort hoch.
          </p>
          <a class="btn btn-primary" href="/projects/{_e(project_id)}/documents"
            style="text-decoration:none">Zu Documents</a>
        </div>
        """

    rows = ""
    for d in docs:
        rows += f"""
        <tr>
          <td style="width:36px;text-align:center">
            <input type="checkbox" name="document_ids" value="{_e(d['document_id'])}" checked>
          </td>
          <td style="font-weight:600;color:var(--accent)">{_e(d['original_filename'])}</td>
          <td>{_e(d['file_extension'])}</td>
          <td>{_fmt_size(d.get('file_size', 0))}</td>
          <td style="color:var(--muted)">{_e(d.get('folder_path') or 'Root')}</td>
        </tr>"""

    return f"""
    {flash}
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;
        gap:16px;margin-bottom:12px">
        <div>
          <h3 style="font-size:15px;margin-bottom:4px">IFC-Quelle: Documents</h3>
          <p style="color:var(--muted);font-size:12px">{slot_hint}</p>
        </div>
        <a class="btn" href="/projects/{_e(project_id)}/documents"
          style="text-decoration:none;font-size:12px">Documents öffnen</a>
      </div>
      <form method="POST" action="/projects/{_e(project_id)}/list/load">
        <div style="overflow-x:auto;max-height:220px;overflow-y:auto">
          <table>
            <tr><th></th><th>Datei</th><th>Typ</th><th>Größe</th><th>Ordner</th></tr>
            {rows}
          </table>
        </div>
        <button class="btn btn-primary" type="submit" style="margin-top:12px">
          Ausgewählte Modelle für Elementliste laden
        </button>
      </form>
    </div>
    """


# ─────────────────────────────────────────────────────────────────────────────
# JSON-Daten-API
# ─────────────────────────────────────────────────────────────────────────────

@list_router.get("/viewer/list/meta/")
def list_meta_api(
    session_id: str = Query(...),
    slots: str = Query(default=""),
    include_psets: bool = Query(default=False),
):
    """
    Leichtgewichtiger Endpunkt.

    Standard:
    - lädt nur Basisdaten
    - extrahiert keine Psets

    Nur wenn include_psets=True:
    - werden Pset-Schlüssel gesammelt
    """
    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    selected_slots = _parse_slots_param(session_id, slots)
    elements = _load_all_elements(
        session_id,
        selected_slots,
        include_psets=include_psets,
    )

    pset_keys = _collect_all_pset_keys(elements) if include_psets else []

    return JSONResponse({
        "total": len(elements),
        "pset_keys": pset_keys,
        "psets_loaded": include_psets,
    })


@list_router.get("/viewer/list/data/")
def list_data_api(
    session_id: str = Query(...),
    slots: str = Query(default=""),
    filters_json: str = Query(default="[]"),
    columns_json: str = Query(default="[]"),
):
    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    selected_slots = _parse_slots_param(session_id, slots)
    filters = _parse_json_list(filters_json)
    columns = _parse_json_list(columns_json)

    include_psets = _requires_psets(filters, columns)

    elements = _load_all_elements(
        session_id,
        selected_slots,
        include_psets=include_psets,
    )

    filtered = _apply_filters(elements, filters)

    pset_keys = _collect_all_pset_keys(elements) if include_psets else []
    selected_pset_cols = [
        c for c in columns
        if isinstance(c, str) and c.startswith("pset:")
    ]

    rows = []
    for elem in filtered:
        row = {
            "file_label": elem.get("file_label", ""),
            "slot": elem.get("slot", ""),
            "express_id": elem.get("express_id", ""),
            "type": elem.get("type", ""),
            "name": elem.get("name", ""),
            "global_id": elem.get("global_id", ""),
            "object_type": elem.get("object_type", ""),
            "predefined_type": elem.get("predefined_type", ""),
        }

        if include_psets and selected_pset_cols:
            flat_psets = _flatten_psets(elem.get("psets", {}))
            for col_key in selected_pset_cols:
                row[col_key] = flat_psets.get(col_key[5:], "")

        rows.append(row)

    return JSONResponse({
        "total": len(elements),
        "filtered": len(filtered),
        "pset_keys": pset_keys,
        "psets_loaded": include_psets,
        "rows": rows,
    })


# ─────────────────────────────────────────────────────────────────────────────
# Excel-Export
# ─────────────────────────────────────────────────────────────────────────────

@list_router.get("/viewer/list/export/")
def list_export_excel(
    session_id: str = Query(...),
    slots: str = Query(default=""),
    filters_json: str = Query(default="[]"),
    columns_json: str = Query(default="[]"),
):
    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)

    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        return Response(
            content="openpyxl nicht installiert. Bitte 'pip install openpyxl' ausführen.",
            status_code=500,
        )

    selected_slots = _parse_slots_param(session_id, slots)
    filters = _parse_json_list(filters_json)
    columns = _parse_json_list(columns_json)

    include_psets = _requires_psets(filters, columns)

    elements = _load_all_elements(
        session_id,
        selected_slots,
        include_psets=include_psets,
    )

    filtered = _apply_filters(elements, filters)

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
        export_cols = []
        for col_key in columns:
            if col_key in base_col_keys:
                label = next((lbl for k, lbl in BASE_COLUMNS if k == col_key), col_key)
                export_cols.append((col_key, label))
            elif col_key.startswith("pset:"):
                export_cols.append((col_key, col_key[5:]))
    else:
        export_cols = BASE_COLUMNS

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BIMPruef Elementliste"

    header_fill  = PatternFill("solid", fgColor="0F2040")
    header_font  = Font(name="Calibri", bold=True, color="4FC3F7", size=11)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_side    = Side(style="thin", color="1E3A6E")
    cell_border  = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)
    alt_fill     = PatternFill("solid", fgColor="1A2A4A")
    normal_fill  = PatternFill("solid", fgColor="16213E")
    cell_font    = Font(name="Calibri", color="D0DCE8", size=10)
    cell_align   = Alignment(vertical="top", wrap_text=False)

    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(export_cols), 1))
    title_cell = ws.cell(row=1, column=1)
    title_cell.value     = f"BIMPruef – Elementliste  |  {len(filtered)} Elemente  |  Session: {session_id[:8]}…"
    title_cell.font      = Font(name="Calibri", bold=True, color="4FC3F7", size=13)
    title_cell.fill      = PatternFill("solid", fgColor="0E0E1A")
    title_cell.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 24

    for col_idx, (col_key, col_label) in enumerate(export_cols, start=1):
        cell = ws.cell(row=2, column=col_idx)
        cell.value     = col_label
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = cell_border
    ws.row_dimensions[2].height = 22

    for row_idx, elem in enumerate(filtered, start=3):
        flat_psets = _flatten_psets(elem.get("psets", {})) if include_psets else {}
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

    for col_idx, (col_key, col_label) in enumerate(export_cols, start=1):
        letter = get_column_letter(col_idx)
        max_len = len(col_label)
        for row_idx in range(3, min(3 + len(filtered), 203)):
            v = ws.cell(row=row_idx, column=col_idx).value or ""
            max_len = max(max_len, len(str(v)))
        ws.column_dimensions[letter].width = min(max(max_len + 2, 10), 50)

    ws.freeze_panes = "A3"
    ws.auto_filter.ref = (
        f"A2:{get_column_letter(len(export_cols))}2" if export_cols else "A2:A2"
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="bimpruef_elementliste.xlsx"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-Seite: Load-from-Documents  +  List-UI
# ─────────────────────────────────────────────────────────────────────────────

@list_router.get("/projects/{project_id}/list", response_class=HTMLResponse)
def project_list(project_id: str, request: Request,
                 saved: str = "",
                 error: str = ""):
    """Eigenständige List-Seite als Projektmodul.
    Erster Schritt: Modelle aus Documents laden.
    Zweiter Schritt: Elementliste anzeigen (sobald Slots vorhanden)."""
    try:
        account, project, session_id = _load_context(request, project_id)
    except Exception:
        return RedirectResponse("/auth/login", status_code=302)

    if not project:
        return RedirectResponse("/", status_code=302)

    return _render_list_page(
        account=account,
        project=project,
        session_id=session_id,
        saved=saved,
        error=error,
    )


@list_router.post("/projects/{project_id}/list/load", response_class=HTMLResponse)
def project_list_load(
    request: Request,
    project_id: str,
    document_ids: list[str] = Form(default=[]),
):
    """Lädt ausgewählte IFC-Dokumente aus Documents in den Listen-Cache."""
    try:
        account, project, session_id = _load_context(request, project_id)
    except Exception:
        return RedirectResponse("/auth/login", status_code=302)

    if not project:
        return RedirectResponse("/", status_code=302)

    try:
        for did in document_ids:
            ensure_document_ifc_cache(account["account_id"], project_id, did)
        return RedirectResponse(
            f"/projects/{_e(project_id)}/list?saved=load", status_code=303
        )
    except Exception as exc:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/list?error={quote_plus(str(exc))}",
            status_code=303,
        )




# ─────────────────────────────────────────────────────────────────────────────
# Project-IFC-Index API (neuer Weg ohne Viewer-Session)
# ─────────────────────────────────────────────────────────────────────────────

def _project_elements_from_index(account_id: str, project_id: str, slots: str, filters: list, columns: list) -> tuple[list, list, int]:
    pidx = get_project_ifc_index(account_id, project_id)
    docs = pidx.get("documents", []) or []
    if slots.strip():
        try:
            selected_slots = [int(s) for s in slots.split(",") if s.strip()]
        except ValueError:
            selected_slots = list(range(1, len(docs) + 1))
    else:
        selected_slots = list(range(1, len(docs) + 1))

    elements = []
    for slot in selected_slots:
        try:
            didx = document_index_by_slot(pidx, slot)
        except Exception:
            continue
        for elem in didx.get("elements", []) or []:
            row = dict(elem)
            row["slot"] = slot
            row["file_label"] = didx.get("original_filename", row.get("file_label", ""))
            elements.append(row)

    filtered = _apply_filters(elements, filters)
    return elements, filtered, len(elements)


@list_router.get("/projects/{project_id}/list/meta/")
def project_list_meta_api(request: Request, project_id: str, slots: str = Query(default=""), include_psets: bool = Query(default=False)):
    account = _account_from_request(request)
    elements, _filtered, total = _project_elements_from_index(account["account_id"], project_id, slots, [], [])
    # Der aktuelle Project IFC Index speichert Basisdaten. Psets sind als spätere Erweiterung vorbereitet.
    return JSONResponse({"total": total, "pset_keys": [], "psets_loaded": False})


@list_router.get("/projects/{project_id}/list/data/")
def project_list_data_api(
    request: Request,
    project_id: str,
    slots: str = Query(default=""),
    filters_json: str = Query(default="[]"),
    columns_json: str = Query(default="[]"),
):
    account = _account_from_request(request)
    filters = _parse_json_list(filters_json)
    columns = _parse_json_list(columns_json)
    elements, filtered, total = _project_elements_from_index(account["account_id"], project_id, slots, filters, columns)
    rows = []
    for elem in filtered:
        rows.append({
            "file_label": elem.get("file_label", ""),
            "slot": elem.get("slot", ""),
            "express_id": elem.get("express_id", ""),
            "type": elem.get("type", ""),
            "name": elem.get("name", ""),
            "global_id": elem.get("global_id", ""),
            "object_type": elem.get("object_type", ""),
            "predefined_type": elem.get("predefined_type", ""),
        })
    return JSONResponse({"total": total, "filtered": len(rows), "pset_keys": [], "psets_loaded": False, "rows": rows})


@list_router.get("/projects/{project_id}/list/export/")
def project_list_export_excel(
    request: Request,
    project_id: str,
    slots: str = Query(default=""),
    filters_json: str = Query(default="[]"),
    columns_json: str = Query(default="[]"),
):
    account = _account_from_request(request)
    filters = _parse_json_list(filters_json)
    columns = _parse_json_list(columns_json)
    elements, filtered, total = _project_elements_from_index(account["account_id"], project_id, slots, filters, columns)
    try:
        import openpyxl
        from openpyxl.utils import get_column_letter
    except ImportError:
        return Response(content="openpyxl nicht installiert.", status_code=500)

    base_cols = [
        ("file_label", "Datei"), ("slot", "Slot"), ("express_id", "Express-ID"),
        ("type", "IFC-Typ"), ("name", "Name"), ("global_id", "GlobalId"),
        ("object_type", "ObjectType"), ("predefined_type", "PredefinedType"),
    ]
    selected = [c for c in columns if c in {k for k, _ in base_cols}]
    export_cols = [(k, lbl) for k, lbl in base_cols if not selected or k in selected]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "BIMPruef Elementliste"
    for col_idx, (_key, label) in enumerate(export_cols, start=1):
        ws.cell(row=1, column=col_idx).value = label
    for row_idx, elem in enumerate(filtered, start=2):
        for col_idx, (key, _label) in enumerate(export_cols, start=1):
            ws.cell(row=row_idx, column=col_idx).value = elem.get(key, "")
    for col_idx, (_key, label) in enumerate(export_cols, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = max(12, min(50, len(label) + 8))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return Response(
        content=buf.read(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="bimpruef_elementliste.xlsx"'},
    )

@list_router.get("/viewer/list/", response_class=HTMLResponse)
def viewer_list(session_id: str = Query(...), project_id: str = Query(default="")):
    """Legacy-Einstiegspunkt – leitet bei bekannter project_id weiter."""
    if project_id:
        return RedirectResponse(f"/projects/{_e(project_id)}/list", status_code=302)
    if not session_exists(session_id):
        return HTMLResponse(
            '<div style="padding:40px;color:#e94560"><h2>Session nicht gefunden</h2>'
            '<a href="/">← Start</a></div>',
            status_code=404,
        )
    return _render_list_ui_body_only(session_id=session_id, project_id="",
                                     nav_html=_legacy_nav(), nav_height="47px")


# ─────────────────────────────────────────────────────────────────────────────
# Render helpers
# ─────────────────────────────────────────────────────────────────────────────

def _legacy_nav() -> str:
    return (
        '<div style="display:flex;align-items:center;gap:8px;padding:8px 16px;'
        'background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">'
        '<a href="/" style="font-size:12px;color:var(--muted);text-decoration:none">← BIMPruef</a>'
        '<span style="font-size:12px;color:var(--text)">Elementliste</span>'
        '</div>'
    )


def _render_list_page(
    account: dict,
    project: dict,
    session_id: str,
    saved: str = "",
    error: str = "",
) -> HTMLResponse:
    """Vollständige List-Seite mit Documents-Panel oben und Elementliste unten."""
    from app.projects import _page, _project_subnav, _topbar_global

    project_id = project["project_id"]
    try:
        pidx = get_project_ifc_index(account["account_id"], project_id)
        project_docs = pidx.get("documents", []) or []
    except Exception:
        project_docs = []
    slots = list(range(1, len(project_docs) + 1))
    slot_labels = {i + 1: d.get("original_filename", f"model_{i+1}.ifc") for i, d in enumerate(project_docs)}

    documents_panel = _load_documents_panel(
        account, project_id, session_id, saved=saved, error=error
    )

    if not slots:
        body = f"""
        {_topbar_global(account)}
        {_project_subnav(project_id, "list")}
        <div style="padding:28px 32px;max-width:1100px;margin:0 auto">
          <h1 style="font-size:22px;font-weight:600;margin-bottom:14px">Elementliste</h1>
          {documents_panel}
        </div>
        """
        return _page(f"{project['project_name']} – Liste", body)

    nav_html = _topbar_global(account) + _project_subnav(project_id, "list")
    nav_height = "94px"

    body = f"""
    {nav_html}
    {_render_list_ui_inner(
        session_id=session_id,
        project_id=project_id,
        slots=slots,
        documents_panel_html=documents_panel,
        nav_height=nav_height,
        slot_labels=slot_labels,
    )}
    """
    return _page(f"{project['project_name']} – Liste", body)


def _render_list_ui_body_only(session_id: str, project_id: str,
                               nav_html: str, nav_height: str) -> HTMLResponse:
    """Minimaler Render-Pfad für den Legacy (/viewer/list/) Aufruf ohne Auth."""
    from app.projects import _page

    slots = get_session_slots(session_id)
    if not slots:
        body = f"""
        {nav_html}
        <div style="padding:40px;text-align:center;color:var(--muted)">
          <p style="font-size:15px">Keine Modelle geladen.</p>
          <a href="/" class="btn btn-primary" style="margin-top:16px;display:inline-block;
            text-decoration:none">← Start</a>
        </div>"""
        return _page("Liste – BIMPruef", body)

    body = nav_html + _render_list_ui_inner(
        session_id=session_id,
        project_id=project_id,
        slots=slots,
        documents_panel_html="",
        nav_height=nav_height,
    )
    return _page("Liste – BIMPruef", body)


def _render_list_ui_inner(
    session_id: str,
    project_id: str,
    slots: list,
    documents_panel_html: str,
    nav_height: str,
    slot_labels: dict | None = None,
) -> str:
    """Gibt den inneren HTML-String der zweispaltigen List-UI zurück."""
    sid = _e(session_id)

    docs_collapsible = ""
    if documents_panel_html:
        docs_collapsible = f"""
    <details style="margin:12px 20px 0;border:1px solid var(--border);
      border-radius:8px;background:var(--surface)">
      <summary style="padding:10px 16px;cursor:pointer;font-size:13px;
        color:var(--muted);user-select:none;list-style:none;display:flex;
        align-items:center;gap:8px">
        <span>📁</span>
        <span>IFC-Quelle: Documents – Modelle wechseln oder neu laden</span>
        <span style="margin-left:auto;font-size:11px">▼</span>
      </summary>
      <div style="padding:0 16px 16px">
        {documents_panel_html}
      </div>
    </details>"""

    slot_checkboxes = ""
    slot_labels = slot_labels or {}
    for s in slots:
        label = _e(slot_labels.get(s) or get_ifc_label(session_id, s))
        slot_checkboxes += f"""
<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;
  padding:4px 8px;border:1px solid var(--border);border-radius:5px;
  background:var(--surface2)">
  <input type="checkbox" class="slot-chk" value="{s}" checked
    style="accent-color:var(--accent);width:13px;height:13px">
  <span style="color:var(--text)">{label}</span>
  <span style="color:var(--muted);font-size:10px">(Slot {s})</span>
</label>"""

    extra_height = "56px" if documents_panel_html else "0px"
    list_height = f"calc(100vh - {nav_height} - {extra_height})"

    return f"""
{docs_collapsible}

<div style="display:flex;flex-direction:column;height:{list_height};overflow:hidden;
  margin-top:{('8px' if documents_panel_html else '0')}">

  <div style="display:flex;flex:1;overflow:hidden">

    <div id="left-panel" style="width:360px;min-width:320px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <div style="padding:8px 12px;font-size:10px;font-weight:700;
        background:var(--surface2);color:var(--muted);
        text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0;
        border-bottom:1px solid var(--border)">
        <span>🔍 Such-Manager</span>
        <button id="btn-run" class="btn btn-primary"
          style="font-size:11px;padding:3px 12px">▶ Anwenden</button>
      </div>

      <div style="flex:1;overflow-y:auto;padding:10px">

        <div class="card" style="margin-bottom:10px">
          <div style="font-size:11px;font-weight:600;color:var(--muted);margin-bottom:8px;
            text-transform:uppercase;letter-spacing:.5px">📁 Dateien</div>
          <div style="display:flex;flex-direction:column;gap:5px">
            {slot_checkboxes}
          </div>
        </div>

        <div class="card" style="margin-bottom:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;
            margin-bottom:8px">
            <div style="font-size:11px;font-weight:600;color:var(--muted);
              text-transform:uppercase;letter-spacing:.5px">⚙ Filter-Regeln</div>
            <button id="btn-add-filter" class="btn"
              style="font-size:10px;padding:2px 9px;color:var(--accent)">
              + Hinzufügen
            </button>
          </div>
          <div id="filter-list" style="display:flex;flex-direction:column;gap:6px">
          </div>
          <div id="no-filters-hint" style="font-size:11px;color:var(--muted);
            font-style:italic;padding:4px 0">
            Kein Filter aktiv – alle Elemente werden angezeigt.
          </div>
        </div>

        <div class="card" style="margin-bottom:10px">
          <div style="display:flex;align-items:center;justify-content:space-between;
            margin-bottom:8px">
            <div style="font-size:11px;font-weight:600;color:var(--muted);
              text-transform:uppercase;letter-spacing:.5px">📊 Spalten</div>
            <div style="display:flex;gap:5px;flex-wrap:wrap;justify-content:flex-end">
              <button id="btn-load-psets" class="btn"
                style="font-size:10px;padding:2px 8px;color:var(--accent)">
                Pset-Spalten laden
              </button>
              <button id="btn-cols-all" class="btn"
                style="font-size:10px;padding:2px 8px">Alle</button>
              <button id="btn-cols-none" class="btn"
                style="font-size:10px;padding:2px 8px">Keine</button>
            </div>
          </div>
          <div id="column-list" style="display:flex;flex-direction:column;gap:4px">
            <div style="font-size:11px;color:var(--muted);font-style:italic">
              Lade Spaltenliste …
            </div>
          </div>
        </div>

      </div>

      <div style="padding:10px 12px;border-top:1px solid var(--border);flex-shrink:0">
        <button id="btn-export" class="btn btn-primary"
          style="width:100%;font-size:13px;padding:9px">
          ⬇ Als Excel herunterladen
        </button>
      </div>

    </div>

        <div style="flex:1;display:flex;flex-direction:column;overflow:hidden">

      <div id="status-bar" style="padding:6px 14px;background:var(--surface2);font-size:11px;
        color:var(--muted);border-bottom:1px solid var(--border);flex-shrink:0;
        display:flex;align-items:center;gap:12px">
        <span id="status-total">–</span>
        <span id="status-filtered" style="color:var(--accent)"></span>
        <span style="margin-left:auto;color:var(--muted);font-size:10px" id="status-cols"></span>
      </div>

      <div id="table-wrap" style="flex:1;overflow:auto">
        <div style="display:flex;flex-direction:column;align-items:center;
          justify-content:center;height:100%;gap:14px;padding:40px"
          id="table-placeholder">
          <svg width="40" height="40" fill="none" stroke="var(--muted)" stroke-width="1.5" viewBox="0 0 24 24">
            <rect x="3" y="3" width="18" height="18" rx="2"/><path d="M3 9h18M9 21V9"/>
          </svg>
          <div style="text-align:center">
            <div style="font-size:14px;color:var(--text);margin-bottom:6px">
              Elementliste bereit zum Laden
            </div>
            <div style="font-size:12px;color:var(--muted)">
              Optional Filter setzen, dann
              <strong style="color:var(--accent)">▶ Anwenden</strong> klicken.
            </div>
          </div>
        </div>
        <table id="result-table" style="display:none">
          <thead id="result-thead"></thead>
          <tbody id="result-tbody"></tbody>
        </table>
      </div>

    </div>

  </div>
</div>

<script>
(function() {{

const SESSION_ID  = {json.dumps(session_id)};
const PROJECT_ID  = {json.dumps(project_id)};
const API_BASE    = PROJECT_ID ? `/projects/${PROJECT_ID}/list/data/` : "/viewer/list/data/";
const META_BASE   = PROJECT_ID ? `/projects/${PROJECT_ID}/list/meta/` : "/viewer/list/meta/";
const EXPORT_BASE = PROJECT_ID ? `/projects/${PROJECT_ID}/list/export/` : "/viewer/list/export/";

let allElements   = [];
let psetKeys      = [];
let filters       = [];
let selectedCols  = [];
let colDefs       = [];
let filterCounter = 0;

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

const filterList       = document.getElementById("filter-list");
const noFiltersHint    = document.getElementById("no-filters-hint");
const columnList       = document.getElementById("column-list");
const resultTable      = document.getElementById("result-table");
const resultThead      = document.getElementById("result-thead");
const resultTbody      = document.getElementById("result-tbody");
const tablePlaceholder = document.getElementById("table-placeholder");
const statusTotal      = document.getElementById("status-total");
const statusFiltered   = document.getElementById("status-filtered");
const statusCols       = document.getElementById("status-cols");
const btnRun           = document.getElementById("btn-run");
const btnExport        = document.getElementById("btn-export");
const btnLoadPsets     = document.getElementById("btn-load-psets");

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

function addFilter(fieldKey, operator, value) {{
  const id = ++filterCounter;
  const wrapper = document.createElement("div");
  wrapper.id = `filter-${{id}}`;
  wrapper.style.cssText = "display:grid;gap:4px;padding:8px;background:var(--bg);" +
    "border:1px solid var(--border);border-radius:6px;position:relative";

  const fieldOpts = buildFieldOptions(psetKeys);
  const opOpts = OPERATORS.map(o =>
    `<option value="${{o.key}}" ${{o.key===operator?"selected":""}}>${{o.label}}</option>`
  ).join("");

  wrapper.innerHTML = `
    <div style="display:flex;align-items:center;gap:4px">
      <select class="filter-field" data-id="${{id}}"
        style="flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);
        padding:4px 6px;border-radius:5px;font-size:11px">
        ${{fieldOpts}}
      </select>
      <button class="btn-remove-filter" data-id="${{id}}"
        style="background:var(--surface2);border:1px solid var(--accent2);color:var(--accent2);
        padding:2px 8px;border-radius:4px;font-size:11px;cursor:pointer;flex-shrink:0">✕</button>
    </div>
    <select class="filter-op" data-id="${{id}}"
      style="background:var(--bg);border:1px solid var(--border);color:var(--text);
      padding:4px 6px;border-radius:5px;font-size:11px">
      ${{opOpts}}
    </select>
    <input class="filter-val" data-id="${{id}}" type="text"
      value="${{esc(value||"")}}"
      placeholder="Suchwert eingeben …"
      style="background:var(--bg);border:1px solid var(--border);color:var(--text);
      padding:4px 8px;border-radius:5px;font-size:11px;width:100%;outline:none">
  `;

  if (fieldKey) wrapper.querySelector(".filter-field").value = fieldKey;

  filterList.appendChild(wrapper);
  noFiltersHint.style.display = "none";

  wrapper.querySelector(".btn-remove-filter").addEventListener("click", () => {{
    wrapper.remove();
    if (!filterList.children.length) noFiltersHint.style.display = "";
    syncFilters();
  }});

  const fieldSel = wrapper.querySelector(".filter-field");
  const valInput = wrapper.querySelector(".filter-val");

  function updateDatalist() {{
    const fk = fieldSel.value;
    const dlId = `dl-${{id}}`;
    const old = document.getElementById(dlId);
    if (old) old.remove();
    if (fk.startsWith("pset:")) {{
      const vals = getPsetValues(fk);
      if (vals.length) {{
        const dl = document.createElement("datalist");
        dl.id = dlId;
        vals.forEach(v => {{
          const opt = document.createElement("option");
          opt.value = v;
          dl.appendChild(opt);
        }});
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
    const f = row.querySelector(".filter-field");
    const o = row.querySelector(".filter-op");
    const v = row.querySelector(".filter-val");
    if (f && o && v) {{
      filters.push({{field: f.value, operator: o.value, value: v.value}});
    }}
  }});
}}

function getPsetValues(psetKey) {{
  const vals = new Set();
  allElements.forEach(row => {{
    const v = row[psetKey];
    if (v !== undefined && v !== null && String(v).trim()) vals.add(String(v));
  }});
  return [...vals].sort();
}}

function buildColumnUI() {{
  colDefs = [...BASE_COLS];
  psetKeys.forEach(k => {{
    const lbl = k.startsWith("pset:") ? k.slice(5) : k;
    colDefs.push({{key: k, label: lbl}});
  }});

  columnList.innerHTML = "";

  const baseSection = document.createElement("div");
  baseSection.style.cssText = "margin-bottom:8px";
  baseSection.innerHTML = `<div style="font-size:10px;color:var(--muted);margin-bottom:4px;
    text-transform:uppercase;letter-spacing:.4px">Basis-Felder</div>`;
  BASE_COLS.forEach(col => baseSection.appendChild(makeColRow(col)));
  columnList.appendChild(baseSection);

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

async function loadData() {{
  syncFilters();
  syncColumns();

  const selectedSlots = [...document.querySelectorAll(".slot-chk:checked")].map(c => c.value);
  btnRun.disabled = true;
  btnRun.textContent = "⏳ …";

  const params = new URLSearchParams({{
    session_id: SESSION_ID,
    slots: selectedSlots.join(","),
    filters_json: JSON.stringify(filters),
    columns_json: JSON.stringify(selectedCols),
  }});

  try {{
    const resp = await fetch(API_BASE + "?" + params.toString());
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    allElements = data.rows;
    if (!psetKeys.length && data.pset_keys && data.pset_keys.length) {{
      psetKeys = data.pset_keys;
      buildColumnUI();
      document.querySelectorAll(".filter-field").forEach(sel => {{
        const cur = sel.value;
        sel.innerHTML = buildFieldOptions(psetKeys);
        sel.value = cur;
      }});
    }}

    statusTotal.textContent = `Gesamt: ${{data.total}} Elemente`;
    statusFiltered.textContent = data.filtered < data.total
      ? `Gefiltert: ${{data.filtered}} angezeigt` : "";
    syncColumns();
    renderTable(data.rows);
  }} catch(e) {{
    statusTotal.textContent = "Fehler: " + e.message;
    tablePlaceholder.style.display = "flex";
    tablePlaceholder.innerHTML =
      `<span style="color:var(--accent2)">⚠ ${{esc(e.message)}}</span>`;
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

  const visibleCols = activeCols.filter(c => {{
    return rows.some(row => {{
      const val = row[c.key];
      return val !== undefined && val !== null && String(val).trim() !== "";
    }});
  }});

  if (!visibleCols.length) {{
    tablePlaceholder.style.display = "flex";
    tablePlaceholder.innerHTML =
      "<span>Für die ausgewählten Spalten sind keine Werte vorhanden.</span>";
    resultTable.style.display = "none";
    return;
  }}

  let thead = "<tr>";
  thead += `<th style="min-width:40px;width:40px">#</th>`;
  visibleCols.forEach(c => {{
    thead += `<th style="min-width:80px">${{esc(c.label)}}</th>`;
  }});
  thead += "</tr>";
  resultThead.innerHTML = thead;

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
    tbody += `<tr><td colspan="${{visibleCols.length+1}}"
      style="text-align:center;color:var(--muted);font-style:italic;padding:10px">
      … ${{rows.length - MAX_PREVIEW}} weitere Zeilen (im Excel-Export enthalten)
      </td></tr>`;
  }}

  resultTbody.innerHTML = tbody;
  tablePlaceholder.style.display = "none";
  resultTable.style.display = "";
}}

function doExport() {{
  syncFilters();
  syncColumns();
  const selectedSlots = [...document.querySelectorAll(".slot-chk:checked")].map(c => c.value);
  const params = new URLSearchParams({{
    session_id: SESSION_ID,
    slots: selectedSlots.join(","),
    filters_json: JSON.stringify(filters),
    columns_json: JSON.stringify(selectedCols),
  }});
  window.location.href = EXPORT_BASE + "?" + params.toString();
}}

async function preloadMeta() {{
  const selectedSlots = [...document.querySelectorAll(".slot-chk:checked")].map(c => c.value);
  const params = new URLSearchParams({{
    session_id: SESSION_ID,
    slots: selectedSlots.join(","),
    include_psets: "false",
  }});

  try {{
    const resp = await fetch(META_BASE + "?" + params.toString());
    if (!resp.ok) return;

    const data = await resp.json();
    if (data.error) return;

    if (data.total !== undefined) {{
      statusTotal.textContent =
        data.total + " Elemente verfügbar – Filter setzen und ▶ Anwenden klicken.";
    }}
  }} catch(e) {{
  }}
}}

async function loadPsetKeys() {{
  const selectedSlots = [...document.querySelectorAll(".slot-chk:checked")].map(c => c.value);

  btnLoadPsets.disabled = true;
  btnLoadPsets.textContent = "⏳ Psets …";

  const params = new URLSearchParams({{
    session_id: SESSION_ID,
    slots: selectedSlots.join(","),
    include_psets: "true",
  }});

  try {{
    const resp = await fetch(META_BASE + "?" + params.toString());
    const data = await resp.json();

    if (data.error) throw new Error(data.error);

    psetKeys = data.pset_keys || [];
    buildColumnUI();

    document.querySelectorAll(".filter-field").forEach(sel => {{
      const cur = sel.value;
      sel.innerHTML = buildFieldOptions(psetKeys);
      sel.value = cur;
    }});

    statusTotal.textContent =
      `${{data.total}} Elemente verfügbar – ${{psetKeys.length}} Pset-Spalten geladen.`;
  }} catch(e) {{
    statusTotal.textContent = "Fehler beim Laden der Pset-Spalten: " + e.message;
  }} finally {{
    btnLoadPsets.disabled = false;
    btnLoadPsets.textContent = "Pset-Spalten laden";
  }}
}}

document.getElementById("btn-cols-all").addEventListener("click", () => {{
  document.querySelectorAll(".col-chk").forEach(c => {{ c.checked = true; }});
  syncColumns();
}});
document.getElementById("btn-cols-none").addEventListener("click", () => {{
  document.querySelectorAll(".col-chk").forEach(c => {{ c.checked = false; }});
  syncColumns();
}});

document.getElementById("btn-add-filter").addEventListener("click",
  () => addFilter("type", "contains", ""));
btnRun.addEventListener("click", loadData);
btnExport.addEventListener("click", doExport);
btnLoadPsets.addEventListener("click", loadPsetKeys);

document.addEventListener("keydown", e => {{
  if (e.key === "Enter" && e.target.classList.contains("filter-val")) loadData();
}});

function esc(s) {{
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/\"/g,"&quot;");
}}

buildColumnUI();
preloadMeta();

}})();
</script>"""
