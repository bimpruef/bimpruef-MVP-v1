"""
project_viewer.py – BIMPruef Direct Viewer

ماژول مستقل Viewer که مستقیم از Documents/R2 می‌خونه.
هیچ وابستگی‌ای به session/slot قدیمی نداره.

روت‌ها:
  GET  /projects/{project_id}/view               → صفحه انتخاب مدل
  GET  /projects/{project_id}/view/file/{doc_id} → stream مستقیم IFC از R2
  POST /projects/{project_id}/view/load          → لود مدل‌های انتخاب‌شده
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


# ─────────────────────────────────────────────────────────────────────────────
# File streaming endpoint
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/view/file/{document_id}")
def view_file_stream(request: Request, project_id: str, document_id: str):
    account = _account_from_request(request)
    account_id = account["account_id"]

    try:
        doc = get_document(account_id, project_id, document_id)
    except NotFoundError:
        return Response(content="Document not found", status_code=404)

    if not doc.get("r2_key"):
        return Response(content="R2 key missing", status_code=404)

    if not (r2_enabled() and download_file_from_r2):
        return Response(content="R2 not configured", status_code=503)

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
        return Response(content=f"Error loading file: {exc}", status_code=500)
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
                raise ValueError("No .ifc file inside IFCZIP")
            return zf.read(ifc_names[0])
    return raw


# ─────────────────────────────────────────────────────────────────────────────
# Main viewer page (model selection)
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

    # If no doc_ids selected, show selection page
    if not doc_ids:
        return _render_select_page(account, project, project_id, all_ifc_docs, error)

    valid_docs = {d["document_id"]: d for d in all_ifc_docs}
    selected_docs = [valid_docs[did] for did in doc_ids if did in valid_docs]

    if not selected_docs:
        return _render_select_page(account, project, project_id, all_ifc_docs,
                                   "هیچ مدل معتبری انتخاب نشده.")

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

    if not doc_ids:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/view?error=حداقل+یک+مدل+انتخاب+کنید",
            status_code=303,
        )

    from urllib.parse import urlencode
    params = urlencode([("doc_ids", did) for did in doc_ids])
    return RedirectResponse(f"/projects/{_e(project_id)}/view?{params}", status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Selection Page
# ─────────────────────────────────────────────────────────────────────────────

def _render_select_page(account, project, project_id, docs, error="") -> HTMLResponse:
    from app.projects import _page, _project_subnav, _topbar_global

    pid = _e(project_id)
    flash = f'<div class="flash-err" style="margin-bottom:16px">⚠ {_e(error)}</div>' if error else ""

    if not docs:
        content = f"""
        {flash}
        <div class="bp-card" style="text-align:center;padding:48px 32px;max-width:560px;margin:0 auto">
          <div style="width:56px;height:56px;background:#EFF6FF;border-radius:12px;
            display:flex;align-items:center;justify-content:center;margin:0 auto 16px">
            <svg width="24" height="24" fill="none" stroke="#1E6FBF" stroke-width="1.8" viewBox="0 0 24 24">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
            </svg>
          </div>
          <h3 style="font-size:17px;margin-bottom:8px;color:#0D1B2A">فایل IFC وجود ندارد</h3>
          <p style="color:#4A5568;font-size:14px;margin-bottom:24px">
            ابتدا در ماژول Documents فایل IFC آپلود کنید.
          </p>
          <a class="bp-btn bp-btn--primary" href="/projects/{pid}/documents" style="text-decoration:none">
            رفتن به Documents
          </a>
        </div>
        """
    else:
        doc_cards = ""
        for i, d in enumerate(docs):
            col = _slot_color(i)
            col_light = _color_to_light_bg(col)
            ext_badge = _e(d['file_extension'].upper().lstrip('.'))
            doc_cards += f"""
            <label style="display:flex;align-items:center;gap:14px;padding:14px 16px;
              border:1.5px solid #E2E8F0;border-radius:10px;cursor:pointer;
              background:#FFFFFF;transition:all 0.15s ease;
              box-shadow:0 1px 3px rgba(13,27,42,0.05)"
              onmouseenter="this.style.borderColor='{col}';this.style.boxShadow='0 2px 8px rgba(13,27,42,0.1)'"
              onmouseleave="this.style.borderColor='#E2E8F0';this.style.boxShadow='0 1px 3px rgba(13,27,42,0.05)'">
              <input type="checkbox" name="doc_ids" value="{_e(d['document_id'])}" checked
                style="width:16px;height:16px;accent-color:{col};flex-shrink:0;cursor:pointer">
              <div style="width:36px;height:36px;border-radius:8px;background:{col_light};
                display:flex;align-items:center;justify-content:center;flex-shrink:0">
                <span style="font-size:10px;font-weight:700;color:{col}">{ext_badge}</span>
              </div>
              <div style="flex:1;min-width:0">
                <div style="font-weight:600;font-size:13px;color:#0D1B2A;
                  overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{_e(d['original_filename'])}</div>
                <div style="font-size:11px;color:#8896A5;margin-top:2px">
                  {_e(d.get('folder_path') or 'Root')} · {_fmt_size(d.get('file_size', 0))}
                </div>
              </div>
              <div style="width:10px;height:10px;border-radius:50%;
                background:{col};flex-shrink:0"></div>
            </label>"""

        content = f"""
        {flash}
        <div style="max-width:640px;margin:0 auto">
          <div class="bp-card">
            <div style="margin-bottom:20px">
              <h3 style="font-size:16px;font-weight:600;color:#0D1B2A;margin-bottom:6px">
                انتخاب مدل‌های IFC
              </h3>
              <p style="color:#4A5568;font-size:13px">
                مدل‌هایی که می‌خواهید در Viewer نمایش داده شوند را انتخاب کنید.
              </p>
            </div>
            <form method="POST" action="/projects/{pid}/view/load" id="select-form">
              <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px">
                {doc_cards}
              </div>
              <div style="display:flex;gap:10px;align-items:center">
                <button type="submit" class="bp-btn bp-btn--primary" style="flex:1;justify-content:center">
                  <svg width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24">
                    <polygon points="5 3 19 12 5 21 5 3"/>
                  </svg>
                  نمایش مدل‌های انتخابی
                </button>
                <a href="/projects/{pid}" class="bp-btn bp-btn--secondary" style="text-decoration:none">
                  ← پروژه
                </a>
              </div>
            </form>
          </div>

          <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
            <button type="button" onclick="toggleAll(true)"
              style="font-size:12px;padding:4px 12px;background:#F0F2F5;border:1px solid #E2E8F0;
              border-radius:6px;cursor:pointer;color:#4A5568">همه</button>
            <button type="button" onclick="toggleAll(false)"
              style="font-size:12px;padding:4px 12px;background:#F0F2F5;border:1px solid #E2E8F0;
              border-radius:6px;cursor:pointer;color:#4A5568">هیچکدام</button>
          </div>
        </div>
        <script>
        function toggleAll(v) {{
          document.querySelectorAll('input[name="doc_ids"]').forEach(c => c.checked = v);
        }}
        </script>
        """

    body = f"""
    {_topbar_global(account)}
    {_project_subnav(project_id, "model")}
    <div style="padding:32px;background:#F5F6F8;min-height:calc(100vh - 94px)">
      <div style="display:flex;align-items:center;justify-content:space-between;
        margin-bottom:24px;max-width:640px;margin-left:auto;margin-right:auto">
        <div>
          <h1 style="font-size:22px;font-weight:700;color:#0D1B2A;margin-bottom:4px">
            Direct Viewer
          </h1>
          <p style="color:#4A5568;font-size:13px">{_e(project['project_name'])}</p>
        </div>
      </div>
      {content}
    </div>
    """
    return _page(f"{project['project_name']} – Viewer", body)


def _color_to_light_bg(hex_color: str) -> str:
    """Convert hex color to a light background version."""
    mapping = {
        "#1E6FBF": "#EFF6FF",
        "#D97706": "#FFFBEB",
        "#059669": "#ECFDF5",
        "#DC2626": "#FEF2F2",
        "#7C3AED": "#F5F3FF",
        "#0891B2": "#ECFEFF",
        "#BE185D": "#FDF2F8",
        "#65A30D": "#F7FEE7",
    }
    return mapping.get(hex_color, "#F5F6F8")


# ─────────────────────────────────────────────────────────────────────────────
# Viewer Page
# ─────────────────────────────────────────────────────────────────────────────

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

    # Sidebar doc list
    select_rows = ""
    for i, doc in enumerate(all_ifc_docs):
        checked = "checked" if doc["document_id"] in [d["document_id"] for d in selected_docs] else ""
        col = _slot_color(i)
        select_rows += f"""
        <label style="display:flex;align-items:center;gap:8px;padding:7px 10px;
          border-radius:6px;cursor:pointer;font-size:12px;
          background:{'#EFF6FF' if checked else 'transparent'};
          transition:background 0.12s"
          id="lbl-{_e(doc['document_id'])}"
          onmouseenter="if(!this.querySelector('input').checked)this.style.background='#F5F6F8'"
          onmouseleave="if(!this.querySelector('input').checked)this.style.background='transparent'">
          <input type="checkbox" name="doc_ids" value="{_e(doc['document_id'])}" {checked}
            style="width:13px;height:13px;accent-color:{col};flex-shrink:0;cursor:pointer"
            onchange="onDocToggle('{_e(doc['document_id'])}',this.checked,'{col}')">
          <span style="width:9px;height:9px;border-radius:50%;background:{col};flex-shrink:0"></span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;color:#0D1B2A"
            title="{_e(doc['original_filename'])}">{_e(doc['original_filename'])}</span>
          <span style="color:#8896A5;flex-shrink:0;font-size:11px">{_fmt_size(doc.get('file_size',0))}</span>
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

    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "model")}

