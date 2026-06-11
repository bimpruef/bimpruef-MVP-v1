"""
project_viewer.py – BIMPruef Direct Viewer

تغییر نسبت به نسخه قبلی:
  انتهای _direct_viewer_js: بلوک VIEWER SESSION STORAGE STATE اضافه شده
  که doc_ids انتخاب‌شده را در sessionStorage مرورگر ذخیره می‌کند
  تا هنگام بازگشت از ماژول دیگر، مدل‌ها بدون نیاز به انتخاب مجدد لود شوند.

Direkter Viewer-Modul – liest IFC-Modelle direkt aus Documents/R2.
Keine Abhängigkeit zu Session/Slot-Cache.

Routen:
  GET  /projects/{project_id}/view               → Viewer-Hauptseite
  GET  /projects/{project_id}/view/file/{doc_id} → IFC-Stream direkt aus R2
  POST /projects/{project_id}/view/load          → Modellauswahl ändern
  POST /projects/{project_id}/view/pset/save     → PSets / Attribute speichern (overwrite oder save-as)
"""

from __future__ import annotations

import html
import json
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_user
from app.document_storage import (
    get_document,
    list_project_ifc_documents,
    save_project_document,
)
from app.exceptions import NotFoundError, StorageError
from app.project_storage import get_project

try:
    from app.r2_storage import download_file_from_r2, r2_enabled, upload_file_to_r2
except Exception:
    download_file_from_r2 = None
    r2_enabled = lambda: False
    upload_file_to_r2 = None

project_viewer_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
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


def _download_ifc_to_temp(doc: dict) -> str:
    """Download an IFC/IFCZIP document from R2 into a temp file; return path."""
    if not (r2_enabled() and download_file_from_r2):
        raise StorageError("Cloudflare R2 ist nicht konfiguriert.")
    suffix = doc.get("file_extension", ".ifc")
    fd, tmp_path = tempfile.mkstemp(prefix="bpview-", suffix=suffix)
    os.close(fd)
    download_file_from_r2(doc["r2_key"], tmp_path)
    return tmp_path


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


