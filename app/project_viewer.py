"""
project_viewer.py – BIMPruef Direct Viewer

ماژول مستقل Viewer که مستقیم از Documents/R2 می‌خونه.
هیچ وابستگی‌ای به session/slot قدیمی نداره.

روت‌ها:
  GET  /projects/{project_id}/view               → صفحه اصلی viewer
  GET  /projects/{project_id}/view/file/{doc_id} → stream مستقیم IFC از R2
  GET  /projects/{project_id}/view/select        → انتخاب مدل‌ها
  POST /projects/{project_id}/view/select        → تأیید انتخاب و redirect به view
"""

from __future__ import annotations

import html
import json
import os
import tempfile
from typing import Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse

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
    "#4fc3f7", "#ef9a9a", "#a5d6a7", "#ffcc80",
    "#ce93d8", "#80cbc4", "#f48fb1", "#ffab40",
]

def _slot_color(index: int) -> str:
    return SLOT_COLORS[index % len(SLOT_COLORS)]


# ─────────────────────────────────────────────────────────────────────────────
# File streaming endpoint  (IFC مستقیم از R2)
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/view/file/{document_id}")
def view_file_stream(request: Request, project_id: str, document_id: str):
    """
    IFC فایل رو مستقیم از R2 stream می‌کنه به browser.
    هیچ نیازی به session/slot نیست.
    فایل موقت در /tmp نگه داشته میشه و بعد پاک میشه.
    """
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

    # دانلود موقت به /tmp
    suffix = doc.get("file_extension", ".ifc")
    fd, tmp_path = tempfile.mkstemp(prefix="bpview-", suffix=suffix)
    os.close(fd)

    try:
        download_file_from_r2(doc["r2_key"], tmp_path)

        # اگه ifczip بود، اول extract کن
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
    """IFC یا IFCZIP رو می‌خونه و bytes خالص IFC برمی‌گردونه."""
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
# Select page
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/view/select", response_class=HTMLResponse)
def view_select_page(
    request: Request,
    project_id: str,
    error: str = Query(default=""),
):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    docs = list_project_ifc_documents(account["account_id"], project_id)
    pid = _e(project_id)

    flash = f'<div class="flash-err" style="margin-bottom:16px">⚠ {_e(error)}</div>' if error else ""

    if not docs:
        content = f"""
        {flash}
        <div class="card" style="border-color:var(--accent2);text-align:center;padding:40px">
          <h3 style="font-size:18px;margin-bottom:10px">کوئی IFC فایل موجود نیست</h3>
          <p style="color:var(--muted);margin-bottom:20px;font-size:13px">
            اول در Documents ماژول فایل IFC آپلود کن.
          </p>
          <a class="btn btn-primary" href="/projects/{pid}/documents" style="text-decoration:none">
            برو به Documents
          </a>
        </div>
        """
    else:
        rows = ""
        for i, d in enumerate(docs):
            col = _slot_color(i)
            rows += f"""
            <label style="display:flex;align-items:center;gap:12px;padding:12px 16px;
              border:1px solid var(--border);border-radius:8px;cursor:pointer;
              background:var(--surface2);transition:border-color .15s"
              onmouseenter="this.style.borderColor='{col}'"
              onmouseleave="this.style.borderColor='var(--border)'">
              <input type="checkbox" name="doc_ids" value="{_e(d['document_id'])}" checked
                style="width:15px;height:15px;accent-color:{col};flex-shrink:0">
              <span style="width:10px;height:10px;border-radius:50%;background:{col};
                flex-shrink:0;display:inline-block"></span>
              <div style="flex:1;min-width:0">
                <div style="font-weight:600;font-size:13px;overflow:hidden;
                  text-overflow:ellipsis;white-space:nowrap">{_e(d['original_filename'])}</div>
                <div style="font-size:11px;color:var(--muted)">{_e(d.get('folder_path') or 'Root')} · {_fmt_size(d.get('file_size',0))}</div>
              </div>
            </label>"""

        content = f"""
        {flash}
        <div class="card" style="max-width:640px">
          <h3 style="font-size:16px;margin-bottom:4px">مدل‌ها رو انتخاب کن</h3>
          <p style="color:var(--muted);font-size:12px;margin-bottom:18px">
            فایل‌های IFC انتخاب‌شده مستقیم از Documents بارگذاری میشن.
          </p>
          <form method="GET" action="/projects/{pid}/view">
            <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:20px">
              {rows}
            </div>
            <div style="display:flex;gap:10px">
              <button type="submit" class="btn btn-primary" style="flex:1;justify-content:center">
                ▶ باز کردن Viewer
              </button>
              <a href="/projects/{pid}" class="btn" style="text-decoration:none">← پروژه</a>
            </div>
          </form>
        </div>
        """

    from app.projects import _page, _project_subnav, _topbar_global

    body = f"""
    {_topbar_global(account)}
    {_project_subnav(project_id, "model")}
    <div style="padding:28px 32px;max-width:1100px;margin:0 auto">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
        <h1 style="font-size:22px;font-weight:600">Direct Viewer – انتخاب مدل</h1>
      </div>
      {content}
    </div>
    """
    return _page(f"{project['project_name']} – Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# Main viewer page
# ─────────────────────────────────────────────────────────────────────────────

@project_viewer_router.get("/projects/{project_id}/view", response_class=HTMLResponse)
def view_main(
    request: Request,
    project_id: str,
    doc_ids: list[str] = Query(default=[]),
    error: str = Query(default=""),
):
    """
    صفحه اصلی viewer.
    doc_ids از query string میاد: ?doc_ids=abc&doc_ids=def
    اگه doc_ids نباشه redirect به select می‌کنه.
    """
    account = _account_from_request(request)
    account_id = account["account_id"]
    project = get_project(account_id, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    # اگه doc_ids نبود برو select
    all_ifc_docs = list_project_ifc_documents(account_id, project_id)
    if not doc_ids:
        # همه رو نمایش بده
        doc_ids = [d["document_id"] for d in all_ifc_docs]
        if not doc_ids:
            return RedirectResponse(
                f"/projects/{_e(project_id)}/view/select", status_code=302
            )

    # فقط doc_idهایی که واقعاً وجود دارن
    valid_docs = {d["document_id"]: d for d in all_ifc_docs}
    selected_docs = [valid_docs[did] for did in doc_ids if did in valid_docs]

    if not selected_docs:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/view/select?error=No+valid+documents+selected",
            status_code=302,
        )

    # ساختن آرایه JS برای model URLs
    pid = _e(project_id)
    model_entries = []
    for i, doc in enumerate(selected_docs):
        color = _slot_color(i)
        url = f"/projects/{pid}/view/file/{_e(doc['document_id'])}"
        label = doc["original_filename"]
        model_entries.append({
            "url": url,
            "label": label,
            "color": color,
            "document_id": doc["document_id"],
        })

    model_urls_js = ",\n".join(
        f'{{url:{json.dumps(m["url"])},label:{json.dumps(m["label"])},color:{json.dumps(m["color"])},documentId:{json.dumps(m["document_id"])}}}'
        for m in model_entries
    )

    # Select panel HTML برای تغییر مدل‌ها
    select_rows = ""
    for i, doc in enumerate(all_ifc_docs):
        checked = "checked" if doc["document_id"] in [d["document_id"] for d in selected_docs] else ""
        col = _slot_color(i)
        select_rows += f"""
        <label style="display:flex;align-items:center;gap:8px;padding:6px 8px;
          border-radius:5px;cursor:pointer;font-size:11px;
          background:{'rgba(79,195,247,.08)' if checked else 'transparent'}"
          id="lbl-{_e(doc['document_id'])}">
          <input type="checkbox" name="doc_ids" value="{_e(doc['document_id'])}" {checked}
            style="width:12px;height:12px;accent-color:{col};flex-shrink:0"
            onchange="toggleDocLabel('{_e(doc['document_id'])}',this.checked)">
          <span style="width:8px;height:8px;border-radius:50%;background:{col};flex-shrink:0"></span>
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1"
            title="{_e(doc['original_filename'])}">{_e(doc['original_filename'])}</span>
          <span style="color:var(--muted);flex-shrink:0">{_fmt_size(doc.get('file_size',0))}</span>
        </label>"""

    from app.projects import _page, _project_subnav, _topbar_global

    error_html = f'<div style="position:absolute;top:56px;left:0;right:0;z-index:30;' \
                 f'padding:8px 16px;background:#2a0a10;color:#ffaaaa;font-size:12px">' \
                 f'⚠ {_e(error)}</div>' if error else ""

    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "model")}