<div style="display:flex;flex-direction:column;height:calc(100vh - 94px);overflow:hidden;
  position:relative;background:#F5F6F8">
  {error_html}

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- ─── Sidebar ──────────────────────────────────────────────────────── -->
    <div id="sidebar" style="width:260px;min-width:260px;background:#FFFFFF;
      border-right:1px solid #E2E8F0;display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0;box-shadow:1px 0 4px rgba(13,27,42,0.04)">

      <!-- مدل‌ها -->
      <div style="padding:10px 12px;font-size:10px;font-weight:700;background:#F8FAFC;
        color:#64748B;text-transform:uppercase;letter-spacing:.8px;
        border-bottom:1px solid #E2E8F0;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>📁 مدل‌ها</span>
        <button onclick="applyDocSelection()" id="btn-apply-select" title="اعمال تغییرات"
          style="display:none;font-size:10px;padding:2px 8px;background:#EFF6FF;
          border:1px solid #BFDBFE;color:#1E6FBF;border-radius:4px;cursor:pointer;font-weight:600">
          ✓ اعمال
        </button>
      </div>

      <div style="padding:6px 8px;border-bottom:1px solid #E2E8F0;flex-shrink:0;
        max-height:200px;overflow-y:auto">
        <form id="model-select-form" method="GET" action="/projects/{pid}/view">
          {select_rows}
        </form>
      </div>

      <!-- IFC ساختار -->
      <div style="padding:8px 12px;font-size:10px;font-weight:700;background:#F8FAFC;
        color:#64748B;text-transform:uppercase;letter-spacing:.8px;
        border-bottom:1px solid #E2E8F0;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>🏗 ساختار IFC</span>
        <span style="display:flex;gap:6px">
          <button id="btn-cat-all" style="font-size:10px;cursor:pointer;color:#1E6FBF;
            background:none;border:none;padding:0;font-weight:600">همه</button>
          <button id="btn-cat-none" style="font-size:10px;cursor:pointer;color:#1E6FBF;
            background:none;border:none;padding:0;font-weight:600">هیچ</button>
        </span>
      </div>
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:4px 0">
        <div style="padding:12px;font-size:12px;color:#8896A5;font-style:italic">
          در حال بارگذاری…
        </div>
      </div>

      <!-- وضعیت -->
      <div id="load-status" style="padding:6px 12px;font-size:11px;color:#64748B;
        border-top:1px solid #E2E8F0;flex-shrink:0;background:#F8FAFC;
        overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></div>
    </div>

    <!-- ─── Canvas ──────────────────────────────────────────────────────── -->
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden;background:#F0F2F5">

      <canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>

      <!-- جستجوی GlobalId -->
      <div id="search-bar" style="position:absolute;top:12px;left:12px;z-index:10;width:300px">
        <div style="display:flex;gap:4px">
          <div style="flex:1;position:relative">
            <svg style="position:absolute;left:9px;top:50%;transform:translateY(-50%);pointer-events:none"
              width="14" height="14" fill="none" stroke="#8896A5" stroke-width="2" viewBox="0 0 24 24">
              <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
            </svg>
            <input id="gid-search" type="text" placeholder="جستجوی GlobalId…"
              style="width:100%;background:rgba(255,255,255,0.96);border:1px solid #E2E8F0;
              color:#0D1B2A;padding:7px 10px 7px 30px;border-radius:8px;font-size:12px;
              outline:none;backdrop-filter:blur(8px);
              box-shadow:0 2px 8px rgba(13,27,42,0.08)"
              autocomplete="off">
          </div>
          <button id="search-clear" style="display:none;background:rgba(255,255,255,0.96);
            border:1px solid #E2E8F0;color:#8896A5;border-radius:8px;
            padding:6px 10px;cursor:pointer;font-size:12px;
            box-shadow:0 2px 8px rgba(13,27,42,0.08)">✕</button>
        </div>
        <div id="search-results" style="display:none;margin-top:4px;
          background:rgba(255,255,255,0.98);border:1px solid #E2E8F0;
          border-radius:8px;max-height:260px;overflow-y:auto;
          box-shadow:0 4px 16px rgba(13,27,42,0.12)"></div>
      </div>

      <!-- دکمه‌های Overlay -->
      <div style="position:absolute;top:12px;right:12px;display:flex;gap:6px;z-index:6">
        <button id="btn-fit" style="font-size:12px;padding:6px 12px;background:rgba(255,255,255,0.95);
          border:1px solid #E2E8F0;border-radius:7px;cursor:pointer;color:#0D1B2A;
          box-shadow:0 2px 6px rgba(13,27,42,0.08);transition:all 0.12s">⊡ Fit</button>
        <button id="btn-reset" style="font-size:12px;padding:6px 12px;background:rgba(255,255,255,0.95);
          border:1px solid #E2E8F0;border-radius:7px;cursor:pointer;color:#0D1B2A;
          box-shadow:0 2px 6px rgba(13,27,42,0.08);transition:all 0.12s">⟳ دوربین</button>
        <button id="btn-show-all" style="font-size:12px;padding:6px 12px;display:none;
          background:rgba(254,242,242,0.96);border:1px solid rgba(220,38,38,0.3);
          border-radius:7px;cursor:pointer;color:#DC2626;
          box-shadow:0 2px 6px rgba(13,27,42,0.08)">👁 همه</button>
        <span id="hidden-count" style="font-size:11px;color:#DC2626;display:none;
          align-self:center;background:rgba(254,242,242,0.95);
          padding:4px 8px;border-radius:6px;border:1px solid rgba(220,38,38,0.2)"></span>
      </div>

      <!-- راهنما -->
      <div style="position:absolute;bottom:12px;right:12px;font-size:11px;color:#8896A5;
        background:rgba(255,255,255,0.85);padding:5px 10px;border-radius:6px;
        border:1px solid #E2E8F0;pointer-events:none;backdrop-filter:blur(4px)">
        LMB چرخش · MMB Pan · Scroll زوم · Space مخفی
      </div>

      <!-- Loading overlay -->
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:rgba(240,242,245,0.95);z-index:20">
        <div style="width:44px;height:44px;border:3px solid #BFDBFE;
          border-top-color:#1E6FBF;border-radius:50%;
          animation:spin .7s linear infinite;margin-bottom:16px"></div>
        <p id="load-txt" style="color:#4A5568;font-size:13px;margin:0">
          اتصال به R2…
        </p>
        <div id="load-progress" style="margin-top:12px;width:220px;height:3px;
          background:#E2E8F0;border-radius:2px;overflow:hidden">
          <div id="load-bar" style="width:0%;height:100%;background:#1E6FBF;
            transition:width .3s ease;border-radius:2px"></div>
        </div>
      </div>
    </div>

    <!-- ─── Info Panel ──────────────────────────────────────────────────── -->
    <div id="info-panel" style="width:300px;min-width:300px;background:#FFFFFF;
      border-left:1px solid #E2E8F0;display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0;box-shadow:-1px 0 4px rgba(13,27,42,0.04);
      transition:width 0.2s ease">
      <div style="padding:10px 12px;font-size:10px;font-weight:700;background:#F8FAFC;
        color:#64748B;text-transform:uppercase;letter-spacing:.8px;
        border-bottom:1px solid #E2E8F0;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between">
        <span>اطلاعات عنصر</span>
        <span id="info-close" style="cursor:pointer;color:#8896A5;font-size:16px;
          padding:0 4px;line-height:1" title="بستن">✕</span>
      </div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:14px;font-size:12px">
        <div style="color:#8896A5;font-style:italic;text-align:center;padding:24px 0">
          روی یک عنصر کلیک کنید.
        </div>
      </div>
    </div>

  </div>
