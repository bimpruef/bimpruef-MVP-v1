"""
main.py – BIMPruef FastAPI-Applikation

Primäre Struktur (projects_router):
  /                              → Account-/Projektübersicht
  /projects/new                  → Neues Projekt anlegen
  /projects/create               → Projekt erstellen (POST)
  /projects/{id}                 → Projekt-Dashboard
  /projects/{id}/model           → Integrierter 3D-Viewer
  /projects/{id}/model/upload    → IFC hochladen
  /projects/{id}/model/remove    → Slot entfernen
  /projects/{id}/model/clash     → Clash-Analyse
  /projects/{id}/model/list      → Elementliste
  /projects/{id}/model/rulecheck → Rule-Check

Technische API-Endpunkte:
  /viewer/file/                  → IFC-Datei ausliefern
  /viewer/ai-chat/               → KI-Assistent
  /viewer/list/data/             → JSON-Daten-API Elementliste
  /viewer/rulecheck/run/         → Rule-Check ausführen

Legacy-Routen:
  /upload-session/
  /session/{id}
  /objects/
  /compare-elements/
  /compare-clashes/
  /download-clashes-bcf/

Debug:
  /debug/r2-test                 → Temporärer R2 upload/download test
"""

import html
import json
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app.auth import auth_router, get_current_user_optional
from app.bcf_export import create_bcf_zip_from_clashes
from app.clash import compare_models_for_clashes
from app.compare import compare_models
from app.extractors import (
    build_object_rows,
    filter_objects,
    get_objects_from_model,
)
from app.ifc_loader import load_ifc_models_from_session
from app.legal_modules import render_datenschutz_module, render_impressum_module
from app.list_module import list_router
from app.projects import projects_router
from app.r2_storage import download_file_from_r2, r2_enabled, upload_file_to_r2
from app.rulecheck import rulecheck_router
from app.storage import (
    cleanup_old_sessions,
    create_upload_session,
    delete_session,
    load_clash_cache,
    save_clash_cache,
)
from app.viewer import router as viewer_router


# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────

BCF_CLASH_LIMIT = int(os.environ.get("BCF_CLASH_LIMIT", "500"))
MAX_FILE_SIZE_MB = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# App-Lifespan
# ─────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    cleanup_old_sessions()
    yield


app = FastAPI(
    title="BIMPruef – IFC Comparison Platform",
    lifespan=lifespan,
)


# ─────────────────────────────────────────────────────────────────────────────
# Middleware – Authentifizierung
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    public_prefixes = (
        "/auth",
        "/impressum",
        "/datenschutz",
        "/favicon.ico",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/debug/r2-test",
    )

    path = request.url.path

    if not path.startswith(public_prefixes):
        user = get_current_user_optional(request)
        if not user:
            return RedirectResponse("/auth/login", status_code=302)

    return await call_next(request)


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────

app.include_router(auth_router)

# Reihenfolge wichtig:
# projects_router definiert "/" und muss vor viewer_router liegen.
app.include_router(projects_router)
app.include_router(viewer_router)
app.include_router(list_router)
app.include_router(rulecheck_router)


# ─────────────────────────────────────────────────────────────────────────────
# Debug – Cloudflare R2 Verbindung testen
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/debug/r2-test")
def r2_test():
    """
    Temporärer Cloudflare-R2-Test.

    Ablauf:
      1. Kleine Textdatei nach /tmp schreiben
      2. Datei nach Cloudflare R2 hochladen
      3. Datei wieder aus R2 herunterladen
      4. Inhalt als PlainTextResponse zurückgeben

    Nach erfolgreichem Test kann diese Route wieder entfernt werden.
    """
    if not r2_enabled():
        return PlainTextResponse(
            "R2 is not configured.",
            status_code=500,
        )

    test_path = "/tmp/bimpruef-r2-test.txt"

    with open(test_path, "w", encoding="utf-8") as f:
        f.write("BIMPruef R2 test OK")

    storage_key = "debug/bimpruef-r2-test.txt"

    upload_file_to_r2(
        local_path=test_path,
        storage_key=storage_key,
        content_type="text/plain",
    )

    download_path = "/tmp/bimpruef-r2-test-downloaded.txt"

    download_file_from_r2(
        storage_key=storage_key,
        local_path=download_path,
    )

    with open(download_path, "r", encoding="utf-8") as f:
        content = f.read()

    return PlainTextResponse(f"R2 upload/download OK: {content}")