<div style="display:flex;flex-direction:column;height:calc(100vh - 94px);overflow:hidden;position:relative">
  {error_html}

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- ─── Sidebar ──────────────────────────────────────────────────────── -->
    <div id="sidebar" style="width:240px;min-width:240px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0;transition:width .2s ease">

      <!-- مدل‌های بارگذاری‌شده -->
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <span>📁 مدل‌ها</span>
        <button onclick="reloadWithSelection()" title="اعمال تغییرات"
          style="font-size:10px;padding:2px 7px;background:#0a2a40;border:1px solid #1e4a6e;
          color:#4fc3f7;border-radius:3px;cursor:pointer;display:none" id="btn-apply-select">
          ✓ اعمال
        </button>
      </div>

      <div style="padding:6px 8px;border-bottom:1px solid var(--border);flex-shrink:0;
        max-height:180px;overflow-y:auto">
        <form id="model-select-form" method="GET" action="/projects/{pid}/view">
          {select_rows}
        </form>
      </div>

      <!-- IFC ساختار -->
      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;flex-shrink:0;
        display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)">
        <span>🏗 IFC ساختار</span>
        <span style="display:flex;gap:4px">
          <button id="btn-cat-all" style="font-size:10px;cursor:pointer;color:#6af;
            background:none;border:none;padding:0">همه</button>
          &nbsp;
          <button id="btn-cat-none" style="font-size:10px;cursor:pointer;color:#6af;
            background:none;border:none;padding:0">هیچ</button>
        </span>
      </div>
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0">
        <div style="padding:10px;font-size:11px;color:var(--muted);font-style:italic">
          در حال بارگذاری…
        </div>
      </div>

      <!-- وضعیت بارگذاری -->
      <div id="load-status" style="padding:6px 10px;font-size:10px;color:var(--muted);
        border-top:1px solid var(--border);flex-shrink:0"></div>
    </div>

    <!-- ─── Canvas ──────────────────────────────────────────────────────── -->
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden;background:#0e0e1a">

      <canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>

      <!-- سرچ GlobalId -->
      <div id="search-bar" style="position:absolute;top:10px;left:10px;z-index:10;width:300px">
        <div style="display:flex;gap:4px">
          <input id="gid-search" type="text" placeholder="🔍 جستجوی GlobalId…"
            style="flex:1;background:rgba(14,20,36,.93);border:1px solid var(--border);
            color:var(--text);padding:6px 10px;border-radius:6px;font-size:12px;
            outline:none;backdrop-filter:blur(6px)"
            autocomplete="off">
          <button id="search-clear" style="display:none;background:rgba(14,20,36,.93);
            border:1px solid var(--border);color:var(--muted);border-radius:6px;
            padding:5px 8px;cursor:pointer;font-size:12px">✕</button>
        </div>
        <div id="search-results" style="display:none;margin-top:4px;
          background:rgba(14,20,36,.97);border:1px solid var(--border);
          border-radius:6px;max-height:240px;overflow-y:auto;backdrop-filter:blur(6px)"></div>
      </div>

      <!-- دکمه‌های Overlay -->
      <div style="position:absolute;top:10px;right:10px;display:flex;gap:4px;z-index:6">
        <button id="btn-fit"   class="btn" style="font-size:11px;padding:4px 9px">⊡ Fit</button>
        <button id="btn-reset" class="btn" style="font-size:11px;padding:4px 9px">⟳ Camera</button>
        <button id="btn-show-all" class="btn btn-danger"
          style="font-size:11px;padding:4px 9px;display:none">👁 همه</button>
        <span id="hidden-count" style="font-size:11px;color:var(--accent2);display:none;align-self:center"></span>
      </div>

      <div style="position:absolute;bottom:10px;right:10px;font-size:10px;color:#445;
        pointer-events:none">LMB چرخش · MMB Pan · Scroll زوم · Space: مخفی</div>

      <!-- Loading overlay -->
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:rgba(14,14,26,.95);z-index:20">
        <div style="width:44px;height:44px;border:4px solid #0f3460;
          border-top-color:var(--accent2);border-radius:50%;
          animation:spin .7s linear infinite;margin-bottom:14px"></div>
        <p id="load-txt" style="color:#889;font-size:13px;margin:0">
          اتصال به R2…
        </p>
        <div id="load-progress" style="margin-top:10px;width:200px;height:3px;
          background:#1a2a4a;border-radius:2px;overflow:hidden">
          <div id="load-bar" style="width:0%;height:100%;background:var(--accent);
            transition:width .3s ease;border-radius:2px"></div>
        </div>
      </div>
    </div>

    <!-- ─── Info Panel ──────────────────────────────────────────────────── -->
    <div id="info-panel" style="width:300px;min-width:300px;background:var(--surface);
      border-left:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0;transition:width .2s ease">
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <span>Element Info</span>
        <span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px;
          padding:0 4px" title="بستن">✕</span>
      </div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:12px;font-size:12px">
        <div style="color:var(--muted);font-style:italic;text-align:center;padding:20px 0">
          روی یه المان کلیک کن.
        </div>
      </div>
    </div>

  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_direct_viewer_js(model_urls_js)}