</div>

<style>
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}
#btn-fit:hover, #btn-reset:hover {{
  background: rgba(255,255,255,1) !important;
  box-shadow: 0 3px 10px rgba(13,27,42,0.12) !important;
}}
</style>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_direct_viewer_js(model_urls_js)}
</script>
"""
    return _page(f"{project['project_name']} – Direct Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# Three.js JavaScript (light theme, IFC colors, fixed search)
# ─────────────────────────────────────────────────────────────────────────────

def _direct_viewer_js(model_urls_js: str) -> str:
    return f"const MODEL_URLS = [{model_urls_js}];\n" + r"""

// ═══════════════════════════════════════════════════════════════════
// IFC رنگ‌بندی (برای تم روشن)
// ═══════════════════════════════════════════════════════════════════
const TYPE_COLOR = {
  IfcWall:0xa07840, IfcWallStandardCase:0xa07840, IfcCurtainWall:0xc09030,
  IfcColumn:0x2a5c8a, IfcColumnStandardCase:0x2a5c8a,
  IfcBeam:0x3a78b8, IfcBeamStandardCase:0x3a78b8,
  IfcSlab:0x5a8ea8, IfcSlabStandardCase:0x5a8ea8,
  IfcRoof:0x6b3db8, IfcDoor:0xb85030, IfcWindow:0x40b8d0,
  IfcStair:0x9a5030, IfcStairFlight:0x9a5030,
  IfcRamp:0xb88820, IfcRailing:0x405860,
  IfcPlate:0x6090a8, IfcMember:0x3878a0, IfcCovering:0x608858,
  IfcFooting:0x1a4060, IfcPile:0x103050,
  IfcBuildingElementProxy:0x707080,
  IfcFurnishingElement:0xb87050, IfcFurniture:0xb87050,
  IfcSpace:0x88c888, IfcOpeningElement:0xdddddd,
  IfcPipeSegment:0x108870, IfcDuctSegment:0x506870,
};
const TYPE_FALLBACK = [
  [/^IfcWall/,0xa07840],[/^IfcSlab/,0x5a8ea8],[/^IfcColumn/,0x2a5c8a],
  [/^IfcBeam/,0x3a78b8],[/^IfcStair/,0x9a5030],[/^IfcRoof/,0x6b3db8],
  [/^IfcDoor/,0xb85030],[/^IfcWindow/,0x40b8d0],[/^IfcPipe/,0x108870],
  [/^IfcFurnish/,0xb87050],[/^IfcElectric/,0xc09810],
];
const FLAT_TYPES = new Set(["IfcOpeningElement","IfcAnnotation","IfcGrid","IfcSpace"]);