# ─────────────────────────────────────────────────────────────────────────────
# HTML-Hilfsfunktionen – Legacy
# ─────────────────────────────────────────────────────────────────────────────

def _base_styles():
    return """<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f6f8fb;
  --bg-soft:#eef3f8;
  --surface:#ffffff;
  --surface2:#f8fafc;
  --surface3:#eef4f9;
  --border:#d8e2ec;
  --border-strong:#b9c8d8;
  --accent:#2563eb;
  --accent-hover:#1d4ed8;
  --accent-soft:#e8f0ff;
  --accent2:#dc2626;
  --danger:#dc2626;
  --danger-soft:#fef2f2;
  --success:#16a34a;
  --success-soft:#ecfdf3;
  --warn:#b7791f;
  --warn-soft:#fff7e6;
  --info:#0f6fae;
  --info-soft:#e8f5ff;
  --text:#172033;
  --text-strong:#0f172a;
  --muted:#65758b;
  --muted2:#94a3b8;
  --shadow-sm:0 1px 2px rgba(15,23,42,.04);
  --shadow-md:0 10px 30px rgba(15,23,42,.08);
  --radius-sm:8px;
  --radius-md:12px;
  --radius-lg:18px;
  --topbar-h:56px;
}
html{height:100%;background:var(--bg)}
body{
  font-family:Inter,'Segoe UI',Roboto,Arial,sans-serif;
  background:
    radial-gradient(circle at 8% -12%,rgba(37,99,235,.10),transparent 34%),
    linear-gradient(180deg,#fbfdff 0%,var(--bg) 56%,#f3f6fa 100%);
  color:var(--text);
  min-height:100vh;
  line-height:1.5;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:none;color:var(--accent-hover)}
h1,h2,h3{color:var(--text-strong);letter-spacing:-.025em;line-height:1.22}
p{line-height:1.65}
button,.btn,a.button{
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  min-height:34px;padding:8px 14px;
  background:var(--surface);
  border:1px solid var(--border);
  color:var(--text);
  border-radius:var(--radius-sm);
  cursor:pointer;
  font-size:13px;
  font-weight:650;
  letter-spacing:.005em;
  transition:background .16s,border-color .16s,color .16s,box-shadow .16s,transform .16s;
  text-decoration:none;
  box-shadow:var(--shadow-sm);
}
button:hover,.btn:hover,a.button:hover{
  background:var(--surface2);
  border-color:var(--border-strong);
  color:var(--text-strong);
  box-shadow:0 8px 20px rgba(15,23,42,.08);
  transform:translateY(-1px);
  text-decoration:none;
}
button:active,.btn:active,a.button:active{transform:translateY(0);box-shadow:var(--shadow-sm)}
button:focus-visible,.btn:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{
  outline:3px solid rgba(37,99,235,.16);
  outline-offset:2px;
}
.btn-primary,button.main-btn{
  background:var(--accent);
  border-color:var(--accent);
  color:#fff;
  box-shadow:0 10px 22px rgba(37,99,235,.20);
}
.btn-primary:hover,button.main-btn:hover{
  background:var(--accent-hover);
  border-color:var(--accent-hover);
  color:#fff;
}
.btn-danger{
  background:#fff;
  border-color:#fecaca;
  color:var(--danger);
}
.btn-danger:hover{
  background:var(--danger-soft);
  border-color:#fca5a5;
  color:#991b1b;
}
.card,.box,.summary-box,.diff-box{
  background:rgba(255,255,255,.92);
  border:1px solid var(--border);
  border-radius:var(--radius-md);
  padding:18px;
  margin-bottom:16px;
  box-shadow:var(--shadow-sm);
}
.card:hover,.box:hover,.summary-box:hover{box-shadow:0 8px 26px rgba(15,23,42,.06)}
input[type=text],input[type=email],input[type=password],input[type=tel],input[type=number],textarea,select{
  width:100%;
  background:#fff;
  border:1px solid var(--border);
  color:var(--text);
  padding:9px 12px;
  border-radius:var(--radius-sm);
  font-size:14px;
  font-family:inherit;
  outline:none;
  transition:border-color .16s,box-shadow .16s,background .16s;
  box-shadow:0 1px 1px rgba(15,23,42,.02);
}
input:hover,textarea:hover,select:hover{border-color:var(--border-strong)}
input:focus,textarea:focus,select:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 4px rgba(37,99,235,.12);
}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;margin-top:12px;font-weight:650}
input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px;cursor:pointer}
table{
  border-collapse:separate;
  border-spacing:0;
  width:100%;
  background:#fff;
  border:1px solid var(--border);
  border-radius:var(--radius-md);
  overflow:hidden;
  box-shadow:var(--shadow-sm);
}
th,td{
  border:0;
  border-bottom:1px solid var(--border);
  padding:10px 12px;
  text-align:left;
  vertical-align:top;
  font-size:12px;
}
th{
  background:linear-gradient(180deg,#f8fafc,#eef4f9);
  color:#42526a;
  font-weight:750;
  position:sticky;
  top:0;
  z-index:1;
  text-transform:uppercase;
  letter-spacing:.035em;
  font-size:11px;
}
tr:last-child td{border-bottom:0}
tr:hover td{background:#f8fbff}
.badge,.tag{
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 9px;
  border-radius:999px;
  font-size:11px;
  font-weight:750;
  line-height:1.2;
}
.badge-active,.flash-ok{background:var(--success-soft);color:#166534;border:1px solid #bbf7d0}
.badge-inactive{background:var(--warn-soft);color:#92400e;border:1px solid #fed7aa}
.badge-error,.tag-2{background:var(--danger-soft);color:#991b1b;border:1px solid #fecaca}
.badge-warning{background:var(--warn-soft);color:#92400e;border:1px solid #fed7aa}
.badge-info,.tag-1{background:var(--info-soft);color:#075985;border:1px solid #bae6fd}
.flash-err{
  background:var(--danger-soft);
  border:1px solid #fecaca;
  border-radius:var(--radius-md);
  padding:11px 14px;
  color:#991b1b;
  font-size:13px;
  margin-bottom:14px;
  box-shadow:var(--shadow-sm);
}
.flash-ok{
  border-radius:var(--radius-md);
  padding:11px 14px;
  font-size:13px;
  margin-bottom:14px;
  box-shadow:var(--shadow-sm);
}
.small{font-size:12px;color:var(--muted)}
footer{
  text-align:center;
  padding:24px 0 14px;
  border-top:1px solid var(--border);
  color:var(--muted);
  font-size:12px;
  margin-top:40px;
}
pre{
  white-space:pre-wrap;
  word-break:break-word;
  margin:0;
  font-size:12px;
  background:#f8fafc;
  border:1px solid var(--border);
  border-radius:var(--radius-md);
  padding:14px;
}
.model-card,.cat-item{
  background:#fff;
  border-bottom:1px solid var(--border);
}
.cat-item:hover{background:#f1f6ff}
.sev-error{color:#991b1b;font-weight:750}
.sev-warning{color:#92400e;font-weight:750}
.sev-info{color:#075985}
.danger{color:#991b1b;font-weight:750}
.success{color:#166534;font-weight:750}
.info-banner{
  background:var(--warn-soft);
  border:1px solid #fed7aa;
  border-radius:var(--radius-md);
  padding:11px 14px;
  margin-bottom:16px;
  font-size:13px;
  color:#6b4a0f;
}

/* Project/viewer shell refinement */
[style*="height:calc(100vh - 47px)"]{height:calc(100vh - var(--topbar-h)) !important}
[style*="height:100vh"]{background:transparent}
[style*="background:var(--surface)"]{background:var(--surface) !important}
[style*="background:var(--surface2)"]{background:var(--surface2) !important}
[style*="background:#0f2040"],
[style*="background:#12192e"],
[style*="background:#0e1e38"],
[style*="background:#0e1a30"],
[style*="background:#1a3a6e"],
[style*="background:#223a5e"]{
  background:var(--surface2) !important;
}
[style*="background:#0e0e1a"],
[style*="background:rgba(14,14,26"]{
  background:rgba(248,250,252,.94) !important;
}
[style*="border-left:1px solid var(--border)"],
[style*="border-right:1px solid var(--border)"],
[style*="border-bottom:1px solid var(--border)"],
[style*="border-top:1px solid var(--border)"]{
  border-color:var(--border) !important;
}
[style*="color:#8ab"],[style*="color:#889"],[style*="color:#4a6080"],[style*="color:#445"]{
  color:var(--muted) !important;
}
[style*="color:#4fc3f7"]{color:var(--accent) !important}
[style*="color:#ffb3b3"],[style*="color:#ffaaaa"]{color:#991b1b !important}
[style*="box-shadow:0 8px 40px rgba(0,0,0,.7)"]{
  box-shadow:0 24px 70px rgba(15,23,42,.16) !important;
}
#loading{backdrop-filter:blur(6px)}
#loading [style*="border:4px"]{
  border-color:#dbeafe !important;
  border-top-color:var(--accent) !important;
}
#info-panel{
  background:#fff !important;
  box-shadow:-16px 0 40px rgba(15,23,42,.06);
}
#ai-chat-panel{
  background:#fff !important;
  border-color:var(--border) !important;
  box-shadow:0 24px 70px rgba(15,23,42,.18) !important;
}
.ai-header{
  background:linear-gradient(180deg,#fff,#f8fafc) !important;
  border-bottom:1px solid var(--border) !important;
}
.ai-header-icon{background:linear-gradient(135deg,#2563eb,#0ea5e9) !important;color:#fff}
.ai-header-title{color:var(--text-strong) !important}
.ai-header-sub,.ai-slot-label{color:var(--muted) !important}
.ai-slot-row,.ai-suggestions{border-color:var(--border) !important;background:#fff !important}
.ai-slot-select{
  background:#fff !important;
  border:1px solid var(--border) !important;
  color:var(--text) !important;
}
.ai-msg.user{
  background:var(--accent) !important;
  color:#fff !important;
}
.ai-msg.assistant,.ai-msg.typing{
  background:#f8fafc !important;
  color:var(--text) !important;
  border:1px solid var(--border) !important;
}
.ai-msg.error{
  background:var(--danger-soft) !important;
  color:#991b1b !important;
  border:1px solid #fecaca !important;
}
.ai-close{color:var(--muted) !important;background:transparent !important;border:0 !important;box-shadow:none !important}
@keyframes spin{to{transform:rotate(360deg)}}
@media (max-width:900px){
  .card,.box,.summary-box{padding:16px}
  button,.btn,a.button{width:auto}
  table{font-size:12px}
}
</style>"""