</script>
"""
    return _page(f"{project['project_name']} – Direct Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# Three.js JavaScript  (مستقل از viewer قدیمی)
# ─────────────────────────────────────────────────────────────────────────────

def _direct_viewer_js(model_urls_js: str) -> str:
    return f"const MODEL_URLS = [{model_urls_js}];\n" + r"""

// ═══════════════════════════════════════════════════════════════════
// رنگ‌بندی IFC
// ═══════════════════════════════════════════════════════════════════
const TYPE_COLOR = {
  IfcWall:0xc8a057, IfcWallStandardCase:0xc8a057, IfcCurtainWall:0xf0c040,
  IfcColumn:0x2e6b9e, IfcColumnStandardCase:0x2e6b9e,
  IfcBeam:0x5b9bd5, IfcBeamStandardCase:0x5b9bd5,
  IfcSlab:0x7aaec8, IfcSlabStandardCase:0x7aaec8,
  IfcRoof:0x8b5de5, IfcDoor:0xe07040, IfcWindow:0x70d8f0,
  IfcStair:0xc87050, IfcStairFlight:0xc87050,
  IfcRamp:0xd4a030, IfcRailing:0x607080,
  IfcPlate:0x90b8d0, IfcMember:0x4898b0, IfcCovering:0x88b878,
  IfcFooting:0x2a5070, IfcPile:0x1e3850,
  IfcBuildingElementProxy:0x888888,
  IfcFurnishingElement:0xe08860, IfcFurniture:0xe08860,
  IfcSpace:0xa8d8a8, IfcOpeningElement:0x444466,
  IfcPipeSegment:0x188880, IfcDuctSegment:0x607080,
};
const TYPE_FALLBACK = [
  [/^IfcWall/,0xc8a057],[/^IfcSlab/,0x7aaec8],[/^IfcColumn/,0x2e6b9e],
  [/^IfcBeam/,0x5b9bd5],[/^IfcStair/,0xc87050],[/^IfcRoof/,0x8b5de5],
  [/^IfcDoor/,0xe07040],[/^IfcWindow/,0x70d8f0],[/^IfcPipe/,0x188880],
  [/^IfcFurnish/,0xe08860],[/^IfcElectric/,0xe0b830],
];
const FLAT_TYPES = new Set(["IfcOpeningElement","IfcAnnotation","IfcGrid","IfcSpace"]);