function getColor(t) {
  if (TYPE_COLOR[t] !== undefined) return new THREE.Color(TYPE_COLOR[t]);
  for (const [rx, hex] of TYPE_FALLBACK) if (rx.test(t)) return new THREE.Color(hex);
  return new THREE.Color(0x607080);
}

// ═══════════════════════════════════════════════════════════════════
// Three.js Setup (light background)
// ═══════════════════════════════════════════════════════════════════
const canvas   = document.getElementById("three-canvas");
const wrap     = document.getElementById("canvas-wrap");
const loadEl   = document.getElementById("loading");
const loadTxt  = document.getElementById("load-txt");
const loadBar  = document.getElementById("load-bar");
const infoBody = document.getElementById("info-body");
const loadStatus = document.getElementById("load-status");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xf0f2f5);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);

// Lighting برای تم روشن
scene.add(new THREE.AmbientLight(0xffffff, 0.7));
const dirL1 = new THREE.DirectionalLight(0xffffff, 0.8);
dirL1.position.set(60, 100, 60);
scene.add(dirL1);
const dirL2 = new THREE.DirectionalLight(0xffffff, 0.3);
dirL2.position.set(-40, 40, -40);
scene.add(dirL2);
scene.add(new THREE.HemisphereLight(0xeef4ff, 0xd4c8a0, 0.4));