# ─────────────────────────────────────────────────────────────────────────────
# PSets / Attribute speichern  (overwrite oder save-as)
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.post("/projects/{project_id}/view/pset/save")
async def view_pset_save(request: Request, project_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]

    project = get_project(account_id, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiger JSON-Body."}, status_code=400)

    document_id  = str(body.get("document_id",  "")).strip()
    express_id   = int(body.get("express_id",   0))
    save_mode    = str(body.get("save_mode",    "overwrite"))
    new_filename = str(body.get("new_filename", "")).strip()
    changes      = body.get("changes", {})

    if not document_id:
        return JSONResponse({"error": "document_id fehlt."}, status_code=400)

    try:
        doc = get_document(account_id, project_id, document_id)
    except NotFoundError:
        return JSONResponse({"error": "Dokument nicht gefunden."}, status_code=404)

    tmp_path = ""
    ifc_tmp  = ""
    try:
        tmp_path = _download_ifc_to_temp(doc)

        ext = doc.get("file_extension", ".ifc").lower()
        if ext == ".ifczip":
            ifc_bytes = _read_ifc_bytes(tmp_path, ext)
            fd2, ifc_tmp = tempfile.mkstemp(prefix="bpedit-", suffix=".ifc")
            os.close(fd2)
            with open(ifc_tmp, "wb") as f:
                f.write(ifc_bytes)
            work_path = ifc_tmp
        else:
            work_path = tmp_path

        import ifcopenshell
        import ifcopenshell.util.element
        import ifcopenshell.guid

        try:
            import ifcopenshell.api
            _has_api = True
        except Exception:
            _has_api = False

        model = ifcopenshell.open(work_path)
        elem  = model.by_id(express_id)
        if elem is None:
            return JSONResponse(
                {"error": f"Element #{express_id} nicht im Modell gefunden."},
                status_code=404,
            )

        for attr in ("Name", "ObjectType", "Description", "Tag"):
            if attr in changes:
                val = changes[attr]
                try:
                    setattr(elem, attr, val if val != "" else None)
                except Exception:
                    pass

        for pset_name, props in (changes.get("psets") or {}).items():
            pset_name = str(pset_name or "").strip()
            if not pset_name:
                continue

            existing_pset = None
            try:
                all_psets = ifcopenshell.util.element.get_psets(elem, psets_only=True)
                if pset_name in all_psets:
                    for rel in model.by_type("IfcRelDefinesByProperties"):
                        pdef = rel.RelatingPropertyDefinition
                        if (pdef.is_a("IfcPropertySet")
                                and pdef.Name == pset_name
                                and elem in rel.RelatedObjects):
                            existing_pset = pdef
                            break
            except Exception:
                pass

            if existing_pset is None:
                if _has_api:
                    try:
                        existing_pset = ifcopenshell.api.run(
                            "pset.add_pset", model, product=elem, name=pset_name
                        )
                    except Exception:
                        existing_pset = None

                if existing_pset is None:
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

            for prop_name, prop_val in (props or {}).items():
                prop_name = str(prop_name or "").strip()
                if not prop_name:
                    continue

                found_prop = None
                current_props = list(existing_pset.HasProperties or [])
                for p in current_props:
                    try:
                        if (p.is_a("IfcPropertySingleValue")
                                and str(getattr(p, "Name", "") or "") == prop_name):
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

        model.write(work_path)

        if save_mode == "save_as":
            if not new_filename:
                base = doc["original_filename"].rsplit(".", 1)[0]
                new_filename = f"{base}_bearbeitet.ifc"
            if not new_filename.lower().endswith(".ifc"):
                new_filename = new_filename.rsplit(".", 1)[0] + ".ifc"

            with open(work_path, "rb") as f:
                file_bytes = f.read()

            new_doc = save_project_document(
                account_id,
                project_id,
                file_bytes,
                new_filename,
                content_type="application/octet-stream",
                folder_id=doc.get("folder_id") or "",
            )
            return JSONResponse({
                "ok":          True,
                "mode":        "save_as",
                "document":    new_doc,
                "document_id": new_doc["document_id"],
            })

        else:
            if not (r2_enabled() and upload_file_to_r2):
                return JSONResponse({"error": "R2 nicht konfiguriert."}, status_code=503)

            upload_file_to_r2(work_path, doc["r2_key"], "application/octet-stream")
            return JSONResponse({
                "ok":          True,
                "mode":        "overwrite",
                "document_id": document_id,
            })

    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    finally:
        for p in (tmp_path, ifc_tmp):
            if p:
                try:
                    os.remove(p)
                except OSError:
                    pass


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-Viewer-Seite
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

    valid_docs    = {d["document_id"]: d for d in all_ifc_docs}
    selected_docs = [valid_docs[did] for did in doc_ids if did in valid_docs]

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
# Viewer-Page Rendering
# ─────────────────────────────────────────────────────────────────────────────

def _color_to_light_bg(hex_color: str) -> str:
    mapping = {
        "#1E6FBF": "#EFF6FF", "#D97706": "#FFFBEB", "#059669": "#ECFDF5",
        "#DC2626": "#FEF2F2", "#7C3AED": "#F5F3FF", "#0891B2": "#ECFEFF",
        "#BE185D": "#FDF2F8", "#65A30D": "#F7FEE7",
    }
    return mapping.get(hex_color, "#F5F6F8")




def _doc_options(docs) -> str:
    """Return HTML option tags for IFC document dropdowns."""
    return "".join(
        f'<option value="{_e(d.get("document_id", ""))}">{_e(d.get("original_filename") or d.get("document_id") or "IFC")}</option>'
        for d in docs
    )


def _render_viewer_page(account, project, project_id, selected_docs, all_ifc_docs, error="") -> HTMLResponse:
    from app.projects import _page, _topbar_global

    pid = _e(project_id)
    project_name = _e(project.get("project_name", "Projekt"))

    model_entries = []
    for i, doc in enumerate(selected_docs):
        color = _slot_color(i)
        url = f"/projects/{pid}/view/file/{_e(doc['document_id'])}"
        model_entries.append({
            "url": url,
            "label": doc.get("original_filename", "IFC"),
            "color": color,
            "documentId": doc.get("document_id", ""),
        })

    model_urls_js = ",\n".join(
        '{url:' + json.dumps(m["url"]) +
        ',label:' + json.dumps(m["label"]) +
        ',color:' + json.dumps(m["color"]) +
        ',documentId:' + json.dumps(m["documentId"]) + '}'
        for m in model_entries
    )

    selected_ids = {d.get("document_id") for d in selected_docs}
    select_rows = ""
    for i, doc in enumerate(all_ifc_docs):
        doc_id = _e(doc.get("document_id", ""))
        checked = "checked" if doc.get("document_id") in selected_ids else ""
        col = _slot_color(i)
        col_light = _color_to_light_bg(col)
        ext_badge = _e(str(doc.get("file_extension", ".ifc")).upper().lstrip("."))
        name = _e(doc.get("original_filename", "IFC"))
        size = _fmt_size(doc.get("file_size", 0))
        folder = _e(doc.get("folder_path") or "Root")
        select_rows += f"""
        <label id="lbl-{doc_id}" style="display:flex;align-items:center;gap:8px;padding:8px 9px;border-radius:8px;cursor:pointer;font-size:12px;background:{'rgba(30,111,191,0.06)' if checked else 'transparent'};transition:background .12s;border:1px solid {'rgba(30,111,191,0.2)' if checked else 'transparent'}">
          <input type="checkbox" name="doc_ids" value="{doc_id}" {checked} style="width:14px;height:14px;accent-color:{col};flex-shrink:0;cursor:pointer" onchange="onDocToggle('{doc_id}',this.checked)">
          <div style="width:28px;height:28px;border-radius:7px;background:{col_light};display:flex;align-items:center;justify-content:center;flex-shrink:0;border:1px solid {col}22">
            <span style="font-size:9px;font-weight:800;color:{col}">{ext_badge}</span>
          </div>
          <div style="flex:1;min-width:0">
            <div title="{name}" style="font-weight:700;font-size:12px;color:#0D1B2A;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{name}</div>
            <div title="{folder}" style="font-size:10px;color:#8896A5;margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{size} · {folder}</div>
          </div>
          <span style="width:8px;height:8px;border-radius:50%;background:{col};flex-shrink:0;opacity:{'1' if checked else '0.3'}"></span>
        </label>"""

    empty_docs = '<div class="bp-empty">Keine Modelle im Documents-Modul.</div>'
    docs_html = select_rows if select_rows else empty_docs
    doc_options = _doc_options(all_ifc_docs) or '<option value="">Keine IFC-Dateien</option>'

    rules = [
        ("missing_names", "Missing Names"),
        ("missing_global_id", "Missing GlobalId"),
        ("missing_spaces", "Missing Spaces"),
        ("door_without_name", "Door without Name"),
        ("window_without_name", "Window without Name"),
        ("wall_without_fire_rating", "Wall without FireRating"),
        ("external_wall_check", "External Wall Check"),
    ]
    rule_rows = "".join(
        f'<label class="bp-rule-row"><input type="checkbox" name="checking_rule" value="{_e(rule_id)}" checked><span>{_e(label)}</span></label>'
        for rule_id, label in rules
    )

    error_html = f'<div class="bp-error" style="position:absolute;top:54px;left:292px;right:14px;z-index:30">{_e(error)}</div>' if error else ""

    no_models_hint = ""
    if not all_ifc_docs:
        no_models_hint = """
        <div style="position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;z-index:10;pointer-events:none">
          <div style="background:rgba(255,255,255,.95);border:1px solid #E2E8F0;border-radius:12px;padding:32px 40px;box-shadow:0 4px 20px rgba(13,27,42,.1)">
            <div style="font-size:32px;margin-bottom:12px">📂</div>
            <div style="font-size:15px;font-weight:700;color:#0D1B2A;margin-bottom:6px">Keine IFC-Modelle vorhanden</div>
            <div style="font-size:13px;color:#4A5568">Bitte laden Sie zunächst IFC-Dateien im Documents-Modul hoch.</div>
          </div>
        </div>"""

    body = f"""
{_topbar_global(account)}
<div class="bp-viewer-shell">
  {error_html}

  <div class="bp-viewer-topbar">
    <a class="bp-back" href="/projects/{pid}" title="Zurück zum Projekt">← {project_name}</a>
    <nav class="bp-tabs" role="tablist" aria-label="Viewer Navigation">
      <button type="button" class="bp-tab-btn is-active" data-viewer-tab="navigation" aria-selected="true">Navigation</button>
      <button type="button" class="bp-tab-btn" data-viewer-tab="conflicts" aria-selected="false">Conflicts</button>
      <button type="button" class="bp-tab-btn" data-viewer-tab="lists" aria-selected="false">Lists</button>
      <button type="button" class="bp-tab-btn" data-viewer-tab="issues" aria-selected="false">Issues</button>
      <button type="button" class="bp-tab-btn" data-viewer-tab="checking" aria-selected="false">Checking</button>
    </nav>
    <div class="bp-topbar-tools">
      <span id="hidden-count"></span>
      <button id="btn-show-all" type="button" class="bp-tool-btn">👁 Alle</button>
      <button id="btn-fit" type="button" class="bp-tool-btn">Fit</button>
      <button id="btn-reset" type="button" class="bp-tool-btn">Camera</button>
    </div>
  </div>

  <div class="bp-main">
    <aside id="sidebar" class="bp-sidebar">
      <div class="bp-tab-stack">
        <section id="tab-navigation" class="bp-tab-panel bp-nav-panel is-active">
          <div class="bp-section-head">
            <span>📁 IFC Dateien</span>
            <button id="btn-apply-select" type="button" class="bp-primary-btn">✓ Laden</button>
          </div>
          <div class="bp-doc-list">
            <form id="model-select-form" method="GET" action="/projects/{pid}/view">
              {docs_html}
            </form>
          </div>
          <div class="bp-doc-actions">
            <button type="button" onclick="toggleAllDocs(true)" class="bp-small-btn" style="flex:1">Alle</button>
            <button type="button" onclick="toggleAllDocs(false)" class="bp-small-btn" style="flex:1">Keine</button>
          </div>
          <div class="bp-section-head">
            <span>🏗 IFC Struktur</span>
            <span style="display:flex;gap:8px">
              <button id="btn-cat-all" type="button" class="bp-small-btn">Alle</button>
              <button id="btn-cat-none" type="button" class="bp-small-btn">Keine</button>
            </span>
          </div>
          <div id="cat-scroll" class="bp-cat-scroll">
            <div class="bp-empty" style="margin:10px">Wird geladen…</div>
          </div>
          <div id="load-status"></div>
        </section>

        <section id="tab-conflicts" class="bp-tab-panel">
          <div class="bp-panel-scroll">
            <div class="bp-section-head" style="margin:-10px -10px 10px">Conflicts</div>
            <label class="bp-field">Gruppe A<select id="clash-a">{doc_options}</select></label>
            <label class="bp-field">Gruppe B<select id="clash-b">{doc_options}</select></label>
            <label class="bp-field">Toleranz (m)<input id="clash-tolerance" type="number" step="0.01" min="0" value="0.01"></label>
            <button id="btn-clash-run" type="button" class="bp-primary-btn" style="width:100%">Clash starten</button>
            <div id="clash-results" class="bp-results"></div>
          </div>
        </section>

        <section id="tab-lists" class="bp-tab-panel">
          <div class="bp-panel-scroll">
            <div class="bp-section-head" style="margin:-10px -10px 10px">Lists</div>
            <label class="bp-field">Datei<select id="list-doc">{doc_options}</select></label>
            <label class="bp-field">IFC-Typ<input id="list-type-filter" type="text" placeholder="z.B. IfcWall"></label>
            <label class="bp-field">Name<input id="list-name-filter" type="text" placeholder="Name enthält …"></label>
            <button id="btn-list-run" type="button" class="bp-primary-btn" style="width:100%">Laden</button>
            <div id="list-results" class="bp-results"></div>
          </div>
        </section>

        <section id="tab-issues" class="bp-tab-panel">
          <div class="bp-panel-scroll">
            <div class="bp-section-head" style="margin:-10px -10px 10px">Issues</div>
            <div id="issues-results" class="bp-results"><div class="bp-empty">Tab öffnen lädt Issues automatisch.</div></div>
          </div>
        </section>

        <section id="tab-checking" class="bp-tab-panel">
          <div class="bp-panel-scroll">
            <div class="bp-section-head" style="margin:-10px -10px 10px">Checking</div>
            <label class="bp-field">Datei<select id="checking-doc">{doc_options}</select></label>
            <div style="display:flex;gap:6px;margin-bottom:8px">
              <button id="btn-rules-all" type="button" class="bp-small-btn" style="flex:1">Alle Regeln</button>
              <button id="btn-rules-none" type="button" class="bp-small-btn" style="flex:1">Keine</button>
            </div>
            {rule_rows}
            <button id="btn-checking-run" type="button" class="bp-primary-btn" style="width:100%;margin-top:4px">Prüfung starten</button>
            <div id="checking-results" class="bp-results"></div>
          </div>
        </section>
      </div>

      <section id="info-panel" class="bp-info-panel">
        <div class="bp-info-head">
          <span id="info-panel-title">Element Info</span>
          <button id="info-close" type="button" class="bp-info-close" title="Auswahl leeren">✕</button>
        </div>
        <div id="info-body" class="bp-info-body">
          <div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0;line-height:1.6">Klicken Sie auf ein Element<br>für Details.</div>
        </div>
      </section>
    </aside>

    <main id="canvas-wrap" class="bp-canvas-wrap">
      {no_models_hint}
      <canvas id="three-canvas"></canvas>

      <div id="search-bar" style="position:absolute;top:14px;left:14px;z-index:10;width:310px">
        <div style="display:flex;gap:5px">
          <div style="flex:1;position:relative">
            <svg style="position:absolute;left:10px;top:50%;transform:translateY(-50%);pointer-events:none" width="13" height="13" fill="none" stroke="#8896A5" stroke-width="2" viewBox="0 0 24 24"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
            <input id="gid-search" type="text" placeholder="GlobalId suchen…" style="width:100%;background:rgba(255,255,255,.97);border:1px solid #E2E8F0;color:#0D1B2A;padding:8px 10px 8px 30px;border-radius:9px;font-size:12px;outline:none;box-shadow:0 2px 10px rgba(13,27,42,.08);font-family:'Inter',system-ui,sans-serif" autocomplete="off">
          </div>
          <button id="search-clear" style="display:none;background:rgba(255,255,255,.97);border:1px solid #E2E8F0;color:#8896A5;border-radius:9px;padding:7px 11px;cursor:pointer;font-size:12px;box-shadow:0 2px 10px rgba(13,27,42,.08)">✕</button>
        </div>
        <div id="search-results" style="display:none;margin-top:5px;background:rgba(255,255,255,.98);border:1px solid #E2E8F0;border-radius:9px;max-height:280px;overflow-y:auto;box-shadow:0 6px 20px rgba(13,27,42,.12)"></div>
      </div>

      <div style="position:absolute;bottom:14px;right:14px;font-size:10px;color:#8896A5;background:rgba(255,255,255,.88);padding:5px 11px;border-radius:7px;border:1px solid #E2E8F0;pointer-events:none;backdrop-filter:blur(4px)">
        LMB Drehen · MMB Verschieben · Scroll Zoom · Leertaste Ausblenden
      </div>

      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;background:rgba(240,242,245,.96);z-index:20">
        <div style="width:46px;height:46px;border:3px solid #BFDBFE;border-top-color:#1E6FBF;border-radius:50%;animation:spin .7s linear infinite;margin-bottom:18px"></div>
        <p id="load-txt" style="color:#4A5568;font-size:13px;margin:0;font-family:inherit">Verbindung zu R2 wird hergestellt…</p>
        <div id="load-progress" style="margin-top:14px;width:240px;height:3px;background:#E2E8F0;border-radius:2px;overflow:hidden">
          <div id="load-bar" style="width:0%;height:100%;background:#1E6FBF;transition:width .4s ease;border-radius:2px"></div>
        </div>
        <p id="load-sub" style="color:#8896A5;font-size:11px;margin:8px 0 0;font-family:inherit"></p>
      </div>
    </main>
  </div>
</div>


<style>
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
.bp-viewer-shell{{height:calc(100vh - 52px);display:flex;flex-direction:column;overflow:hidden;background:#F0F2F5;color:#0D1B2A;font-family:'Inter',system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;position:relative}}
.bp-viewer-topbar{{height:46px;min-height:46px;background:#FFFFFF;border-bottom:1px solid #E2E8F0;display:flex;align-items:center;gap:14px;padding:0 14px;box-shadow:0 1px 4px rgba(13,27,42,.04);z-index:12}}
.bp-back{{font-weight:700;color:#0D1B2A;text-decoration:none;max-width:230px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}}
.bp-tabs{{display:flex;align-items:center;gap:4px;height:100%;flex:1;min-width:0}}
.bp-tab-btn{{border:none;background:transparent;color:#64748B;font-size:12px;font-weight:650;padding:8px 10px;border-radius:8px;cursor:pointer;font-family:inherit;white-space:nowrap}}
.bp-tab-btn:hover{{background:#F1F5F9;color:#0D1B2A}}
.bp-tab-btn.is-active{{background:#EFF6FF;color:#1E40AF}}
.bp-topbar-tools{{display:flex;align-items:center;gap:8px;flex-shrink:0}}
.bp-tool-btn{{font-size:12px;padding:7px 12px;background:#FFFFFF;border:1px solid #E2E8F0;border-radius:8px;cursor:pointer;color:#0D1B2A;box-shadow:0 1px 4px rgba(13,27,42,.06);font-family:inherit;font-weight:600}}
.bp-tool-btn:hover{{background:#F8FAFC;transform:translateY(-1px)}}
#btn-show-all{{display:none;color:#DC2626;background:#FEF2F2;border-color:rgba(220,38,38,.25)}}
#hidden-count{{font-size:11px;color:#DC2626;display:none;background:#FEF2F2;padding:5px 9px;border-radius:7px;border:1px solid rgba(220,38,38,.2)}}
.bp-main{{flex:1;min-height:0;display:flex;overflow:hidden}}
.bp-sidebar{{width:280px;min-width:280px;background:#FFFFFF;border-right:1px solid #E2E8F0;display:flex;flex-direction:column;overflow:hidden;box-shadow:2px 0 8px rgba(13,27,42,.04);z-index:5}}
.bp-tab-stack{{flex:1;min-height:0;display:flex;overflow:hidden}}
.bp-tab-panel{{display:none;flex:1;min-height:0;overflow:hidden;flex-direction:column;background:#FFFFFF}}
.bp-tab-panel.is-active{{display:flex}}
.bp-panel-scroll{{flex:1;min-height:0;overflow-y:auto;padding:10px;background:#FFFFFF}}
.bp-nav-panel{{padding:0;display:flex;flex-direction:column;min-height:0;overflow:hidden}}
.bp-section-head{{padding:9px 12px;font-size:10px;font-weight:800;background:#F8FAFC;color:#64748B;text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid #E2E8F0;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-shrink:0}}
.bp-small-btn{{font-size:10px;padding:4px 9px;background:#F1F5F9;border:1px solid #E2E8F0;border-radius:5px;cursor:pointer;color:#64748B;font-weight:700;font-family:inherit}}
.bp-primary-btn{{font-size:11px;padding:7px 10px;background:#1E6FBF;border:1px solid #1E6FBF;color:#fff;border-radius:7px;cursor:pointer;font-weight:700;font-family:inherit}}
.bp-primary-btn:hover{{background:#175A9D}}
.bp-doc-list{{padding:7px 8px;border-bottom:1px solid #E2E8F0;max-height:168px;overflow-y:auto;flex-shrink:0}}
.bp-doc-actions{{padding:7px 8px;display:flex;gap:6px;border-bottom:1px solid #E2E8F0;flex-shrink:0}}
.bp-cat-scroll{{flex:1;min-height:0;overflow-y:auto;padding:4px 0;background:#FAFBFC}}
#load-status{{padding:7px 12px;font-size:11px;color:#64748B;border-top:1px solid #E2E8F0;flex-shrink:0;background:#F8FAFC;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-height:30px;display:flex;align-items:center}}
.bp-field{{display:flex;flex-direction:column;gap:5px;margin-bottom:10px;font-size:11px;color:#64748B;font-weight:700}}
.bp-field select,.bp-field input{{width:100%;box-sizing:border-box;background:#FFFFFF;border:1px solid #CBD5E1;border-radius:7px;padding:8px 9px;font-size:12px;color:#0D1B2A;font-family:inherit;outline:none}}
.bp-field select:focus,.bp-field input:focus{{border-color:#1E6FBF;box-shadow:0 0 0 2px rgba(30,111,191,.12)}}
.bp-results{{display:flex;flex-direction:column;gap:8px;margin-top:10px;font-size:12px}}
.bp-summary{{background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;padding:9px 10px;color:#334155;font-size:12px;margin-bottom:8px}}
.bp-empty{{background:#F8FAFC;border:1px dashed #CBD5E1;border-radius:8px;padding:12px;color:#94A3B8;font-size:12px;text-align:center;line-height:1.5}}
.bp-error{{background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;padding:10px;color:#991B1B;font-size:12px;line-height:1.45}}
.bp-result-card,.bp-issue-card{{background:#FFFFFF;border:1px solid #E2E8F0;border-radius:9px;padding:9px 10px;box-shadow:0 1px 3px rgba(13,27,42,.04);font-size:11px;line-height:1.45;overflow:hidden}}
.bp-result-error{{border-left:4px solid #DC2626;background:#FEF2F2}}
.bp-result-warning{{border-left:4px solid #D97706;background:#FFFBEB}}
.bp-result-info{{border-left:4px solid #1E6FBF;background:#EFF6FF}}
.bp-card-title{{font-weight:800;color:#0D1B2A;margin-bottom:4px;font-size:12px}}
.bp-muted{{color:#64748B;font-size:10px;word-break:break-all}}
.bp-mono{{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;word-break:break-all}}
.bp-issue-meta{{display:flex;gap:5px;flex-wrap:wrap;color:#64748B;font-size:10px}}
.bp-issue-meta span{{background:#F1F5F9;border:1px solid #E2E8F0;border-radius:999px;padding:2px 6px}}
.bp-table-wrap{{max-height:360px;overflow:auto;border:1px solid #E2E8F0;border-radius:8px;background:#FFFFFF}}
.bp-table{{width:100%;border-collapse:collapse;font-size:11px;min-width:560px}}
.bp-table th{{position:sticky;top:0;background:#F8FAFC;color:#475569;text-align:left;font-size:10px;text-transform:uppercase;letter-spacing:.4px;border-bottom:1px solid #E2E8F0;padding:7px}}
.bp-table td{{border-bottom:1px solid #F1F5F9;padding:7px;color:#334155;vertical-align:top}}
.bp-rule-row{{display:flex;gap:8px;align-items:flex-start;padding:7px 8px;border:1px solid #E2E8F0;border-radius:7px;margin-bottom:6px;background:#F8FAFC;color:#334155;font-size:11px;font-weight:650;cursor:pointer}}
.bp-rule-row input{{margin-top:1px;accent-color:#1E6FBF}}
.bp-info-panel{{max-height:280px;min-height:150px;border-top:1px solid #E2E8F0;background:#FFFFFF;display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}}
.bp-info-head{{padding:9px 12px;font-size:10px;font-weight:800;background:#F8FAFC;color:#64748B;text-transform:uppercase;letter-spacing:.8px;border-bottom:1px solid #E2E8F0;display:flex;align-items:center;justify-content:space-between;gap:8px;flex-shrink:0}}
.bp-info-close{{border:none;background:transparent;color:#94A3B8;cursor:pointer;font-size:14px;line-height:1;padding:0 3px}}
.bp-info-body{{overflow-y:auto;padding:12px;font-size:12px;min-height:0;flex:1}}
.bp-canvas-wrap{{flex:1;min-width:0;position:relative;overflow:hidden;background:#F0F2F5}}
#three-canvas{{width:100%!important;height:100%!important;display:block}}
#cat-scroll::-webkit-scrollbar,.bp-panel-scroll::-webkit-scrollbar,.bp-info-body::-webkit-scrollbar,.bp-doc-list::-webkit-scrollbar,.bp-table-wrap::-webkit-scrollbar{{width:5px;height:5px}}
#cat-scroll::-webkit-scrollbar-thumb,.bp-panel-scroll::-webkit-scrollbar-thumb,.bp-info-body::-webkit-scrollbar-thumb,.bp-doc-list::-webkit-scrollbar-thumb,.bp-table-wrap::-webkit-scrollbar-thumb{{background:#CBD5E1;border-radius:3px}}
</style>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_viewer_js(model_urls_js, project_id)}
</script>
"""
    return _page(f"{project.get('project_name', 'Projekt')} – Direct Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Typ colour / category map + Viewer JavaScript
# ─────────────────────────────────────────────────────────────────────────────

def _viewer_js(model_urls_js, project_id) -> str:
    js = r"""
const PROJECT_ID = __PROJECT_ID__;
const MODEL_URLS = [__MODEL_URLS__];
const VIEWER_STATE_KEY = "bp_viewer_docs_" + PROJECT_ID;

(function syncViewerDocsFromSession(){
  const url = new URL(window.location.href);
  const hasDocs = url.searchParams.has("doc_ids");
  if (!hasDocs) {
    try {
      const saved = JSON.parse(sessionStorage.getItem(VIEWER_STATE_KEY) || "null");
      if (saved && Array.isArray(saved) && saved.length > 0) {
        saved.forEach(id => url.searchParams.append("doc_ids", id));
        window.location.replace(url.toString());
      }
    } catch (_) {}
    return;
  }
  try {
    const ids = url.searchParams.getAll("doc_ids");
    if (ids.length > 0) sessionStorage.setItem(VIEWER_STATE_KEY, JSON.stringify(ids));
    else sessionStorage.removeItem(VIEWER_STATE_KEY);
  } catch (_) {}
})();

// ════════════════════════════════════════════════════════════════════════════
// Topbar tabs + module APIs
// ════════════════════════════════════════════════════════════════════════════
let _issuesLoaded = false;

function _el(id){ return document.getElementById(id); }
function _statusHtml(text){ return `<div class="bp-empty">${esc(text)}</div>`; }
function _errorHtml(text){ return `<div class="bp-error">⚠ ${esc(text)}</div>`; }

async function _fetchJson(url, options){
  const resp = await fetch(url, options || {});
  const raw = await resp.text();
  let data = {};
  try { data = raw ? JSON.parse(raw) : {}; }
  catch (_) { data = {error: raw || `HTTP ${resp.status}`}; }
  if (!resp.ok || data.error) {
    const err = new Error(data.error || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

function switchViewerTab(tab){
  document.querySelectorAll("[data-viewer-tab]").forEach(btn => {
    const active = btn.dataset.viewerTab === tab;
    btn.classList.toggle("is-active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".bp-tab-panel").forEach(panel => {
    panel.classList.toggle("is-active", panel.id === `tab-${tab}`);
  });
  if (tab === "issues" && !_issuesLoaded) loadIssuesTab();
}

document.querySelectorAll("[data-viewer-tab]").forEach(btn => {
  btn.addEventListener("click", () => switchViewerTab(btn.dataset.viewerTab));
});

function _selectedRules(){
  return [...document.querySelectorAll("input[name=checking_rule]:checked")].map(i => i.value);
}

async function runClashTab(){
  const out = _el("clash-results");
  const a = _el("clash-a")?.value || "";
  const b = _el("clash-b")?.value || "";
  const tolerance = Number(_el("clash-tolerance")?.value || 0);
  if (!out) return;
  if (!a || !b) { out.innerHTML = _errorHtml("Bitte Gruppe A und Gruppe B auswählen."); return; }
  out.innerHTML = _statusHtml("Clash-Analyse läuft …");
  try {
    const data = await _fetchJson(`/projects/${encodeURIComponent(PROJECT_ID)}/clash/run`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        tolerance,
        group_a: {document_id: a, filters: []},
        group_b: {document_id: b, filters: []}
      })
    });
    const rows = data.clashes || [];
    let html = `<div class="bp-summary"><strong>${rows.length}</strong> Clash(es) · A: ${esc(data.count_a ?? 0)} · B: ${esc(data.count_b ?? 0)}</div>`;
    if (!rows.length) html += _statusHtml("Keine Konflikte gefunden.");
    html += rows.slice(0, 250).map((c, i) => `
      <div class="bp-result-card bp-result-error">
        <div class="bp-card-title">#${i + 1} ${esc(c.type_1 || "")} ↔ ${esc(c.type_2 || "")}</div>
        <div>${esc(c.name_1 || "Ohne Name")} <span class="bp-muted">${esc(c.global_id_1 || "")}</span></div>
        <div>${esc(c.name_2 || "Ohne Name")} <span class="bp-muted">${esc(c.global_id_2 || "")}</span></div>
      </div>`).join("");
    if (rows.length > 250) html += `<div class="bp-empty">Nur die ersten 250 Treffer werden angezeigt.</div>`;
    out.innerHTML = html;
  } catch (err) {
    out.innerHTML = _errorHtml(err.message);
  }
}

async function runListTab(){
  const out = _el("list-results");
  const docId = _el("list-doc")?.value || "";
  const typeFilter = (_el("list-type-filter")?.value || "").trim();
  const nameFilter = (_el("list-name-filter")?.value || "").trim();
  if (!out) return;
  if (!docId) { out.innerHTML = _errorHtml("Bitte eine IFC-Datei auswählen."); return; }
  const filters = [];
  if (typeFilter) filters.push({field: "type", operator: "contains", value: typeFilter});
  if (nameFilter) filters.push({field: "name", operator: "contains", value: nameFilter});
  out.innerHTML = _statusHtml("Elementliste wird geladen …");
  try {
    const data = await _fetchJson(`/projects/${encodeURIComponent(PROJECT_ID)}/list/run`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        document_ids: [docId],
        filters,
        columns: ["type", "name", "global_id"],
        include_psets: false
      })
    });
    const rows = data.rows || [];
    let html = `<div class="bp-summary"><strong>${esc(data.total ?? rows.length)}</strong> Elemente</div>`;
    if (!rows.length) html += _statusHtml("Keine Elemente gefunden.");
    else html += `<div class="bp-table-wrap"><table class="bp-table"><thead><tr><th>type</th><th>name</th><th>global_id</th></tr></thead><tbody>` +
      rows.slice(0, 500).map(r => `<tr><td>${esc(r.type || "")}</td><td>${esc(r.name || "")}</td><td class="bp-mono">${esc(r.global_id || "")}</td></tr>`).join("") +
      `</tbody></table></div>`;
    if (rows.length > 500) html += `<div class="bp-empty">Nur die ersten 500 Zeilen werden angezeigt.</div>`;
    out.innerHTML = html;
  } catch (err) {
    out.innerHTML = _errorHtml(err.message);
  }
}

async function loadIssuesTab(){
  const out = _el("issues-results");
  if (!out) return;
  _issuesLoaded = true;
  out.innerHTML = _statusHtml("Issues werden geladen …");
  try {
    const data = await _fetchJson(`/projects/${encodeURIComponent(PROJECT_ID)}/issues/data`);
    const issues = data.issues || [];
    if (!issues.length) { out.innerHTML = _statusHtml("Keine Issues vorhanden."); return; }
    out.innerHTML = issues.map(i => `
      <div class="bp-issue-card">
        <div class="bp-card-title">${esc(i.title || "Ohne Titel")}</div>
        <div class="bp-issue-meta">
          <span>${esc(i.status || "")}</span>
          <span>${esc(i.issue_type || "")}</span>
          <span>${esc(i.created_at || "")}</span>
        </div>
      </div>`).join("");
  } catch (err) {
    _issuesLoaded = false;
    out.innerHTML = _errorHtml(err.message);
  }
}

async function runCheckingTab(){
  const out = _el("checking-results");
  const docId = _el("checking-doc")?.value || "";
  const rules = _selectedRules();
  if (!out) return;
  if (!docId) { out.innerHTML = _errorHtml("Bitte eine IFC-Datei auswählen."); return; }
  if (!rules.length) { out.innerHTML = _errorHtml("Bitte mindestens eine Regel auswählen."); return; }
  out.innerHTML = _statusHtml("Prüfung läuft …");
  const bodyByDoc = {document_id: docId, rules};
  try {
    let data;
    try {
      data = await _fetchJson(`/projects/${encodeURIComponent(PROJECT_ID)}/checking/run-by-doc`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(bodyByDoc)
      });
    } catch (err) {
      if (err.status !== 404) throw err;
      data = await _fetchJson(`/projects/${encodeURIComponent(PROJECT_ID)}/checking/run`, {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({document_ids: [docId], rules})
      });
    }
    const summary = data.summary || {};
    const results = data.results || [];
    let html = `<div class="bp-summary"><strong>${esc(summary.total ?? results.length)}</strong> Treffer · Errors: ${esc(summary.errors ?? 0)} · Warnings: ${esc(summary.warnings ?? 0)} · Infos: ${esc(summary.infos ?? 0)}</div>`;
    if (!results.length) html += _statusHtml("Keine Regelverletzungen gefunden.");
    html += results.slice(0, 300).map(r => {
      const sev = String(r.severity || "info").toLowerCase();
      const cls = sev === "error" ? "bp-result-error" : (sev === "warning" ? "bp-result-warning" : "bp-result-info");
      return `<div class="bp-result-card ${cls}">
        <div class="bp-card-title">${esc(r.rule_id || "Regel")} · ${esc(sev)}</div>
        <div>${esc(r.ifc_type || "")} ${esc(r.name || "")}</div>
        <div class="bp-muted">${esc(r.message || "")}</div>
      </div>`;
    }).join("");
    if (results.length > 300) html += `<div class="bp-empty">Nur die ersten 300 Treffer werden angezeigt.</div>`;
    out.innerHTML = html;
  } catch (err) {
    out.innerHTML = _errorHtml(err.message);
  }
}

document.getElementById("btn-clash-run")?.addEventListener("click", runClashTab);
document.getElementById("btn-list-run")?.addEventListener("click", runListTab);
document.getElementById("btn-checking-run")?.addEventListener("click", runCheckingTab);
document.getElementById("btn-rules-all")?.addEventListener("click", () => document.querySelectorAll("input[name=checking_rule]").forEach(i => i.checked = true));
document.getElementById("btn-rules-none")?.addEventListener("click", () => document.querySelectorAll("input[name=checking_rule]").forEach(i => i.checked = false));


// ════════════════════════════════════════════════════════════════════════════
// IFC-Typ → Farbe & Symbol
// ════════════════════════════════════════════════════════════════════════════
const IFC_TYPE_STYLES = {
  IfcWall:               { color: 0xa07840, lightColor: '#FEF3C7', icon: '▭', group: 'Tragwerk' },
  IfcWallStandardCase:   { color: 0xa07840, lightColor: '#FEF3C7', icon: '▭', group: 'Tragwerk' },
  IfcColumn:             { color: 0x2a5c8a, lightColor: '#DBEAFE', icon: '⬛', group: 'Tragwerk' },
  IfcColumnStandardCase: { color: 0x2a5c8a, lightColor: '#DBEAFE', icon: '⬛', group: 'Tragwerk' },
  IfcBeam:               { color: 0x3a78b8, lightColor: '#EFF6FF', icon: '━', group: 'Tragwerk' },
  IfcBeamStandardCase:   { color: 0x3a78b8, lightColor: '#EFF6FF', icon: '━', group: 'Tragwerk' },
  IfcSlab:               { color: 0x5a8ea8, lightColor: '#E0F2FE', icon: '▬', group: 'Tragwerk' },
  IfcSlabStandardCase:   { color: 0x5a8ea8, lightColor: '#E0F2FE', icon: '▬', group: 'Tragwerk' },
  IfcFooting:            { color: 0x1a4060, lightColor: '#0C4A6E22', icon: '⬜', group: 'Tragwerk' },
  IfcMember:             { color: 0x3878a0, lightColor: '#E0F2FE', icon: '╱', group: 'Tragwerk' },
  IfcPlate:              { color: 0x6090a8, lightColor: '#E0F2FE', icon: '▱', group: 'Tragwerk' },
  IfcRoof:               { color: 0x6b3db8, lightColor: '#EDE9FE', icon: '⌂', group: 'Hülle' },
  IfcCurtainWall:        { color: 0xc09030, lightColor: '#FEF9C3', icon: '⧉', group: 'Hülle' },
  IfcCovering:           { color: 0x608858, lightColor: '#DCFCE7', icon: '≡', group: 'Hülle' },
  IfcRailing:            { color: 0x405860, lightColor: '#F0F9FF', icon: '⁞', group: 'Hülle' },
  IfcDoor:               { color: 0xb85030, lightColor: '#FEE2E2', icon: '🚪', group: 'Öffnungen' },
  IfcWindow:             { color: 0x40b8d0, lightColor: '#CFFAFE', icon: '⬡', group: 'Öffnungen' },
  IfcOpeningElement:     { color: 0xdddddd, lightColor: '#F8FAFC', icon: '○', group: 'Öffnungen' },
  IfcStair:              { color: 0x9a5030, lightColor: '#FEF2E2', icon: '𝌊', group: 'Erschließung' },
  IfcStairFlight:        { color: 0x9a5030, lightColor: '#FEF2E2', icon: '𝌊', group: 'Erschließung' },
  IfcRamp:               { color: 0xb88820, lightColor: '#FEFCE8', icon: '⟋', group: 'Erschließung' },
  IfcFurnishingElement:  { color: 0xb87050, lightColor: '#FEF2E2', icon: '⊡', group: 'Ausbau' },
  IfcFurniture:          { color: 0xb87050, lightColor: '#FEF2E2', icon: '⊡', group: 'Ausbau' },
  IfcSpace:              { color: 0x88c888, lightColor: '#F0FDF4', icon: '□', group: 'Ausbau' },
  IfcPipeSegment:        { color: 0x108870, lightColor: '#ECFDF5', icon: '⊃', group: 'TGA' },
  IfcDuctSegment:        { color: 0x506870, lightColor: '#F0F9FF', icon: '▷', group: 'TGA' },
  IfcCableSegment:       { color: 0xe09810, lightColor: '#FEFCE8', icon: '⌇', group: 'TGA' },
  IfcPump:               { color: 0x3040a0, lightColor: '#EFF6FF', icon: '⊕', group: 'TGA' },
  IfcFan:                { color: 0x4858a8, lightColor: '#EFF6FF', icon: '✺', group: 'TGA' },
  IfcValve:              { color: 0x50b898, lightColor: '#ECFDF5', icon: '⊗', group: 'TGA' },
  IfcSensor:             { color: 0xe080a8, lightColor: '#FDF2F8', icon: '◉', group: 'TGA' },
  IfcFlowTerminal:       { color: 0x40a880, lightColor: '#ECFDF5', icon: '⊸', group: 'TGA' },
  IfcFlowSegment:        { color: 0x508898, lightColor: '#E0F7FF', icon: '⊶', group: 'TGA' },
  IfcBuildingElementProxy: { color: 0x707080, lightColor: '#F8FAFC', icon: '?', group: 'Sonstiges' },
  IfcAnnotation:           { color: 0x888888, lightColor: '#F8FAFC', icon: '✎', group: 'Sonstiges' },
  IfcGrid:                 { color: 0xaaaaaa, lightColor: '#F8FAFC', icon: '⊞', group: 'Sonstiges' },
};
const FLAT_TYPES = new Set(["IfcOpeningElement","IfcAnnotation","IfcGrid","IfcSpace"]);
const IFC_ENTITY_COLOR_PALETTE = [
  "#1E6FBF","#B85030","#40B8D0","#A07840","#5A8EA8","#2A5C8A",
  "#3A78B8","#6B3DB8","#108870","#E09810","#B87050","#707080",
];

function normalizeHexColor(v) {
  const s = String(v||"").trim();
  if (/^#[0-9a-fA-F]{6}$/.test(s)) return s.toUpperCase();
  if (/^[0-9a-fA-F]{6}$/.test(s))  return ("#"+s).toUpperCase();
  return "";
}
function intToHexColor(v) {
  if (v==null||Number.isNaN(Number(v))) return "";
  return "#"+(Number(v)&0xffffff).toString(16).padStart(6,"0").toUpperCase();
}
function hexToIntColor(hex) {
  const h=normalizeHexColor(hex)||"#607080"; return parseInt(h.slice(1),16);
}
function colorWithAlpha(hex,alpha="18") { return (normalizeHexColor(hex)||"#607080")+alpha; }
function hashColorForEntity(entity) {
  let hash=0; for(const ch of String(entity||"IfcElement")) hash=((hash<<5)-hash+ch.charCodeAt(0))|0;
  return IFC_ENTITY_COLOR_PALETTE[Math.abs(hash)%IFC_ENTITY_COLOR_PALETTE.length];
}
function getBaseTypeStyle(t) {
  if (IFC_TYPE_STYLES[t]) return IFC_TYPE_STYLES[t];
  if (/^IfcWall/.test(t))    return {color:0xa07840,lightColor:'#FEF3C7',icon:'▭',group:'Tragwerk'};
  if (/^IfcSlab/.test(t))    return {color:0x5a8ea8,lightColor:'#E0F2FE',icon:'▬',group:'Tragwerk'};
  if (/^IfcColumn/.test(t))  return {color:0x2a5c8a,lightColor:'#DBEAFE',icon:'⬛',group:'Tragwerk'};
  if (/^IfcBeam/.test(t))    return {color:0x3a78b8,lightColor:'#EFF6FF',icon:'━',group:'Tragwerk'};
  if (/^IfcDoor/.test(t))    return {color:0xb85030,lightColor:'#FEE2E2',icon:'🚪',group:'Öffnungen'};
  if (/^IfcWindow/.test(t))  return {color:0x40b8d0,lightColor:'#CFFAFE',icon:'⬡',group:'Öffnungen'};
  if (/^IfcStair/.test(t))   return {color:0x9a5030,lightColor:'#FEF2E2',icon:'𝌊',group:'Erschließung'};
  if (/^IfcRoof/.test(t))    return {color:0x6b3db8,lightColor:'#EDE9FE',icon:'⌂',group:'Hülle'};
  if (/^IfcPipe/.test(t))    return {color:0x108870,lightColor:'#ECFDF5',icon:'⊃',group:'TGA'};
  if (/^IfcDuct/.test(t))    return {color:0x506870,lightColor:'#F0F9FF',icon:'▷',group:'TGA'};
  if (/^IfcFlow/.test(t))    return {color:0x40a880,lightColor:'#ECFDF5',icon:'⊸',group:'TGA'};
  return {color:hexToIntColor(hashColorForEntity(t)),lightColor:'#F8FAFC',icon:'◆',group:'Sonstiges'};
}
function getTypeStyle(t) {
  const base=getBaseTypeStyle(t);
  const colorHex=intToHexColor(base.color)||hashColorForEntity(t);
  return {...base,color:hexToIntColor(colorHex),colorHex,lightColor:base.lightColor||colorWithAlpha(colorHex,"18")};
}
function getColor(t) { return new THREE.Color(getTypeStyle(t).color); }

// ════════════════════════════════════════════════════════════════════════════
// Three.js setup
// ════════════════════════════════════════════════════════════════════════════
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
scene.add(new THREE.AmbientLight(0xffffff, 0.72));
const dirL1 = new THREE.DirectionalLight(0xffffff, 0.75);
dirL1.position.set(60, 100, 60); scene.add(dirL1);
const dirL2 = new THREE.DirectionalLight(0xffffff, 0.28);
dirL2.position.set(-40, 40, -40); scene.add(dirL2);
scene.add(new THREE.HemisphereLight(0xeef4ff, 0xd4c8a0, 0.38));
const gridHelper = new THREE.GridHelper(200, 40, 0xcccccc, 0xe0e0e0);
scene.add(gridHelper);

function onResize() {
  const w=wrap.clientWidth, h=wrap.clientHeight;
  renderer.setSize(w,h,false);
  camera.aspect=w/h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", onResize); onResize();

const orb = { sph: new THREE.Spherical(80, Math.PI/4, Math.PI/4), tgt: new THREE.Vector3(), drag:false, pan:false, lx:0, ly:0 };
function applyOrb() {
  camera.position.copy(new THREE.Vector3().setFromSpherical(orb.sph).add(orb.tgt));
  camera.lookAt(orb.tgt);
}
applyOrb();
canvas.addEventListener("mousedown", e => { if(e.button===0) orb.drag=true; if(e.button===1){orb.pan=true;e.preventDefault();} orb.lx=e.clientX;orb.ly=e.clientY; });
window.addEventListener("mouseup", () => { orb.drag=false; orb.pan=false; });
window.addEventListener("mousemove", e => {
  const dx=e.clientX-orb.lx, dy=e.clientY-orb.ly; orb.lx=e.clientX; orb.ly=e.clientY;
  if(orb.drag){ orb.sph.theta-=dx*.005; orb.sph.phi=Math.max(.04,Math.min(Math.PI-.04,orb.sph.phi+dy*.005)); applyOrb(); }
  if(orb.pan){ const r=new THREE.Vector3().crossVectors(camera.getWorldDirection(new THREE.Vector3()),camera.up).normalize(); const sc=orb.sph.radius*.001; orb.tgt.addScaledVector(r,-dx*sc); orb.tgt.addScaledVector(camera.up.clone().normalize(),dy*sc); applyOrb(); }
});
canvas.addEventListener("wheel", e => { orb.sph.radius=Math.max(.5,Math.min(5000,orb.sph.radius*(1+e.deltaY*.001))); applyOrb(); e.preventDefault(); }, {passive:false});

// ════════════════════════════════════════════════════════════════════════════
// State
// ════════════════════════════════════════════════════════════════════════════
const modelMeshes={}, modelGroups={}, structureTree={}, modelVisible={}, entityVisible={}, typeVisible={}, hiddenIds=new Set(), docMeta={};

function modelKey(d){ return String(d??""); }
function entityKey(d,e){ return modelKey(d)+"::"+e; }
function typeKey(d,e,t){ return entityKey(d,e)+"::"+t; }
function meshInstanceKey(m){ return (m?.userData?.docId||"")+"::"+(m?.userData?.expressId??""); }
function allMeshes(){ return Object.values(modelMeshes).flat(); }
function displayEntityName(e){ return String(e||"Unknown").replace(/^Ifc/,"")||"Unknown"; }
function displayTypeName(t){ return String(t||"Ohne Typ").replace(/^Ifc/,"")||"Ohne Typ"; }
function getModelNode(d){ const k=modelKey(d); return structureTree[k]||{label:docMeta[k]?.label||`Modell ${k}`,color:docMeta[k]?.color||"#64748B",entities:{}}; }
function modelEntities(d){ return getModelNode(d).entities||{}; }
function modelLabel(d){ return getModelNode(d).label||`Modell ${modelKey(d)}`; }
function modelColor(d){ return normalizeHexColor(getModelNode(d).color)||"#64748B"; }
function entityElementCount(d,e){ return Object.values(modelEntities(d)[e]||{}).reduce((s,l)=>s+l.length,0); }
function modelElementCount(d){ return Object.keys(modelEntities(d)).reduce((s,e)=>s+entityElementCount(d,e),0); }
function entityTypeNames(d,e){ return Object.keys(modelEntities(d)[e]||{}); }
function entityHasAnyTypeOn(d,e){ return entityTypeNames(d,e).some(t=>typeVisible[typeKey(d,e,t)]!==false); }
function entityIsOn(d,e){ return entityVisible[entityKey(d,e)]!==false&&entityHasAnyTypeOn(d,e); }
function modelHasAnyVisibleChild(d){ return Object.keys(modelEntities(d)).some(e=>entityIsOn(d,e)); }
function isStructureTypeVisible(d,e,t){ return modelVisible[modelKey(d)]!==false&&entityVisible[entityKey(d,e)]!==false&&typeVisible[typeKey(d,e,t)]!==false; }

function applyVisibility(){
  for(const m of allMeshes()){
    const catOn=isStructureTypeVisible(m.userData.docId,m.userData.ifcType,m.userData.typeName||"Ohne Typ");
    m.visible=catOn&&!hiddenIds.has(meshInstanceKey(m));
  }
  updateHiddenCount();
}
function updateHiddenCount(){
  const n=hiddenIds.size;
  const el=document.getElementById("hidden-count"), btn=document.getElementById("btn-show-all");
  if(n>0){ el.textContent=`${n} ausgeblendet`; el.style.display="inline"; if(btn)btn.style.display="inline"; }
  else    { el.style.display="none"; if(btn)btn.style.display="none"; }
}

// ════════════════════════════════════════════════════════════════════════════
// Category Tree UI
// ════════════════════════════════════════════════════════════════════════════
function buildCategoryUI(){
  const list=document.getElementById("cat-scroll"); if(!list)return; list.innerHTML="";
  const modelIds=Object.keys(structureTree).filter(d=>Object.keys(modelEntities(d)).length).sort((a,b)=>modelLabel(a).localeCompare(modelLabel(b),"de",{sensitivity:"base"}));
  if(!modelIds.length){ list.innerHTML='<div style="padding:14px;color:#94A3B8;font-size:12px">Keine IFC-Elemente geladen.</div>'; return; }
  for(const docId of modelIds){
    const entitiesMap=modelEntities(docId);
    const entities=Object.keys(entitiesMap).sort((a,b)=>displayEntityName(a).localeCompare(displayEntityName(b),"de",{sensitivity:"base"}));
    if(!entities.length) continue;
    if(modelVisible[modelKey(docId)]===undefined) modelVisible[modelKey(docId)]=true;
    for(const entity of entities){
      const typeNames=entityTypeNames(docId,entity), isFlat=FLAT_TYPES.has(entity), eKey=entityKey(docId,entity);
      if(entityVisible[eKey]===undefined) entityVisible[eKey]=!isFlat;
      for(const t of typeNames){ const k=typeKey(docId,entity,t); if(typeVisible[k]===undefined) typeVisible[k]=!isFlat; }
    }
    const mColor=modelColor(docId), mCount=modelElementCount(docId);
    const mOn=modelVisible[modelKey(docId)]!==false&&modelHasAnyVisibleChild(docId);
    const mAllOn=entities.every(e=>{ const tn=entityTypeNames(docId,e); return entityVisible[entityKey(docId,e)]!==false&&tn.every(t=>typeVisible[typeKey(docId,e,t)]!==false); });
    const modelBlock=document.createElement("div"); modelBlock.style.cssText="border-bottom:1px solid #CBD5E1;background:#FFFFFF;flex-shrink:0";
    const mh=document.createElement("div");
    mh.style.cssText=`display:flex;align-items:center;gap:6px;padding:5px 8px 5px 9px;cursor:pointer;user-select:none;border-left:3px solid ${mColor};background:linear-gradient(90deg,${colorWithAlpha(mColor,"18")},#F8FAFC 70%);opacity:${mOn?"1":".45"};line-height:1.15`;
    mh.innerHTML=`<span class="mtog" style="font-size:9px;color:${mColor};width:9px;flex-shrink:0;transition:transform .15s">▼</span><input class="model-cb" type="checkbox" ${mOn?"checked":""} data-doc-id="${esc(docId)}" style="width:12px;height:12px;accent-color:${mColor};flex-shrink:0;cursor:pointer;margin:0"><span style="width:9px;height:9px;border-radius:50%;background:${mColor};flex-shrink:0"></span><span style="font-size:11px;font-weight:750;color:#0D1B2A;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(modelLabel(docId))}">${esc(modelLabel(docId))}</span><span style="font-size:9px;color:${mColor};font-weight:700;background:${colorWithAlpha(mColor,"16")};padding:1px 5px;border-radius:8px;flex-shrink:0">${mCount}</span>`;
    modelBlock.appendChild(mh);
    const mcb=mh.querySelector(".model-cb"); if(mcb) mcb.indeterminate=mOn&&!mAllOn;
    const mb=document.createElement("div"); mb.style.cssText="padding:0;background:#FFFFFF";
    for(const entity of entities){
      const tg=entitiesMap[entity]||{}, tn=Object.keys(tg).sort((a,b)=>displayTypeName(a).localeCompare(displayTypeName(b),"de",{sensitivity:"base"}));
      if(!tn.length) continue;
      const style=getTypeStyle(entity), colorHex=style.colorHex||(intToHexColor(style.color)), count=entityElementCount(docId,entity), eKey=entityKey(docId,entity);
      const allTypesOn=tn.every(t=>typeVisible[typeKey(docId,entity,t)]!==false), anyTypesOn=tn.some(t=>typeVisible[typeKey(docId,entity,t)]!==false);
      const entityOn=modelVisible[modelKey(docId)]!==false&&entityVisible[eKey]!==false&&anyTypesOn;
      const category=document.createElement("div"); category.style.cssText="border-bottom:1px solid #EEF2F7;background:#FFFFFF;flex-shrink:0";
      const ch=document.createElement("div");
      ch.style.cssText=`display:flex;align-items:center;gap:5px;padding:3px 8px 3px 21px;cursor:pointer;user-select:none;border-left:3px solid ${colorHex};background:linear-gradient(90deg,${colorWithAlpha(colorHex,"10")},#FFFFFF 65%);opacity:${entityOn?"1":".45"};line-height:1.1;min-height:22px;box-sizing:border-box`;
      ch.innerHTML=`<span class="etog" style="font-size:8px;color:${colorHex};width:8px;flex-shrink:0;transition:transform .15s">▼</span><input class="entity-cb" type="checkbox" ${entityOn?"checked":""} data-doc-id="${esc(docId)}" data-entity="${esc(entity)}" style="width:11px;height:11px;accent-color:${colorHex};flex-shrink:0;cursor:pointer;margin:0"><span style="width:8px;height:8px;border-radius:50%;background:${colorHex};flex-shrink:0"></span><span style="font-size:11px;font-weight:700;color:#0D1B2A;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(entity)}">${esc(displayEntityName(entity))}</span><span style="font-size:9px;color:${colorHex};font-weight:700;background:${colorWithAlpha(colorHex,"16")};padding:0 5px;border-radius:8px;flex-shrink:0">${count}</span>`;
      category.appendChild(ch);
      const ecb=ch.querySelector(".entity-cb"); if(ecb) ecb.indeterminate=entityOn&&!allTypesOn;
      const cb=document.createElement("div"); cb.style.cssText="padding:1px 0 3px;background:#FFFFFF";
      for(const typeName of tn){
        const elements=tg[typeName]||[], k=typeKey(docId,entity,typeName), vis=modelVisible[modelKey(docId)]!==false&&entityVisible[eKey]!==false&&typeVisible[k]!==false, isFlat=FLAT_TYPES.has(entity);
        const typeRow=document.createElement("div");
        typeRow.style.cssText=`display:flex;align-items:center;gap:5px;padding:3px 8px 3px 40px;cursor:pointer;border-bottom:1px solid #F1F5F9;user-select:none;opacity:${vis?"1":".45"};line-height:1.15;min-height:20px;box-sizing:border-box`;
        typeRow.dataset.docId=docId; typeRow.dataset.entity=entity; typeRow.dataset.typeName=typeName;
        typeRow.innerHTML=`<span class="ttog" style="font-size:8px;color:#CBD5E1;width:8px;flex-shrink:0">▶</span><input class="type-cb" type="checkbox" ${vis?"checked":""} data-doc-id="${esc(docId)}" data-entity="${esc(entity)}" data-type-name="${esc(typeName)}" style="width:11px;height:11px;accent-color:${colorHex};flex-shrink:0;cursor:pointer;margin:0"><span style="width:8px;height:8px;border-radius:${isFlat?"0":"2px"};background:${isFlat?"transparent":colorHex};border:${isFlat?"1px dashed #CBD5E1":"1.5px solid "+colorWithAlpha(colorHex,"33")};flex-shrink:0;display:inline-block;box-sizing:border-box"></span><span style="font-size:10px;color:#1E293B;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600" title="${esc(typeName)}">${esc(displayTypeName(typeName))}</span><span style="font-size:9px;color:#94A3B8;flex-shrink:0;background:#F1F5F9;padding:0 5px;border-radius:8px">${elements.length}</span>`;
        cb.appendChild(typeRow);
        const el2=document.createElement("div"); el2.style.cssText="display:none;padding:0;background:#FAFBFD;border-bottom:1px solid #E2E8F0";
        for(const el of elements.slice(0,150)){
          const eRow=document.createElement("div");
          eRow.style.cssText=`padding:3px 8px 3px 60px;font-size:10px;color:#475569;cursor:pointer;border-bottom:1px solid #F1F5F9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;display:flex;align-items:center;gap:5px;line-height:1.15`;
          eRow.title=(el.name||displayEntityName(entity))+(el.globalId?" · "+el.globalId:"");
          eRow.dataset.expressId=el.expressId; eRow.dataset.docId=el.docId;
          eRow.innerHTML=`<span style="width:6px;height:6px;border-radius:50%;background:${colorHex};flex-shrink:0"></span><span style="overflow:hidden;text-overflow:ellipsis;flex:1">${esc(el.name||"(Kein Name)")}</span>`;
          eRow.addEventListener("mouseenter",()=>eRow.style.background="#EFF6FF");
          eRow.addEventListener("mouseleave",()=>eRow.style.background="");
          eRow.addEventListener("click",e=>{
            e.stopPropagation();
            const target=allMeshes().find(m=>m.userData.expressId===el.expressId&&String(m.userData.docId)===String(el.docId));
            if(target){ if(!target.visible){modelVisible[modelKey(docId)]=true;entityVisible[entityKey(docId,entity)]=true;typeVisible[typeKey(docId,entity,typeName)]=true;hiddenIds.delete(meshInstanceKey(target));applyVisibility();} selectMesh(target); const box=new THREE.Box3().setFromObject(target); if(!box.isEmpty()){orb.tgt.copy(box.getCenter(new THREE.Vector3()));orb.sph.radius=Math.max(box.getSize(new THREE.Vector3()).length()*2.8,2);applyOrb();} }
          });
          el2.appendChild(eRow);
        }
        if(elements.length>150){ const mr=document.createElement("div"); mr.style.cssText="padding:4px 60px;font-size:10px;color:#94A3B8;font-style:italic"; mr.textContent=`… ${elements.length-150} weitere`; el2.appendChild(mr); }
        cb.appendChild(el2);
        typeRow.addEventListener("click",e=>{
          if(e.target.closest("input")) return;
          const tog=typeRow.querySelector(".ttog"), collapsed=el2.style.display==="none";
          el2.style.display=collapsed?"block":"none"; tog.style.color=collapsed?"#64748B":"#CBD5E1"; tog.textContent=collapsed?"▼":"▶"; typeRow.style.background=collapsed?"#F0F7FF":"";
        });
        typeRow.addEventListener("mouseenter",()=>{if(el2.style.display==="none")typeRow.style.background="#F8FAFC";});
        typeRow.addEventListener("mouseleave",()=>{if(el2.style.display==="none")typeRow.style.background="";});
      }
      category.appendChild(cb); mb.appendChild(category);
      ch.addEventListener("click",e=>{if(e.target.closest("input,label"))return; const tog=ch.querySelector(".etog"),collapsed=cb.style.display==="none"; cb.style.display=collapsed?"":"none"; tog.style.transform=collapsed?"":"rotate(-90deg)";});
    }
    modelBlock.appendChild(mb); list.appendChild(modelBlock);
    mh.addEventListener("click",e=>{if(e.target.closest("input,label"))return; const tog=mh.querySelector(".mtog"),collapsed=mb.style.display==="none"; mb.style.display=collapsed?"":"none"; tog.style.transform=collapsed?"":"rotate(-90deg)";});
  }
  list.onchange=e=>{
    const target=e.target;
    if(target.classList.contains("model-cb")){
      const docId=target.dataset.docId, checked=target.checked;
      modelVisible[modelKey(docId)]=checked;
      for(const entity of Object.keys(modelEntities(docId))){ entityVisible[entityKey(docId,entity)]=checked; for(const t of entityTypeNames(docId,entity)) typeVisible[typeKey(docId,entity,t)]=checked; }
      buildCategoryUI(); applyVisibility(); return;
    }
    if(target.classList.contains("entity-cb")){
      const docId=target.dataset.docId, entity=target.dataset.entity, checked=target.checked;
      if(checked) modelVisible[modelKey(docId)]=true;
      entityVisible[entityKey(docId,entity)]=checked;
      for(const t of entityTypeNames(docId,entity)) typeVisible[typeKey(docId,entity,t)]=checked;
      if(!checked) modelVisible[modelKey(docId)]=modelHasAnyVisibleChild(docId);
      buildCategoryUI(); applyVisibility(); return;
    }
    if(target.classList.contains("type-cb")){
      const docId=target.dataset.docId, entity=target.dataset.entity, typeName=target.dataset.typeName||"Ohne Typ";
      typeVisible[typeKey(docId,entity,typeName)]=target.checked;
      entityVisible[entityKey(docId,entity)]=entityHasAnyTypeOn(docId,entity);
      modelVisible[modelKey(docId)]=target.checked?true:modelHasAnyVisibleChild(docId);
      buildCategoryUI(); applyVisibility();
    }
  };
}
function setCatAll(v){
  for(const docId of Object.keys(structureTree)){ modelVisible[modelKey(docId)]=v; for(const entity of Object.keys(modelEntities(docId))){ entityVisible[entityKey(docId,entity)]=v; for(const t of entityTypeNames(docId,entity)) typeVisible[typeKey(docId,entity,t)]=v; } }
  buildCategoryUI(); applyVisibility();
}
document.getElementById("btn-cat-all")?.addEventListener("click",()=>setCatAll(true));
document.getElementById("btn-cat-none")?.addEventListener("click",()=>setCatAll(false));

// ════════════════════════════════════════════════════════════════════════════
// web-ifc + model loader
// ════════════════════════════════════════════════════════════════════════════
let webIfc=null;
async function initWebIfc(){
  const mod=await import("https://esm.sh/web-ifc@0.0.57");
  webIfc=new mod.IfcAPI(); webIfc.SetWasmPath("https://esm.sh/web-ifc@0.0.57/"); await webIfc.Init();
}
async function loadModel(cfg,index,total){
  if(loadTxt) loadTxt.textContent=`Lade ${cfg.label}…`;
  if(loadSub) loadSub.textContent=`Modell ${index} von ${total}`;
  if(loadBar)  loadBar.style.width=`${((index-1)/total)*80}%`;
  const resp=await fetch(cfg.url); if(!resp.ok) throw new Error(`HTTP ${resp.status} für ${cfg.label}`);
  const data=new Uint8Array(await resp.arrayBuffer());
  if(loadBar) loadBar.style.width=`${((index-1)/total)*80+60/total}%`;
  const modelId=webIfc.OpenModel(data,{COORDINATE_TO_ORIGIN:false,USE_FAST_BOOLS:false});
  const elemIndex={};
  const allLines=webIfc.GetAllLines(modelId);
  for(let i=0;i<allLines.size();i++){ const id=allLines.get(i); try{const l=webIfc.GetLine(modelId,id,false);if(l)elemIndex[id]=l;}catch(_){} }
  const _tnc={};
  function resolveTypeCode(code){ if(_tnc[code]!==undefined)return _tnc[code]; let name="Unknown"; try{const raw=webIfc.GetNameFromTypeCode(code); if(raw){const low=raw.toLowerCase().replace(/^ifc_/,""); const parts=low.split("_"); name="Ifc"+parts.map(w=>w.charAt(0).toUpperCase()+w.slice(1)).join("");}}catch(_){} _tnc[code]=name; return name; }
  function typeName(line){ if(!line)return"Unknown"; if(typeof line.type==="number")return resolveTypeCode(line.type); if(typeof line.type==="string"&&line.type.startsWith("Ifc"))return line.type; const cn=line.constructor?.name??""; if(cn.startsWith("Ifc"))return cn; return"Unknown"; }
  function sv(v){ if(v==null)return""; if(typeof v==="object"&&v.value!==undefined)return String(v.value); return String(v); }
  const relMap={}, typeRelMap={};
  for(const[id,line]of Object.entries(elemIndex)){
    const rt=typeName(line).toLowerCase();
    if(rt.includes("reldefinesbyprop")){ const pref=line.RelatingPropertyDefinition;if(!pref)continue; const pid=pref.value??pref; const rels=line.RelatedObjects;if(!rels)continue; const ids=Array.isArray(rels)?rels:[rels]; for(const r of ids){const rid=r?.value??r;if(!relMap[rid])relMap[rid]=[];relMap[rid].push(pid);} }
    if(rt.includes("reldefinesbytype")){ const tref=line.RelatingType;if(!tref)continue; const tid=tref.value??tref; const rels=line.RelatedObjects;if(!rels)continue; const ids=Array.isArray(rels)?rels:[rels]; for(const r of ids){const rid=r?.value??r;typeRelMap[rid]=tid;} }
  }
  function resolveElementTypeName(eid,line){ const tl=elemIndex[typeRelMap[eid]]; const tnr=sv(tl?.Name)||sv(tl?.ElementType)||sv(tl?.ObjectType); const ot=sv(line?.ObjectType)||sv(line?.ElementType); const pre=sv(line?.PredefinedType); const up=pre&&!/^([0-9]+|NOTDEFINED|UNDEFINED)$/i.test(pre)?pre:""; return tnr||ot||up||"Ohne Typ"; }
  function getPsets(eid){ const res={}; for(const pid of relMap[eid]??[]){ const pset=elemIndex[pid];if(!pset)continue; const pn=sv(pset.Name)||typeName(pset); const props={}; const hp=pset.HasProperties; if(hp){ const list=Array.isArray(hp)?hp:[hp]; for(const ref of list){ const id=ref?.value??ref; const prop=elemIndex[id];if(!prop)continue; props[sv(prop.Name)||String(id)]=prop.NominalValue!=null?sv(prop.NominalValue):"–"; }} res[pn]=props; } return res; }
  const docId=cfg.documentId, group=new THREE.Group(); group.name=cfg.label; scene.add(group); modelGroups[docId]=group; modelMeshes[docId]=[]; const docKey=modelKey(docId); docMeta[docKey]={label:cfg.label,color:cfg.color};
  if(!structureTree[docKey]) structureTree[docKey]={label:cfg.label,color:cfg.color,entities:{}};
  structureTree[docKey].label=cfg.label; structureTree[docKey].color=cfg.color; if(!structureTree[docKey].entities) structureTree[docKey].entities={};
  if(modelVisible[docKey]===undefined) modelVisible[docKey]=true;
  const docEntities=structureTree[docKey].entities;
  const fms=webIfc.LoadAllGeometry(modelId); let vertCount=0; const seen=new Set();
  for(let i=0;i<fms.size();i++){
    const fm=fms.get(i), expId=fm.expressID, line=elemIndex[expId], tName=typeName(line), elementTypeName=resolveElementTypeName(expId,line);
    const typeStyle=getTypeStyle(tName), isFlat=FLAT_TYPES.has(tName), tCol=new THREE.Color(typeStyle.color);
    if(!seen.has(expId)){ seen.add(expId); if(!docEntities[tName])docEntities[tName]={}; if(!docEntities[tName][elementTypeName])docEntities[tName][elementTypeName]=[]; const eKey=entityKey(docKey,tName); if(entityVisible[eKey]===undefined)entityVisible[eKey]=!isFlat; const k=typeKey(docKey,tName,elementTypeName); if(typeVisible[k]===undefined)typeVisible[k]=!isFlat; docEntities[tName][elementTypeName].push({name:sv(line?.Name)||sv(line?.GlobalId)||String(expId),expressId:expId,globalId:sv(line?.GlobalId),ifcEntity:tName,typeName:elementTypeName,docId,modelLabel:cfg.label,slotColor:cfg.color}); }
    const meta={expressId:expId,ifcType:tName,typeName:elementTypeName,name:sv(line?.Name),globalId:sv(line?.GlobalId),objectType:sv(line?.ObjectType),description:sv(line?.Description),tag:sv(line?.Tag),docId,documentId:cfg.documentId,modelLabel:cfg.label,slotColor:cfg.color,psets:getPsets(expId),isFlat,typeStyle};
    const mat=new THREE.MeshLambertMaterial({color:tCol.clone(),transparent:true,opacity:isFlat?.28:.88,wireframe:isFlat,side:THREE.DoubleSide});
    const pgs=fm.geometries;
    for(let j=0;j<pgs.size();j++){
      const pg=pgs.get(j), gd=webIfc.GetGeometry(modelId,pg.geometryExpressID), vs=webIfc.GetVertexArray(gd.GetVertexData(),gd.GetVertexDataSize()), idx=webIfc.GetIndexArray(gd.GetIndexData(),gd.GetIndexDataSize());
      if(!vs||vs.length===0){gd.delete();continue;}
      const S=6, pa=new Float32Array(vs.length/S*3), na=new Float32Array(vs.length/S*3);
      for(let k=0;k<vs.length/S;k++){pa[k*3]=vs[k*S];pa[k*3+1]=vs[k*S+1];pa[k*3+2]=vs[k*S+2];na[k*3]=vs[k*S+3];na[k*3+1]=vs[k*S+4];na[k*3+2]=vs[k*S+5];}
      const geo=new THREE.BufferGeometry(); geo.setAttribute("position",new THREE.BufferAttribute(pa,3)); geo.setAttribute("normal",new THREE.BufferAttribute(na,3)); geo.setIndex(new THREE.BufferAttribute(idx,1));
      const mesh=new THREE.Mesh(geo,mat.clone()); mesh.applyMatrix4(new THREE.Matrix4().fromArray(pg.flatTransformation)); mesh.userData={...meta,_baseColor:tCol.clone(),_origColor:tCol.clone()}; mesh.visible=isStructureTypeVisible(docId,tName,elementTypeName); group.add(mesh); modelMeshes[docId].push(mesh); vertCount+=pa.length/3; gd.delete();
    }
  }
  webIfc.CloseModel(modelId); if(loadStatus)loadStatus.textContent=`✓ ${cfg.label}: ${vertCount.toLocaleString()} Punkte`; return vertCount;
}

function fitAll(){
  const box=new THREE.Box3(); scene.traverse(o=>{if(o.isMesh&&o.visible)box.expandByObject(o);}); if(box.isEmpty())scene.traverse(o=>{if(o.isMesh)box.expandByObject(o);}); if(box.isEmpty())return;
  const center=box.getCenter(new THREE.Vector3()), size=box.getSize(new THREE.Vector3()); orb.tgt.copy(center); orb.sph.radius=Math.max(size.x,size.y,size.z)*1.9; applyOrb(); gridHelper.position.y=box.min.y;
}

// ════════════════════════════════════════════════════════════════════════════
// INFO PANEL – read-only + EDIT MODE with PSets editor
// ════════════════════════════════════════════════════════════════════════════

let selectedMesh   = null;
let mouseMoved     = false;
let _editMode      = false;
let _editDocumentId = null;
let _editExpressId  = null;
let _editElemData   = null;

const HIGHLIGHT = new THREE.Color(0xff6600);

function _inpS(){
  return `background:#F8FAFC;border:1px solid #CBD5E1;color:#0D1B2A;
    padding:5px 8px;border-radius:5px;font-size:11px;width:100%;
    box-sizing:border-box;font-family:inherit;outline:none;
    transition:border-color 0.15s;`;
}

function _renderReadOnly(d){
  const ts  = d.typeStyle || getTypeStyle(d.ifcType);
  const tHex  = "#"+new THREE.Color(ts.color).getHexString();
  const tL  = ts.lightColor||'#F8FAFC';
  let h = `
<div style="font-size:11px;font-weight:700;color:${d.slotColor};margin-bottom:12px;
  padding-bottom:10px;border-bottom:1px solid #E2E8F0;
  display:flex;align-items:center;gap:6px">
  <span style="width:8px;height:8px;border-radius:50%;background:${d.slotColor};flex-shrink:0"></span>
  ${esc(d.modelLabel)}
</div>
<div style="background:${tL};border:1px solid ${tHex}33;border-radius:8px;
  padding:8px 10px;margin-bottom:10px;display:flex;align-items:center;gap:8px">
  <span style="font-size:16px">${ts.icon||'◆'}</span>
  <div>
    <div style="font-size:12px;font-weight:700;color:${tHex}">${esc(d.ifcType.replace(/^Ifc/,''))}</div>
    <div style="font-size:10px;color:#64748B">${ts.group||'Sonstiges'}</div>
  </div>
</div>
<div style="display:flex;flex-direction:column;gap:4px;margin-bottom:10px">`;
  const fields=[["GlobalId",d.globalId],["Name",d.name],["Express-ID",String(d.expressId)],["Typname",d.typeName],["ObjectType",d.objectType],["Description",d.description],["Tag",d.tag]];
  for(const[label,value]of fields){
    if(!value) continue;
    h+=`<div style="display:flex;gap:6px;align-items:flex-start">
  <span style="color:#94A3B8;min-width:80px;flex-shrink:0;font-size:10px;padding-top:1px;font-weight:500">${esc(label)}</span>
  <span style="color:#0D1B2A;font-size:11px;word-break:break-all;font-family:${label==="GlobalId"?"monospace":"inherit"}">${esc(value)}</span>
</div>`;
  }
  h+=`</div>`;
  const psets=d.psets||{};
  if(Object.keys(psets).length){
    h+=`<div style="border-top:1px solid #E2E8F0;padding-top:10px;margin-top:4px;font-size:10px;font-weight:700;color:#64748B;text-transform:uppercase;letter-spacing:.5px;margin-bottom:8px">Eigenschaften</div>`;
    for(const[pn,props]of Object.entries(psets)){
      const filt=Object.entries(props).filter(([k])=>k!=="id");
      if(!filt.length) continue;
      h+=`<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:7px;margin-bottom:6px;overflow:hidden">
  <div style="padding:5px 10px;background:#F1F5F9;font-size:10px;font-weight:700;color:#475569;border-bottom:1px solid #E2E8F0">${esc(pn)}</div>`;
      for(const[k,v]of filt){
        h+=`<div style="display:flex;gap:6px;padding:4px 10px;border-bottom:1px solid #F1F5F9">
  <span style="color:#94A3B8;font-size:10px;min-width:100px;flex-shrink:0">${esc(k)}</span>
  <span style="color:#374151;font-size:10px;word-break:break-all">${esc(String(v))}</span>
</div>`;
      }
      h+=`</div>`;
    }
  }
  h+=`<div style="margin-top:14px;border-top:1px solid #E2E8F0;padding-top:12px">
  <button onclick="enterEditMode()"
    style="width:100%;padding:8px;background:#EFF6FF;border:1px solid #BFDBFE;color:#1E40AF;
    border-radius:7px;cursor:pointer;font-size:12px;font-weight:600;font-family:inherit">
    ✎ Eigenschaften bearbeiten
  </button>
</div>`;
  infoBody.innerHTML=h;
}

function _renderEditMode(d){
  let h=`
<div style="font-size:11px;font-weight:700;color:${d.slotColor};margin-bottom:10px;
  display:flex;align-items:center;gap:6px">
  <span style="width:8px;height:8px;border-radius:50%;background:${d.slotColor};flex-shrink:0"></span>
  ${esc(d.modelLabel)} <span style="margin-left:auto;font-size:10px;background:#FEF3C7;color:#92400E;padding:2px 6px;border-radius:4px;font-weight:600">Bearbeitungsmodus</span>
</div>
<div style="font-size:10px;color:#94A3B8;margin-bottom:10px">
  <div><strong>IFC-Typ:</strong> ${esc(d.ifcType)} &nbsp;|&nbsp; <strong>GlobalId:</strong> <span style="font-family:monospace">${esc(d.globalId)}</span></div>
  <div style="margin-top:2px"><strong>Express-ID:</strong> ${esc(String(d.expressId))}</div>
</div>
<div style="border-top:1px solid #E2E8F0;padding-top:10px;margin-bottom:4px;font-size:10px;font-weight:700;color:#1E40AF;text-transform:uppercase;letter-spacing:.4px">Attribute</div>`;
  const attrs=[["Name","Name",d.name],["ObjectType","ObjectType",d.objectType],["Description","Description",d.description],["Tag","Tag",d.tag]];
  for(const[label,field,val]of attrs){
    h+=`<div style="margin-bottom:6px">
  <div style="color:#64748B;font-size:10px;margin-bottom:3px;font-weight:500">${esc(label)}</div>
  <input type="text" data-edit-field="${esc(field)}" value="${esc(val||"")}" placeholder="${esc(label)}" style="${_inpS()}">
</div>`;
  }
  h+=`<div style="border-top:1px solid #E2E8F0;padding-top:10px;margin-top:6px;margin-bottom:6px;
    display:flex;align-items:center;justify-content:space-between">
  <span style="font-size:10px;font-weight:700;color:#1E40AF;text-transform:uppercase;letter-spacing:.4px">Property Sets</span>
  <button onclick="addNewPset()" style="font-size:10px;padding:3px 9px;
    background:#EFF6FF;border:1px solid #BFDBFE;color:#1E40AF;border-radius:5px;cursor:pointer;font-family:inherit">+ Neues PSet</button>
</div>
<div id="pset-edit-container">`;
  const psets=d.psets||{};
  for(const[pn,props]of Object.entries(psets)){
    h+=_renderPsetEditBlock(pn,props);
  }
  h+=`</div>
<div style="border-top:1px solid #E2E8F0;padding-top:12px;margin-top:12px;display:flex;flex-direction:column;gap:6px">
  <div style="display:flex;gap:6px">
    <button onclick="saveEdits('overwrite')" style="flex:1;padding:8px;background:#065F46;border:1px solid rgba(6,95,70,.3);color:#ECFDF5;border-radius:7px;cursor:pointer;font-size:12px;font-weight:600;font-family:inherit">
      💾 Überschreiben
    </button>
    <button onclick="saveEdits('save_as')" style="flex:1;padding:8px;background:#1E3A5F;border:1px solid rgba(30,58,95,.4);color:#DBEAFE;border-radius:7px;cursor:pointer;font-size:12px;font-weight:600;font-family:inherit">
      📋 Speichern als…
    </button>
  </div>
  <button onclick="leaveEditMode()" style="padding:7px;background:#F1F5F9;border:1px solid #E2E8F0;color:#64748B;border-radius:7px;cursor:pointer;font-size:11px;font-family:inherit">
    ✕ Abbrechen
  </button>
  <div id="edit-save-status" style="font-size:11px;min-height:18px;text-align:center"></div>
</div>`;
  infoBody.innerHTML=h;
}

function _renderPsetEditBlock(psetName,props){
  const filteredProps=Object.entries(props).filter(([k])=>k!=="id");
  let rows="";
  if(filteredProps.length===0){
    rows=_propEditRow("","");
  } else {
    for(const[k,v]of filteredProps) rows+=_propEditRow(k,v);
  }
  return `<div class="pset-edit-block" data-pset="${esc(psetName)}"
  style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:7px;margin-bottom:8px;overflow:hidden">
  <div onclick="togglePsetBlock(this)"
    style="display:flex;align-items:center;justify-content:space-between;padding:5px 10px;
    background:#F1F5F9;cursor:pointer;border-bottom:1px solid #E2E8F0">
    <input type="text" class="pset-name-input" value="${esc(psetName)}"
      onclick="event.stopPropagation()"
      style="${_inpS()}max-width:160px;font-weight:600;color:#1E40AF;font-size:11px;background:transparent;border:1px dashed #93C5FD;"
      placeholder="PSet-Name">
    <div style="display:flex;gap:4px;align-items:center;flex-shrink:0;margin-left:6px">
      <button onclick="event.stopPropagation();addPsetProp(this)" title="Eigenschaft hinzufügen"
        style="font-size:10px;padding:2px 8px;background:#DBEAFE;border:1px solid #93C5FD;color:#1E40AF;border-radius:4px;cursor:pointer;font-family:inherit">+</button>
      <span class="pset-toggle-arrow" style="color:#94A3B8;font-size:11px">▼</span>
    </div>
  </div>
  <div class="pset-props-wrap" style="padding:6px 8px">${rows}</div>
</div>`;
}

function _propEditRow(k,v){
  return `<div class="prop-edit-row" style="display:flex;gap:4px;margin-bottom:5px;align-items:center">
  <input type="text" data-prop-key value="${esc(k)}" placeholder="Eigenschaft"
    style="${_inpS()}flex:1;min-width:0" oninput="markDirty()">
  <span style="color:#CBD5E1;flex-shrink:0;padding:0 2px">=</span>
  <input type="text" data-prop-val value="${esc(v)}" placeholder="Wert"
    style="${_inpS()}flex:1;min-width:0" oninput="markDirty()">
  <button onclick="removePropRow(this)" title="Entfernen"
    style="flex-shrink:0;padding:3px 7px;font-size:10px;background:#FEE2E2;border:1px solid rgba(220,38,38,.25);color:#DC2626;border-radius:4px;cursor:pointer">✕</button>
</div>`;
}

function togglePsetBlock(header){
  const wrap=header.nextElementSibling, arrow=header.querySelector(".pset-toggle-arrow"), hidden=wrap.style.display==="none";
  wrap.style.display=hidden?"":"none"; if(arrow)arrow.textContent=hidden?"▼":"▶";
}
function markDirty(){ const el=document.getElementById("edit-save-status"); if(el)el.innerHTML=""; }
function addNewPset(){
  const name=(prompt("Name des neuen Property Sets:","Pset_Custom")||"").trim();
  if(!name) return;
  const cont=document.getElementById("pset-edit-container"); if(!cont)return;
  const tmp=document.createElement("div"); tmp.innerHTML=_renderPsetEditBlock(name,{});
  const block=tmp.firstElementChild; cont.appendChild(block);
  const fi=block.querySelector("[data-prop-key]"); if(fi)fi.focus();
}
function addPsetProp(btn){
  const block=btn.closest(".pset-edit-block"); if(!block)return;
  const wrap=block.querySelector(".pset-props-wrap"); if(!wrap)return;
  if(wrap.style.display==="none"){ wrap.style.display=""; const arrow=block.querySelector(".pset-toggle-arrow"); if(arrow)arrow.textContent="▼"; }
  const tmp=document.createElement("div"); tmp.innerHTML=_propEditRow("","");
  const row=tmp.firstElementChild; wrap.appendChild(row);
  const ki=row.querySelector("[data-prop-key]"); if(ki)ki.focus();
  markDirty();
}
function removePropRow(btn){ btn.closest(".prop-edit-row")?.remove(); markDirty(); }

function enterEditMode(){
  if(!_editElemData) return;
  _editMode=true;
  document.getElementById("info-panel-title").textContent="Bearbeiten";
  _renderEditMode(_editElemData);
}
function leaveEditMode(){
  _editMode=false;
  document.getElementById("info-panel-title").textContent="Element-Info";
  _renderReadOnly(_editElemData);
}

function _collectEdits(){
  const changes={};
  for(const inp of infoBody.querySelectorAll("[data-edit-field]")) changes[inp.dataset.editField]=inp.value;
  const psets={};
  for(const block of infoBody.querySelectorAll(".pset-edit-block")){
    const nameInp=block.querySelector(".pset-name-input");
    const psetName=(nameInp?.value||block.dataset.pset||"").trim(); if(!psetName)continue;
    const props={};
    for(const row of block.querySelectorAll(".prop-edit-row")){
      const k=row.querySelector("[data-prop-key]")?.value?.trim();
      const v=row.querySelector("[data-prop-val]")?.value??"";
      if(k) props[k]=v;
    }
    psets[psetName]=props;
  }
  changes.psets=psets;
  return changes;
}

async function saveEdits(mode){
  if(!_editDocumentId||!_editExpressId){ alert("Kein Dokument ausgewählt."); return; }
  const status=document.getElementById("edit-save-status");
  const changes=_collectEdits();
  let new_filename="";
  if(mode==="save_as"){
    const base=((_editElemData?.modelLabel)||"modell").replace(/\.[^.]+$/,"");
    new_filename=(prompt("Dateiname für neue IFC-Datei:", `${base}_bearbeitet.ifc`)||"").trim();
    if(!new_filename) return;
    if(!new_filename.toLowerCase().endsWith(".ifc")) new_filename+=".ifc";
  }
  if(status) status.innerHTML='<span style="color:#1E40AF">⏳ Wird gespeichert…</span>';
  try{
    const resp=await fetch(`/projects/${encodeURIComponent(PROJECT_ID)}/view/pset/save`,{
      method:"POST", headers:{"Content-Type":"application/json"},
      body:JSON.stringify({document_id:_editDocumentId,express_id:_editExpressId,save_mode:mode,new_filename,changes})
    });
    const data=await resp.json();
    if(!resp.ok||data.error){ if(status)status.innerHTML=`<span style="color:#DC2626">⚠ ${esc(data.error||"Fehler")}</span>`; return; }
    if(mode==="save_as"){
      if(status)status.innerHTML=`<span style="color:#059669">✓ Gespeichert als „${esc(new_filename)}"</span>`;
      setTimeout(()=>{ if(confirm(`„${new_filename}" wurde im Documents-Modul gespeichert. Seite neu laden?`)) location.reload(); },400);
    } else {
      if(status)status.innerHTML='<span style="color:#059669">✓ Datei in R2 überschrieben</span>';
      setTimeout(()=>{ if(status&&status.innerHTML.includes("✓")) status.innerHTML=""; },4000);
    }
  }catch(err){
    if(status)status.innerHTML=`<span style="color:#DC2626">⚠ ${esc(err.message)}</span>`;
  }
}

function showInfo(m){
  const d=m.userData;
  _editDocumentId=d.documentId||d.docId||"";
  _editExpressId=d.expressId;
  _editElemData=d;
  _editMode=false;
  document.getElementById("info-panel-title").textContent="Element-Info";
  _renderReadOnly(d);
}

const raycaster=new THREE.Raycaster(), mouse=new THREE.Vector2();
canvas.addEventListener("mousedown",()=>{mouseMoved=false;});
canvas.addEventListener("mousemove",()=>{mouseMoved=true;});
canvas.addEventListener("mouseup",e=>{
  if(e.button!==0||mouseMoved)return;
  const rect=canvas.getBoundingClientRect();
  mouse.x=((e.clientX-rect.left)/rect.width)*2-1;
  mouse.y=-((e.clientY-rect.top)/rect.height)*2+1;
  raycaster.setFromCamera(mouse,camera);
  const hits=raycaster.intersectObjects(allMeshes().filter(m=>m.visible),false);
  if(hits.length>0){
    const m=hits[0].object;
    if(selectedMesh&&selectedMesh!==m)selectedMesh.material.color.copy(selectedMesh.userData._origColor);
    if(!m.userData._origColor)m.userData._origColor=m.material.color.clone();
    m.material.color.copy(HIGHLIGHT); selectedMesh=m;
    showInfo(m);
    const panel=document.getElementById("info-panel"); if(panel)panel.style.display="flex";
  } else {
    if(selectedMesh){selectedMesh.material.color.copy(selectedMesh.userData._origColor);selectedMesh=null;}
    infoBody.innerHTML='<div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0;line-height:1.6">Klicken Sie auf ein Element<br>für Details.</div>';
  }
});

function selectMesh(m){
  if(selectedMesh&&selectedMesh!==m)selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  if(!m.userData._origColor)m.userData._origColor=m.material.color.clone();
  m.material.color.copy(HIGHLIGHT); selectedMesh=m; showInfo(m);
  const panel=document.getElementById("info-panel"); if(panel)panel.style.display="flex";
}

window.addEventListener("keydown",e=>{
  if(e.code!=="Space"||!selectedMesh)return; e.preventDefault();
  const id=meshInstanceKey(selectedMesh); hiddenIds.add(id); selectedMesh.visible=false;
  selectedMesh.material.color.copy(selectedMesh.userData._origColor); selectedMesh=null;
  infoBody.innerHTML='<div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0">Element ausgeblendet.</div>';
  updateHiddenCount();
});

document.getElementById("btn-fit")?.addEventListener("click", fitAll);
document.getElementById("btn-reset")?.addEventListener("click",()=>{ orb.tgt.set(0,0,0); orb.sph.set(80,Math.PI/4,Math.PI/4); applyOrb(); });
document.getElementById("btn-show-all")?.addEventListener("click",()=>{ hiddenIds.clear(); applyVisibility(); });
document.getElementById("info-close")?.addEventListener("click",()=>{ if(selectedMesh&&selectedMesh.userData._origColor){ selectedMesh.material.color.copy(selectedMesh.userData._origColor); selectedMesh=null; } infoBody.innerHTML='<div style="color:#8896A5;font-style:italic;text-align:center;padding:28px 0;line-height:1.6">Klicken Sie auf ein Element<br>für Details.</div>'; });

// ════════════════════════════════════════════════════════════════════════════
// Model-Sidebar selection
// ════════════════════════════════════════════════════════════════════════════
function onDocToggle(docId,checked){
  const lbl=document.getElementById(`lbl-${docId}`); if(!lbl)return;
  lbl.style.background=checked?"rgba(30,111,191,0.06)":"transparent";
  lbl.style.borderColor=checked?"rgba(30,111,191,0.2)":"transparent";
  const dot=lbl.querySelector("span:last-child"); if(dot)dot.style.opacity=checked?"1":"0.3";
}
function _saveViewerDocState(ids){
  try{
    if(ids.length>0) sessionStorage.setItem(VIEWER_STATE_KEY,JSON.stringify(ids));
    else sessionStorage.removeItem(VIEWER_STATE_KEY);
  }catch(_){}
}
function applyNavSelection(){
  const form=document.getElementById("model-select-form"); if(!form)return;
  const checked=[...form.querySelectorAll("input[name=doc_ids]:checked")].map(i=>i.value);
  _saveViewerDocState(checked);
  const url=new URL(window.location.href); url.searchParams.delete("doc_ids");
  checked.forEach(id=>url.searchParams.append("doc_ids",id)); window.location.href=url.toString();
}
function applyDocSelection(){ applyNavSelection(); }
function toggleAllDocs(v){
  const form=document.getElementById("model-select-form"); if(!form)return;
  form.querySelectorAll("input[name=doc_ids]").forEach(c=>{c.checked=v; onDocToggle(c.value,v);});
}
document.getElementById("btn-apply-select")?.addEventListener("click",applyNavSelection);

// ════════════════════════════════════════════════════════════════════════════
// GlobalId search
// ════════════════════════════════════════════════════════════════════════════
const searchInput=document.getElementById("gid-search"), searchResults=document.getElementById("search-results"), searchClear=document.getElementById("search-clear");
const searchIndex=[];
function buildSearchIndex(){ searchIndex.length=0; const seen=new Set(); for(const m of allMeshes()){const gid=m.userData.globalId;if(!gid||seen.has(gid))continue;seen.add(gid);searchIndex.push({globalId:gid,expressId:m.userData.expressId,name:m.userData.name,ifcType:m.userData.ifcType,docId:m.userData.docId,modelLabel:m.userData.modelLabel,slotColor:m.userData.slotColor,typeStyle:m.userData.typeStyle});} }
function renderSearch(q){ q=q.trim().toLowerCase(); if(!q){searchResults.style.display="none";searchClear.style.display="none";return;} searchClear.style.display="inline"; const hits=searchIndex.filter(e=>e.globalId.toLowerCase().includes(q)).slice(0,50); if(!hits.length){searchResults.innerHTML='<div style="padding:10px 14px;font-size:12px;color:#94A3B8">Keine Ergebnisse</div>';searchResults.style.display="block";return;} let h=`<div style="padding:6px 14px;font-size:10px;color:#1E6FBF;font-weight:600;border-bottom:1px solid #E2E8F0">${hits.length} Treffer</div>`; h+=hits.map(el=>{const col=el.slotColor||"#1E6FBF",ts=el.typeStyle||getTypeStyle(el.ifcType),tHex="#"+new THREE.Color(ts.color).getHexString(); return `<div class="s-row" data-gid="${esc(el.globalId)}" style="padding:8px 14px;cursor:pointer;border-bottom:1px solid #F1F5F9"><div style="display:flex;align-items:center;gap:5px;margin-bottom:2px"><span style="width:7px;height:7px;border-radius:50%;background:${tHex};flex-shrink:0"></span><span style="font-size:12px;color:#0D1B2A;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(el.name||el.ifcType)}</span><span style="font-size:10px;color:${col};flex-shrink:0">${esc(el.ifcType.replace(/^Ifc/,""))}</span></div><div style="font-size:10px;color:#64748B;font-family:monospace;padding-left:12px">${esc(el.globalId)}</div></div>`; }).join(""); searchResults.innerHTML=h; searchResults.style.display="block"; searchResults.querySelectorAll(".s-row").forEach(row=>{row.addEventListener("mouseenter",()=>row.style.background="#F8FAFC");row.addEventListener("mouseleave",()=>row.style.background="");row.addEventListener("click",()=>{const gid=row.dataset.gid;const mesh=allMeshes().find(m=>m.userData.globalId===gid);if(!mesh)return;selectMesh(mesh);const box=new THREE.Box3().setFromObject(mesh);if(!box.isEmpty()){orb.tgt.copy(box.getCenter(new THREE.Vector3()));orb.sph.radius=Math.max(box.getSize(new THREE.Vector3()).length()*2.5,2);applyOrb();}searchResults.style.display="none";});});}
searchInput.addEventListener("input",e=>renderSearch(e.target.value));
searchInput.addEventListener("keydown",e=>{if(e.key==="Escape"){searchInput.value="";renderSearch("");}});
searchClear.addEventListener("click",()=>{searchInput.value="";renderSearch("");searchInput.focus();});
document.addEventListener("mousedown",e=>{const bar=document.getElementById("search-bar");if(bar&&!bar.contains(e.target))searchResults.style.display="none";});

// ════════════════════════════════════════════════════════════════════════════
// Render loop + bootstrap
// ════════════════════════════════════════════════════════════════════════════
(function animate(){ requestAnimationFrame(animate); renderer.render(scene,camera); })();

function esc(s){ return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }

(async()=>{
  if(!MODEL_URLS.length){ if(loadEl)loadEl.style.display="none"; if(loadStatus)loadStatus.textContent="Kein Modell ausgewählt."; return; }
  try{
    await initWebIfc();
    let total=0;
    for(let i=0;i<MODEL_URLS.length;i++){
      try{ const v=await loadModel(MODEL_URLS[i],i+1,MODEL_URLS.length); total+=v; }
      catch(err){ console.error("Ladefehler:",MODEL_URLS[i].label,err); if(loadStatus)loadStatus.textContent=`⚠ ${err.message}`; }
    }
    if(loadBar)loadBar.style.width="100%";
    buildCategoryUI(); buildSearchIndex(); fitAll();
    if(loadStatus)loadStatus.textContent=`✓ ${MODEL_URLS.length} Modell(e) · ${total.toLocaleString()} Punkte`;
  }catch(err){ if(loadTxt)loadTxt.textContent="Fehler: "+err.message; console.error(err); }
  finally{ if(loadEl)loadEl.style.display="none"; }
})();

"""
    return (
        js.replace("__MODEL_URLS__", model_urls_js)
          .replace("__PROJECT_ID__", json.dumps(str(project_id)))
    )