function getColor(t) {
  if (TYPE_COLOR[t] !== undefined) return new THREE.Color(TYPE_COLOR[t]);
  for (const [rx, hex] of TYPE_FALLBACK) if (rx.test(t)) return new THREE.Color(hex);
  return new THREE.Color(0x777788);
}

// ═══════════════════════════════════════════════════════════════════
// Three.js Setup
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
scene.background = new THREE.Color(0x0e0e1a);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);

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
const modelMeshes = {};   // docId → Mesh[]
const modelGroups = {};   // docId → Group
const catVisible  = {};   // "docId:type" → bool
const catCounts   = {};
const catElements = {};
const hiddenIds   = new Set();
const docMeta     = {};   // docId → {label, color}

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

// ── Category UI ───────────────────────────────────────────────────
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
    const col  = meta.color || "#4fc3f7";
    const lbl  = meta.label || docId.slice(0, 8);

    const fileRow = document.createElement("div");
    fileRow.style.cssText = "padding:4px 8px;background:#0d1f38;border-bottom:1px solid #1a2e50;" +
      "display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;flex-shrink:0";
    fileRow.innerHTML =
      `<span class="ftog" style="color:#6af;font-size:10px;width:10px;flex-shrink:0">▼</span>` +
      `<span style="width:8px;height:8px;border-radius:50%;border:2px solid ${col};flex-shrink:0"></span>` +
      `<span style="font-size:11px;font-weight:600;color:${col};flex:1;overflow:hidden;` +
        `text-overflow:ellipsis;white-space:nowrap" title="${esc(lbl)}">${esc(lbl)}</span>`;
    list.appendChild(fileRow);

    const typeList = document.createElement("div");
    for (const type of Object.keys(byDoc[docId]).sort()) {
      const key   = catKey(docId, type);
      const vis   = catVisible[key] !== false;
      const tCol  = "#" + getColor(type).getHexString();
      const count = byDoc[docId][type];
      const isFlat = FLAT_TYPES.has(type);

      const catRow = document.createElement("div");
      catRow.style.cssText = "display:flex;align-items:center;gap:5px;padding:3px 6px 3px 18px;" +
        "cursor:pointer;user-select:none;border-bottom:1px solid #111e35;" +
        `opacity:${vis ? "1" : ".45"}`;
      catRow.dataset.catKey = key;
      catRow.innerHTML =
        `<input class="cat-cb" type="checkbox" ${vis ? "checked" : ""} data-cat-key="${esc(key)}"
           style="width:12px;height:12px;accent-color:#4af;flex-shrink:0">` +
        `<span style="width:8px;height:8px;border-radius:50%;background:${isFlat ? "#222" : tCol};
           flex-shrink:0"></span>` +
        `<span style="font-size:11px;color:#bcd;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
           title="${esc(type)}">${esc(type)}</span>` +
        `<span style="font-size:10px;color:#556;flex-shrink:0">${count}</span>`;
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
// بارگذاری مدل  (مستقیم از URL بدون session)
// ═══════════════════════════════════════════════════════════════════
async function loadModel(cfg, index, total) {
  if (loadTxt) loadTxt.textContent = `بارگذاری ${cfg.label} (${index}/${total})…`;
  if (loadBar) loadBar.style.width = `${((index-1)/total)*100}%`;

  const resp = await fetch(cfg.url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} برای ${cfg.label}`);
  const data = new Uint8Array(await resp.arrayBuffer());

  if (loadBar) loadBar.style.width = `${(index/total)*90}%`;

  const modelId = webIfc.OpenModel(data, { COORDINATE_TO_ORIGIN: false, USE_FAST_BOOLS: false });

  // Index همه خطوط
  const elemIndex = {};
  const allLines = webIfc.GetAllLines(modelId);
  for (let i = 0; i < allLines.size(); i++) {
    const id = allLines.get(i);
    try { const l = webIfc.GetLine(modelId, id, false); if (l) elemIndex[id] = l; } catch(_) {}
  }

  // TypeCode resolver
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
    _tnc[code] = name;
    return name;
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

  // PSets map
  const relMap = {};
  for (const [id, line] of Object.entries(elemIndex)) {
    if (!typeName(line).toLowerCase().includes("reldefinesbyprop")) continue;
    const pref = line.RelatingPropertyDefinition;
    if (!pref) continue;
    const pid = pref.value ?? pref;
    const rels = line.RelatedObjects;
    if (!rels) continue;
    const ids = Array.isArray(rels) ? rels : [rels];
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
      const pn = sv(pset.Name) || typeName(pset);
      const props = {};
      const hp = pset.HasProperties;
      if (hp) {
        const list = Array.isArray(hp) ? hp : [hp];
        for (const ref of list) {
          const id = ref?.value ?? ref;
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
    const tCol = isFlat ? new THREE.Color(0x222233) : getColor(tName);
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
      expressId: expId,
      ifcType: tName,
      name: sv(line?.Name),
      globalId: sv(line?.GlobalId),
      objectType: sv(line?.ObjectType),
      description: sv(line?.Description),
      tag: sv(line?.Tag),
      docId: docId,
      modelLabel: cfg.label,
      slotColor: cfg.color,
      psets: getPsets(expId),
      isFlat,
    };

    const mat = new THREE.MeshLambertMaterial({
      color: tCol.clone(),
      transparent: true,
      opacity: isFlat ? 0.5 : 0.90,
      wireframe: isFlat,
      side: THREE.DoubleSide,
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
// Info Panel
// ═══════════════════════════════════════════════════════════════════
let selectedMesh = null;
let mouseMoved   = false;

function showInfo(m) {
  const d = m.userData;
  const tc = "#" + getColor(d.ifcType).getHexString();

  let h = `
<div style="font-size:11px;font-weight:bold;color:${d.slotColor};margin-bottom:10px">
  ${esc(d.modelLabel)}
</div>
<div style="margin-bottom:10px">`;

  const fields = [
    ["IFC Type", d.ifcType, tc, true],
    ["GlobalId", d.globalId, null, false],
    ["Express ID", String(d.expressId), null, false],
    ["Name", d.name, null, false],
    ["ObjectType", d.objectType, null, false],
    ["Description", d.description, null, false],
  ];

  for (const [label, value, color, showDot] of fields) {
    if (!value) continue;
    h += `
<div style="display:flex;gap:6px;margin-bottom:4px;align-items:flex-start">
  <span style="color:#667;min-width:90px;flex-shrink:0;font-size:11px">${esc(label)}</span>
  <span style="color:#cce;font-size:11px;word-break:break-all;display:flex;align-items:center;gap:4px">
    ${showDot ? `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></span>` : ""}
    ${esc(value)}
  </span>
</div>`;
  }
  h += "</div>";

  // PSets
  const psets = d.psets || {};
  if (Object.keys(psets).length) {
    h += `<div style="border-top:1px solid #0f3460;padding-top:8px;margin-top:4px;font-size:10px;
      font-weight:700;color:#6af;text-transform:uppercase;letter-spacing:.4px;margin-bottom:6px">
      Property Sets</div>`;
    for (const [pn, props] of Object.entries(psets)) {
      h += `<div style="background:#0c1a30;border:1px solid #1a2e50;border-radius:5px;
        margin-bottom:5px;overflow:hidden">
        <div style="padding:4px 8px;background:#0a1a2a;font-size:11px;font-weight:600;
          color:#4fc3f7;border-bottom:1px solid #1a2e50">${esc(pn)}</div>`;
      const filteredProps = Object.entries(props).filter(([k]) => k !== "id");
      if (filteredProps.length) {
        h += '<div style="padding:4px 8px">';
        for (const [k, v] of filteredProps) {
          h += `<div style="display:flex;gap:6px;padding:2px 0;border-bottom:1px solid #0f1e30">
            <span style="color:#556;font-size:10px;min-width:100px;flex-shrink:0">${esc(k)}</span>
            <span style="color:#9bc;font-size:10px;word-break:break-all">${esc(String(v))}</span>
          </div>`;
        }
        h += "</div>";
      }
      h += "</div>";
    }
  }

  infoBody.innerHTML = h;
}