// Grid روشن
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

// ── Orbit ─────────────────────────────────────────────────────────
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

// ═══════════════════════════════════════════════════════════════════
// State
// ═══════════════════════════════════════════════════════════════════
const modelMeshes = {};
const modelGroups = {};
const catVisible  = {};
const catCounts   = {};
const catElements = {};
const hiddenIds   = new Set();
const docMeta     = {};

function catKey(docId, type) { return docId + ":" + type; }
function allMeshes() { return Object.values(modelMeshes).flat(); }

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
  if (n > 0) { el.textContent = `${n} مخفی`; el.style.display = "inline"; btn.style.display = "inline"; }
  else { el.style.display = "none"; btn.style.display = "none"; }
}

// ── Category UI (light theme) ──────────────────────────────────────
function buildCategoryUI() {
  const list = document.getElementById("cat-scroll");
  if (!list) return;
  list.innerHTML = "";

  const byDoc = {};
  for (const key of Object.keys(catCounts)) {
    const sep = key.indexOf(":");
    const docId = key.slice(0, sep);
    const type  = key.slice(sep + 1);
    if (!byDoc[docId]) byDoc[docId] = {};
    byDoc[docId][type] = catCounts[key];
  }

  for (const docId of Object.keys(byDoc)) {
    const meta = docMeta[docId] || {};
    const col  = meta.color || "#1E6FBF";
    const lbl  = meta.label || docId.slice(0, 8);

    const fileRow = document.createElement("div");
    fileRow.style.cssText = "padding:6px 10px;background:#F8FAFC;border-bottom:1px solid #E2E8F0;" +
      "display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;flex-shrink:0";
    fileRow.innerHTML =
      `<span class="ftog" style="color:#94A3B8;font-size:10px;width:10px;flex-shrink:0">▼</span>` +
      `<span style="width:9px;height:9px;border-radius:50%;background:${col};flex-shrink:0"></span>` +
      `<span style="font-size:11px;font-weight:600;color:#0D1B2A;flex:1;overflow:hidden;` +
        `text-overflow:ellipsis;white-space:nowrap" title="${esc(lbl)}">${esc(lbl)}</span>`;
    list.appendChild(fileRow);

    const typeList = document.createElement("div");
    for (const type of Object.keys(byDoc[docId]).sort()) {
      const key    = catKey(docId, type);
      const vis    = catVisible[key] !== false;
      const tColHex = "#" + getColor(type).getHexString();
      const count  = byDoc[docId][type];
      const isFlat = FLAT_TYPES.has(type);

      const catRow = document.createElement("div");
      catRow.style.cssText = "display:flex;align-items:center;gap:6px;padding:4px 8px 4px 20px;" +
        "cursor:pointer;user-select:none;border-bottom:1px solid #F1F5F9;" +
        `opacity:${vis ? "1" : ".45"};transition:background 0.1s`;
      catRow.dataset.catKey = key;
      catRow.innerHTML =
        `<input class="cat-cb" type="checkbox" ${vis ? "checked" : ""} data-cat-key="${esc(key)}"
           style="width:12px;height:12px;accent-color:#1E6FBF;flex-shrink:0;cursor:pointer">` +
        `<span style="width:8px;height:8px;border-radius:50%;background:${isFlat ? "#CBD5E1" : tColHex};
           flex-shrink:0"></span>` +
        `<span style="font-size:11px;color:#1E293B;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
           title="${esc(type)}">${esc(type)}</span>` +
        `<span style="font-size:10px;color:#94A3B8;flex-shrink:0">${count}</span>`;
      catRow.addEventListener("mouseenter", () => catRow.style.background = "#F8FAFC");
      catRow.addEventListener("mouseleave", () => catRow.style.background = "");
      typeList.appendChild(catRow);
    }
    list.appendChild(typeList);

    fileRow.addEventListener("click", () => {
      const tog = fileRow.querySelector(".ftog");
      const col = typeList.style.display === "none";
      typeList.style.display = col ? "" : "none";
      tog.textContent = col ? "▼" : "▶";
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

function setCatAll(v) {
  Object.keys(catCounts).forEach(k => catVisible[k] = v);
  buildCategoryUI(); applyVisibility();
}

document.getElementById("btn-cat-all").addEventListener("click",  () => setCatAll(true));
document.getElementById("btn-cat-none").addEventListener("click", () => setCatAll(false));

// ═══════════════════════════════════════════════════════════════════
// web-ifc
// ═══════════════════════════════════════════════════════════════════
let webIfc = null;

async function initWebIfc() {
  const mod = await import("https://esm.sh/web-ifc@0.0.57");
  webIfc = new mod.IfcAPI();
  webIfc.SetWasmPath("https://esm.sh/web-ifc@0.0.57/");
  await webIfc.Init();
}

// ═══════════════════════════════════════════════════════════════════
// بارگذاری مدل
// ═══════════════════════════════════════════════════════════════════
async function loadModel(cfg, index, total) {
  if (loadTxt) loadTxt.textContent = `بارگذاری ${cfg.label} (${index}/${total})…`;
  if (loadBar) loadBar.style.width = `${((index-1)/total)*80}%`;

  const resp = await fetch(cfg.url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} برای ${cfg.label}`);
  const data = new Uint8Array(await resp.arrayBuffer());

  if (loadBar) loadBar.style.width = `${((index-1)/total)*80 + 60/total}%`;

  const modelId = webIfc.OpenModel(data, { COORDINATE_TO_ORIGIN: false, USE_FAST_BOOLS: false });

  const elemIndex = {};
  const allLines = webIfc.GetAllLines(modelId);
  for (let i = 0; i < allLines.size(); i++) {
    const id = allLines.get(i);
    try { const l = webIfc.GetLine(modelId, id, false); if (l) elemIndex[id] = l; } catch(_) {}
  }

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
    const isFlat = FLAT_TYPES.has(tName);
    const tCol = isFlat ? new THREE.Color(0xcccccc) : getColor(tName);
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
      });
    }

    const meta = {
      expressId: expId, ifcType: tName,
      name: sv(line?.Name), globalId: sv(line?.GlobalId),
      objectType: sv(line?.ObjectType), description: sv(line?.Description),
      tag: sv(line?.Tag), docId: docId, modelLabel: cfg.label,
      slotColor: cfg.color, psets: getPsets(expId), isFlat,
    };

    const mat = new THREE.MeshLambertMaterial({
      color: tCol.clone(), transparent: true,
      opacity: isFlat ? 0.3 : 0.88,
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
  if (loadStatus) loadStatus.textContent = `✓ ${cfg.label}: ${vertCount.toLocaleString()} vertices`;
  return vertCount;
}

// ═══════════════════════════════════════════════════════════════════
// Fit / Reset
// ═══════════════════════════════════════════════════════════════════
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

// ═══════════════════════════════════════════════════════════════════
// Info Panel (light theme)
// ═══════════════════════════════════════════════════════════════════
let selectedMesh = null;
let mouseMoved   = false;

function showInfo(m) {
  const d = m.userData;
  const tColHex = "#" + getColor(d.ifcType).getHexString();

  let h = `
<div style="font-size:11px;font-weight:600;color:${d.slotColor};margin-bottom:10px;
  padding-bottom:8px;border-bottom:1px solid #E2E8F0">
  ${esc(d.modelLabel)}
</div>
<div style="margin-bottom:10px">`;

  const fields = [
    ["IFC Type", d.ifcType, tColHex, true],
    ["GlobalId", d.globalId, null, false],
    ["Express ID", String(d.expressId), null, false],
    ["Name", d.name, null, false],
    ["ObjectType", d.objectType, null, false],
    ["Description", d.description, null, false],
  ];

  for (const [label, value, color, showDot] of fields) {
    if (!value) continue;
    h += `
<div style="display:flex;gap:6px;margin-bottom:5px;align-items:flex-start">
  <span style="color:#8896A5;min-width:86px;flex-shrink:0;font-size:11px;padding-top:1px">${esc(label)}</span>
  <span style="color:#0D1B2A;font-size:11px;word-break:break-all;display:flex;align-items:center;gap:4px">
    ${showDot ? `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></span>` : ""}
    ${esc(value)}
  </span>
</div>`;
  }
  h += "</div>";

  const psets = d.psets || {};
  if (Object.keys(psets).length) {
    h += `<div style="border-top:1px solid #E2E8F0;padding-top:10px;margin-top:4px;
      font-size:10px;font-weight:700;color:#64748B;text-transform:uppercase;
      letter-spacing:.5px;margin-bottom:8px">Property Sets</div>`;
    for (const [pn, props] of Object.entries(psets)) {
      h += `<div style="background:#F8FAFC;border:1px solid #E2E8F0;border-radius:6px;
        margin-bottom:6px;overflow:hidden">
        <div style="padding:5px 9px;background:#F1F5F9;font-size:11px;font-weight:600;
          color:#1E40AF;border-bottom:1px solid #E2E8F0">${esc(pn)}</div>`;
      const filteredProps = Object.entries(props).filter(([k]) => k !== "id");
      if (filteredProps.length) {
        h += '<div style="padding:5px 9px">';
        for (const [k, v] of filteredProps) {
          h += `<div style="display:flex;gap:6px;padding:2px 0;border-bottom:1px solid #F1F5F9">
            <span style="color:#8896A5;font-size:10px;min-width:100px;flex-shrink:0">${esc(k)}</span>
            <span style="color:#374151;font-size:10px;word-break:break-all">${esc(String(v))}</span>
          </div>`;
        }
        h += "</div>";
      }
      h += "</div>";
    }
  }

  infoBody.innerHTML = h;
}

// ── Raycasting ────────────────────────────────────────────────────
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
    infoBody.innerHTML = '<div style="color:#8896A5;font-style:italic;text-align:center;padding:24px 0">روی یک عنصر کلیک کنید.</div>';
  }
});