def _footer():
    return (
        '<footer style="text-align:center;margin-top:40px;padding:20px 0 10px;'
        'border-top:1px solid #e5e5e5;color:#888;font-size:13px;">'
        '<p>BIMPruef Platform by Foad Amini · '
        '<a href="mailto:amini.foad@gmail.com" style="color:#888">amini.foad@gmail.com</a></p>'
        '<p><a href="/impressum" style="color:#888;margin:0 10px">Impressum</a>'
        '<a href="/datenschutz" style="color:#888;margin:0 10px">Datenschutz</a></p>'
        "</footer>"
    )


def _build_page(title: str, body_html: str) -> HTMLResponse:
    return HTMLResponse(
        f"<html><head><title>{html.escape(title)}</title>{_base_styles()}</head>"
        f'<body><div class="container">{body_html}{_footer()}</div></body></html>'
    )


def _render_error(title: str, message: str) -> HTMLResponse:
    body = (
        f"<h2 class='danger'>{html.escape(title)}</h2>"
        f"<pre>{html.escape(message)}</pre>"
        '<p><a class="button" href="/">Zurück</a></p>'
    )
    return _build_page(title, body)


def _pretty_json(data) -> str:
    return html.escape(
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
            default=str,
        )
    )


def _session_nav(session_id: str) -> str:
    sid = html.escape(session_id)
    return (
        '<div class="box"><h3>Navigation</h3>'
        f'<a class="button" href="/viewer/?session_id={sid}">3D-Viewer</a>'
        f'<a class="button" href="/session/{sid}">Session-Dashboard</a>'
        f'<a class="button" href="/objects/?session_id={sid}&source=both">Objekte</a>'
        f'<a class="button" href="/compare-elements/?session_id={sid}">Vergleich</a>'
        f'<a class="button" href="/compare-clashes/?session_id={sid}">Clash</a>'
        "</div>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Upload – zwei Dateien gleichzeitig
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/upload-session/")
async def upload_session(
    file_1: UploadFile = File(...),
    file_2: UploadFile = File(...),
):
    try:
        for upload_file, number in [(file_1, "1"), (file_2, "2")]:
            filename = upload_file.filename or ""
            if not filename.lower().endswith((".ifc", ".ifczip")):
                return _render_error(
                    "Ungültige Datei",
                    f"Datei {number} ist keine IFC/IFCZIP-Datei.",
                )

        content_1 = await file_1.read()
        content_2 = await file_2.read()

        for content, number in [(content_1, "1"), (content_2, "2")]:
            if len(content) > MAX_FILE_SIZE_BYTES:
                return _render_error(
                    "Datei zu groß",
                    f"Modell {number} überschreitet {MAX_FILE_SIZE_MB} MB.",
                )

        from app.storage import save_ifc_file

        session_id = create_upload_session()
        save_ifc_file(session_id, 1, content_1, file_1.filename)
        save_ifc_file(session_id, 2, content_2, file_2.filename)

        return RedirectResponse(
            url=f"/session/{session_id}",
            status_code=303,
        )

    except Exception as exc:
        return _render_error("Upload fehlgeschlagen", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Routen
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_dashboard(session_id: str):
    try:
        loaded = load_ifc_models_from_session(session_id)

        body = [
            "<h1>Session-Dashboard</h1>",
            '<p><a class="button" href="/">Zurück</a></p>',
            '<div class="box">',
            f"<p><strong>Session-ID:</strong> {html.escape(session_id)}</p>",
            f"<p><strong>Modell 1:</strong> {html.escape(loaded['file_label_1'])}</p>",
            f"<p><strong>Modell 2:</strong> {html.escape(loaded['file_label_2'])}</p>",
            "</div>",
            _session_nav(session_id),
        ]

        return _build_page("Session-Dashboard", "".join(body))

    except Exception as exc:
        return _render_error("Session nicht verfügbar", str(exc))


@app.get("/objects/", response_class=HTMLResponse)
def show_ifc_objects(
    session_id: str = Query(...),
    source: str = Query(default="both"),
    ifc_type: str = Query(default=None),
    global_id: str = Query(default=None),
):
    try:
        loaded = load_ifc_models_from_session(session_id)
        all_objects = []

        if source in ["model_1", "both"]:
            all_objects.extend(
                get_objects_from_model(
                    loaded["model_1"],
                    loaded["file_label_1"],
                )
            )

        if source in ["model_2", "both"]:
            all_objects.extend(
                get_objects_from_model(
                    loaded["model_2"],
                    loaded["file_label_2"],
                )
            )

        filtered = filter_objects(
            all_objects,
            ifc_type=ifc_type,
            global_id=global_id,
        )

        body = [
            "<h1>IFC-Objekte</h1>",
            '<p><a class="button" href="/">Zurück</a></p>',
            _session_nav(session_id),
            '<div class="table-wrap"><table class="main-table">',
            "<tr><th>#</th><th>Datei</th><th>Express-ID</th><th>Typ</th>"
            "<th>Name</th><th>GlobalId</th><th>Eigenschaften</th></tr>",
            build_object_rows(filtered),
            "</table></div>",
        ]

        return _build_page("IFC-Objekte", "".join(body))

    except Exception as exc:
        return _render_error("Fehler", str(exc))


@app.get("/compare-elements/", response_class=HTMLResponse)
def compare_elements(session_id: str = Query(...)):
    try:
        loaded = load_ifc_models_from_session(session_id)

        comparison = compare_models(
            loaded["model_1"],
            loaded["model_2"],
            file_label_1=loaded["file_label_1"],
            file_label_2=loaded["file_label_2"],
        )

        summary = comparison["summary"]

        body = [
            "<h1>Elementvergleich</h1>",
            '<p><a class="button" href="/">Zurück</a></p>',
            _session_nav(session_id),
            '<div class="summary-box"><h3>Zusammenfassung</h3><div class="summary-grid">',
        ]

        for key, value in summary.items():
            body.append(
                f'<div class="summary-item"><strong>{html.escape(str(key))}</strong>'
                f"<br>{html.escape(str(value))}</div>"
            )

        body.append("</div></div>")

        return _build_page("Elementvergleich", "".join(body))

    except Exception as exc:
        return _render_error("Elementvergleich fehlgeschlagen", str(exc))


@app.get("/compare-clashes/", response_class=HTMLResponse)
def compare_clashes(
    session_id: str = Query(...),
    tolerance: float = Query(default=0.0),
):
    try:
        loaded = load_ifc_models_from_session(session_id)

        clashes = compare_models_for_clashes(
            loaded["model_1"],
            loaded["model_2"],
            tolerance=tolerance,
        )

        save_clash_cache(session_id, tolerance, clashes)

        body = [
            "<h1>Clash-Erkennung</h1>",
            '<p><a class="button" href="/">Zurück</a></p>',
            _session_nav(session_id),
            f'<div class="box"><p><strong>Gefundene Clashes:</strong> {len(clashes)}</p>',
            f'<a class="button" href="/download-clashes-bcf/?session_id='
            f'{html.escape(session_id)}&tolerance={tolerance}">BCF herunterladen</a></div>',
        ]

        if clashes:
            body.append('<div class="table-wrap"><table class="main-table">')
            body.append(
                "<tr><th>#</th><th>Typ 1</th><th>Name 1</th><th>GlobalId 1</th>"
                "<th>Typ 2</th><th>Name 2</th><th>GlobalId 2</th></tr>"
            )

            for index, clash in enumerate(clashes, 1):
                body.append(
                    f"<tr><td>{index}</td>"
                    f"<td>{html.escape(str(clash.get('type_1', '')))}</td>"
                    f"<td>{html.escape(str(clash.get('name_1', '')))}</td>"
                    f"<td>{html.escape(str(clash.get('global_id_1', '')))}</td>"
                    f"<td>{html.escape(str(clash.get('type_2', '')))}</td>"
                    f"<td>{html.escape(str(clash.get('name_2', '')))}</td>"
                    f"<td>{html.escape(str(clash.get('global_id_2', '')))}</td></tr>"
                )

            body.append("</table></div>")

        return _build_page("Clash-Erkennung", "".join(body))

    except Exception as exc:
        return _render_error("Clash-Erkennung fehlgeschlagen", str(exc))


@app.get("/download-clashes-bcf/")
def download_clashes_bcf(
    session_id: str = Query(...),
    tolerance: float = Query(default=0.0),
):
    try:
        clashes = load_clash_cache(session_id, tolerance)

        if clashes is None:
            loaded = load_ifc_models_from_session(session_id)
            clashes = compare_models_for_clashes(
                loaded["model_1"],
                loaded["model_2"],
                tolerance=tolerance,
            )
            save_clash_cache(session_id, tolerance, clashes)

        data = create_bcf_zip_from_clashes(clashes[:BCF_CLASH_LIMIT])

        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": 'attachment; filename="ifc_clashes.bcfzip"'
            },
        )

    except Exception as exc:
        return _render_error("BCF-Export fehlgeschlagen", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Impressum & Datenschutz
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/impressum", response_class=HTMLResponse)
def impressum(request: Request):
    content = render_impressum_module(back_link="/")
    return _build_page("Impressum – BIMPruef", content)


@app.get("/datenschutz", response_class=HTMLResponse)
def datenschutz(request: Request):
    content = render_datenschutz_module(back_link="/")
    return _build_page("Datenschutz – BIMPruef", content)


# ─────────────────────────────────────────────────────────────────────────────
# Session-Löschung – beim Schließen des Browser-Tabs
# ─────────────────────────────────────────────────────────────────────────────

@app.delete("/session/delete/")
async def session_delete_endpoint(request: Request):
    """
    Löscht eine temporäre Nicht-Projekt-Session sofort.

    Projekt-Sessions werden hier nicht gelöscht, weil sie über
    project_storage.py verwaltet werden.
    """
    try:
        body = await request.json()
        session_id = str(body.get("session_id", "")).strip()
    except Exception:
        session_id = request.query_params.get("session_id", "").strip()

    if session_id:
        try:
            from app.auth import get_current_user_optional
            from app.project_storage import get_project_session, list_projects

            user = get_current_user_optional(request)
            account_id = user["user_id"] if user else None
            project_sessions: set[str] = set()

            if account_id:
                for project in list_projects(account_id):
                    project_session_id = get_project_session(
                        account_id,
                        project["project_id"],
                    )
                    if project_session_id:
                        project_sessions.add(project_session_id)

        except Exception:
            project_sessions = set()

        if session_id not in project_sessions:
            delete_session(session_id)

    return Response(status_code=204)