// Raycasting
const raycaster = new THREE.Raycaster();
const mouse     = new THREE.Vector2();
const HIGHLIGHT = new THREE.Color(0xffff00);

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
    // پنل باز
    const panel = document.getElementById("info-panel");
    panel.style.width = "300px";
    panel.style.minWidth = "300px";
  } else {
    if (selectedMesh) {
      selectedMesh.material.color.copy(selectedMesh.userData._origColor);
      selectedMesh = null;
    }
    infoBody.innerHTML = '<div style="color:var(--muted);font-style:italic;text-align:center;padding:20px 0">روی یه المان کلیک کن.</div>';
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
  infoBody.innerHTML = '<div style="color:var(--muted);font-style:italic;text-align:center;padding:20px 0">المان مخفی شد.</div>';
  updateHiddenCount();
});

// ─── Buttons ─────────────────────────────────────────────────────
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

// ─── Model checkboxes sidebar ─────────────────────────────────────
function toggleDocLabel(docId, checked) {
  document.getElementById("btn-apply-select").style.display = "inline";
  const lbl = document.getElementById(`lbl-${docId}`);
  if (lbl) lbl.style.background = checked ? "rgba(79,195,247,.08)" : "transparent";
}

function reloadWithSelection() {
  const form = document.getElementById("model-select-form");
  if (!form) return;
  const checked = [...form.querySelectorAll("input[name=doc_ids]:checked")].map(i => i.value);
  if (!checked.length) { alert("حداقل یه مدل انتخاب کن."); return; }
  const url = new URL(window.location.href);
  url.searchParams.delete("doc_ids");
  checked.forEach(id => url.searchParams.append("doc_ids", id));
  window.location.href = url.toString();
}