// Space → hide
window.addEventListener("keydown", e => {
  if (e.code !== "Space" || !selectedMesh) return;
  e.preventDefault();
  const id = selectedMesh.userData.expressId;
  hiddenIds.add(id);
  for (const m of allMeshes()) if (m.userData.expressId === id) m.visible = false;
  selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  selectedMesh = null;
  infoBody.innerHTML = '<div style="color:#8896A5;font-style:italic;text-align:center;padding:24px 0">عنصر مخفی شد.</div>';
  updateHiddenCount();
});

// ── Buttons ─────────────────────────────────────────────────────────
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

// ── مدیریت انتخاب مدل از sidebar ──────────────────────────────────
function onDocToggle(docId, checked, color) {
  document.getElementById("btn-apply-select").style.display = "inline";
  const lbl = document.getElementById(`lbl-${docId}`);
  if (lbl) lbl.style.background = checked ? "#EFF6FF" : "transparent";
}

function applyDocSelection() {
  const form = document.getElementById("model-select-form");
  if (!form) return;
  const checked = [...form.querySelectorAll("input[name=doc_ids]:checked")].map(i => i.value);
  if (!checked.length) { alert("حداقل یک مدل انتخاب کنید."); return; }
  const url = new URL(window.location.href);
  url.searchParams.delete("doc_ids");
  checked.forEach(id => url.searchParams.append("doc_ids", id));
  window.location.href = url.toString();
}

document.getElementById("btn-apply-select").addEventListener("click", applyDocSelection);

// ═══════════════════════════════════════════════════════════════════
// GlobalId Search (fixed)
// ═══════════════════════════════════════════════════════════════════
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
      slotColor: m.userData.slotColor,
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
    searchResults.innerHTML = '<div style="padding:10px 12px;font-size:12px;color:#8896A5">نتیجه‌ای نیست</div>';
    searchResults.style.display = "block";
    return;
  }

  let html = `<div style="padding:5px 12px 5px;font-size:10px;color:#1E6FBF;font-weight:600;
    border-bottom:1px solid #E2E8F0">${hits.length} نتیجه</div>`;

  html += hits.map(el => {
    const col = el.slotColor || "#1E6FBF";
    const tColHex = "#" + getColor(el.ifcType).getHexString();
    const gidDisplay = el.globalId.toLowerCase().replace(q,
      `<span style="background:#FEF3C7;color:#92400E;border-radius:2px;padding:0 1px">${esc(q)}</span>`);
    return `<div class="s-row" data-gid="${esc(el.globalId)}"
      style="padding:7px 12px;cursor:pointer;border-bottom:1px solid #F1F5F9">
      <div style="display:flex;align-items:center;gap:5px;margin-bottom:2px">
        <span style="width:7px;height:7px;border-radius:50%;background:${tColHex};flex-shrink:0"></span>
        <span style="font-size:12px;color:#0D1B2A;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${esc(el.name || el.ifcType)}</span>
        <span style="font-size:10px;color:${col};flex-shrink:0">${esc(el.ifcType)}</span>
      </div>
      <div style="font-size:10px;color:#64748B;font-family:monospace;padding-left:12px">${gidDisplay}</div>
    </div>`;
  }).join("");

  searchResults.innerHTML = html;
  searchResults.style.display = "block";

  searchResults.querySelectorAll(".s-row").forEach(row => {
    row.addEventListener("mouseenter", () => row.style.background = "#F8FAFC");
    row.addEventListener("mouseleave", () => row.style.background = "");
    row.addEventListener("click", () => {
      const gid = row.dataset.gid;
      // Find mesh by globalId
      const mesh = allMeshes().find(m => m.userData.globalId === gid);
      if (!mesh) {
        // Try case-insensitive
        const meshAlt = allMeshes().find(m =>
          m.userData.globalId && m.userData.globalId.toLowerCase() === gid.toLowerCase()
        );
        if (!meshAlt) return;
        selectAndFocus(meshAlt);
      } else {
        selectAndFocus(mesh);
      }
      searchResults.style.display = "none";
    });
  });
}