document.getElementById("btn-apply-select").addEventListener("click", reloadWithSelection);

// ─── Global Id Search ─────────────────────────────────────────────
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
  if (!q) { searchResults.style.display="none"; searchClear.style.display="none"; return; }
  searchClear.style.display = "inline";
  const hits = searchIndex.filter(e => e.globalId.toLowerCase().includes(q)).slice(0, 40);
  if (!hits.length) {
    searchResults.innerHTML = '<div style="padding:8px 12px;font-size:11px;color:var(--muted)">نتیجه‌ای نیست</div>';
    searchResults.style.display = "block"; return;
  }
  let html = hits.map(el => {
    const col = el.slotColor || "#4fc3f7";
    const tCol = "#" + getColor(el.ifcType).getHexString();
    return `<div class="s-row" data-gid="${esc(el.globalId)}"
      style="padding:5px 10px;cursor:pointer;border-bottom:1px solid #0e1a2e;
      display:flex;flex-direction:column;gap:1px">
      <div style="display:flex;align-items:center;gap:5px">
        <span style="width:7px;height:7px;border-radius:50%;background:${tCol};flex-shrink:0"></span>
        <span style="font-size:11px;color:#cce;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          ${esc(el.name || el.ifcType)}</span>
        <span style="font-size:10px;color:${col};flex-shrink:0">${esc(el.ifcType)}</span>
      </div>
      <div style="font-size:10px;color:#4a7090;font-family:monospace;padding-left:12px">
        ${esc(el.globalId)}</div>
    </div>`;
  }).join("");
  searchResults.innerHTML = `<div style="padding:4px 12px;font-size:10px;color:#6af;
    border-bottom:1px solid #1a2e50">${hits.length} نتیجه</div>` + html;
  searchResults.style.display = "block";
  searchResults.querySelectorAll(".s-row").forEach(row => {
    row.addEventListener("mouseenter", () => row.style.background = "#162a48");
    row.addEventListener("mouseleave", () => row.style.background = "");
    row.addEventListener("click", () => {
      const gid = row.dataset.gid;
      const mesh = allMeshes().find(m => m.userData.globalId === gid);
      if (!mesh) return;
      if (selectedMesh && selectedMesh !== mesh)
        selectedMesh.material.color.copy(selectedMesh.userData._origColor);
      if (!mesh.userData._origColor) mesh.userData._origColor = mesh.material.color.clone();
      mesh.material.color.copy(HIGHLIGHT);
      selectedMesh = mesh;
      showInfo(mesh);
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
searchClear.addEventListener("click", () => { searchInput.value = ""; renderSearch(""); searchInput.focus(); });
document.addEventListener("mousedown", e => {
  const bar = document.getElementById("search-bar");
  if (bar && !bar.contains(e.target)) searchResults.style.display = "none";
});

// ─── Render loop ──────────────────────────────────────────────────
(function animate() { requestAnimationFrame(animate); renderer.render(scene, camera); })();

// ═══════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════
function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

(async () => {
  if (!MODEL_URLS.length) {
    loadEl.style.display = "none";
    if (loadStatus) loadStatus.textContent = "کوئی مدل انتخاب نشده.";
    return;
  }
  try {
    await initWebIfc();
    let total = 0;
    for (let i = 0; i < MODEL_URLS.length; i++) {
      const cfg = MODEL_URLS[i];
      try {
        const v = await loadModel(cfg, i + 1, MODEL_URLS.length);
        total += v;
      } catch(err) {
        console.error("خطا در بارگذاری:", cfg.label, err);
        if (loadStatus) loadStatus.textContent = `⚠ ${err.message}`;
      }
    }
    buildCategoryUI();
    buildSearchIndex();
    fitAll();
    if (loadBar) loadBar.style.width = "100%";
    if (loadStatus) loadStatus.textContent = `✓ ${MODEL_URLS.length} مدل · ${total.toLocaleString()} vertices`;
  } catch(err) {
    if (loadTxt) loadTxt.textContent = "خطا: " + err.message;
    console.error(err);
  } finally {
    if (loadEl) loadEl.style.display = "none";
  }
})();
"""