function selectAndFocus(mesh) {
  if (selectedMesh && selectedMesh !== mesh)
    selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  if (!mesh.userData._origColor) mesh.userData._origColor = mesh.material.color.clone();
  mesh.material.color.copy(HIGHLIGHT);
  selectedMesh = mesh;
  showInfo(mesh);
  const panel = document.getElementById("info-panel");
  panel.style.width = "300px"; panel.style.minWidth = "300px";
  const box = new THREE.Box3().setFromObject(mesh);
  if (!box.isEmpty()) {
    orb.tgt.copy(box.getCenter(new THREE.Vector3()));
    orb.sph.radius = Math.max(box.getSize(new THREE.Vector3()).length() * 2.5, 2);
    applyOrb();
  }
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

// ── Render loop ────────────────────────────────────────────────────
(function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); })();

// ═══════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════
function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;")
    .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

(async () => {
  if (!MODEL_URLS.length) {
    loadEl.style.display = "none";
    if (loadStatus) loadStatus.textContent = "مدلی انتخاب نشده.";
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
        console.error("خطا:", MODEL_URLS[i].label, err);
        if (loadStatus) loadStatus.textContent = `⚠ ${err.message}`;
      }
    }
    if (loadBar) loadBar.style.width = "100%";
    buildCategoryUI();
    buildSearchIndex();
    fitAll();
    if (loadStatus) loadStatus.textContent = `✓ ${MODEL_URLS.length} مدل · ${total.toLocaleString()} vertices`;
  } catch(err) {
    if (loadTxt) loadTxt.textContent = "خطا: " + err.message;
    console.error(err);
  } finally {
    if (loadEl) loadEl.style.display = "none";
  }
})();
"""
