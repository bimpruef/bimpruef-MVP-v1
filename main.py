"""
main.py – BIMPruef FastAPI-Applikation

Nach PIN-Login wird direkt zum 3D-Viewer weitergeleitet (neue Session).
Legacy-Routen (Session-Dashboard, Objektliste, Vergleich, Clash) bleiben erhalten.
"""



import html
import json
import os

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.storage import (
    create_upload_session,
    get_session_file_paths,
    save_original_filename,
    sanitize_filename,
    cleanup_old_sessions,
    save_clash_cache,
    load_clash_cache,
    delete_session,
)
from app.ifc_loader import load_ifc_models_from_session
from app.extractors import (
    get_objects_from_model,
    filter_objects,
    build_object_rows,
)
from app.compare import compare_models
from app.clash import compare_models_for_clashes
from app.bcf_export import create_bcf_zip_from_clashes
from app.viewer import router as viewer_router
from app.list_module import list_router
from app.rulecheck import rulecheck_router
from app.legal_modules import render_impressum_module, render_datenschutz_module

# ─────────────────────────────────────────────────────────────────────────────
# Konfiguration
# ─────────────────────────────────────────────────────────────────────────────
ACCESS_PIN      = os.environ.get("ACCESS_PIN", "16880")
BCF_CLASH_LIMIT = int(os.environ.get("BCF_CLASH_LIMIT", "500"))
MAX_FILE_SIZE_MB    = 100
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024


@asynccontextmanager
async def lifespan(app):
    cleanup_old_sessions()
    yield


app = FastAPI(title="BIMPruef – IFC Comparison Platform", lifespan=lifespan)
app.include_router(viewer_router)
app.include_router(list_router)
app.include_router(rulecheck_router)


# ─────────────────────────────────────────────────────────────────────────────
# PIN-Middleware
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def pin_protection(request, call_next):
    open_paths = ["/impressum", "/datenschutz", "/pin"]
    if request.url.path in open_paths:
        return await call_next(request)

    pin_cookie = request.cookies.get("access_pin", "")
    if pin_cookie == ACCESS_PIN:
        return await call_next(request)

    if request.method == "POST" and request.url.path == "/pin-check":
        form        = await request.form()
        entered_pin = form.get("pin", "")
        if entered_pin == ACCESS_PIN:
            # Neue Session pro Browser-Fenster – kein Session-Cookie setzen,
            # damit jedes Fenster seine eigene, unabhängige Session bekommt.
            new_session = create_upload_session()
            response    = RedirectResponse(
                url=f"/viewer/?session_id={new_session}", status_code=303
            )
            response.set_cookie(
                key="access_pin", value=ACCESS_PIN,
                httponly=True, samesite="lax", max_age=60 * 60 * 8,
            )
            # Kein bimpruef_session-Cookie mehr – Session-ID wird nur im
            # sessionStorage des Browsers gehalten (siehe viewer.py JS).
            return response
        else:
            return _pin_page(error=True)

    return _pin_page(error=False)


def _pin_page(error: bool):
    error_msg = (
        '<p style="color:#e94560;margin:0 0 12px;font-size:14px;">'
        "PIN ungültig. Bitte erneut versuchen.</p>"
    ) if error else ""
    content = f"""<!DOCTYPE html>
<html><head><title>BIMPruef – PIN eingeben</title>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:'Segoe UI',system-ui,sans-serif;background:#0e0e1a;
    display:flex;align-items:center;justify-content:center;min-height:100vh}}
  .card{{background:#16213e;padding:44px 40px;border-radius:14px;
    border:1px solid #1e3a6e;width:340px;text-align:center;
    box-shadow:0 8px 32px rgba(0,0,0,.5)}}
  h2{{margin:0 0 4px;font-size:22px;color:#4fc3f7;letter-spacing:1px;
    font-family:monospace}}
  p.sub{{color:#4a6080;font-size:12px;margin:0 0 28px;letter-spacing:.5px}}
  input[type=password]{{width:100%;padding:13px;font-size:22px;
    letter-spacing:8px;text-align:center;background:#0e1a30;
    border:1px solid #1e3a6e;border-radius:8px;color:#d0dce8;
    margin-bottom:16px;outline:none;transition:border-color .2s;
    font-family:monospace}}
  input[type=password]:focus{{border-color:#4fc3f7}}
  button{{width:100%;padding:13px;background:#4fc3f7;color:#0a1a2e;
    border:none;border-radius:8px;font-size:14px;font-weight:700;
    cursor:pointer;transition:background .15s}}
  button:hover{{background:#81d4fa}}
  .links{{margin-top:22px;font-size:11px}}
  .links a{{color:#2a5080;margin:0 8px;text-decoration:none}}
  .links a:hover{{color:#4fc3f7}}
</style>
</head><body>
<div class="card">
  <h2>BIMPruef</h2>
  <p class="sub">IFC · CLASH · 3D</p>
  {error_msg}
  <form method="post" action="/pin-check">
    <input type="password" name="pin" placeholder="••••••" autofocus maxlength="10">
    <button type="submit">Zugang</button>
  </form>
  <div class="links">
    <a href="/impressum">Impressum</a>
    <a href="/datenschutz">Datenschutz</a>
  </div>
</div>
</body></html>"""
    return HTMLResponse(content=content)


# ─────────────────────────────────────────────────────────────────────────────
# Root → Viewer
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    # Immer eine neue Session erstellen – niemals Cookie-Session wiederverwenden,
    # damit jedes Browserfenster eine eigene, isolierte Sitzung erhält.
    new_session = create_upload_session()
    response = RedirectResponse(url=f"/viewer/?session_id={new_session}", status_code=302)
    return response


# ─────────────────────────────────────────────────────────────────────────────
# HTML-Hilfsfunktionen (Legacy)
# ─────────────────────────────────────────────────────────────────────────────

def _base_styles():
    return """<style>
body{font-family:Arial,sans-serif;margin:20px;background:#f7f7f7;color:#222}
.container{max-width:1250px;margin:0 auto;background:white;padding:30px;
  border-radius:12px;box-shadow:0 2px 10px rgba(0,0,0,0.06)}
.box,.summary-box{background:white;padding:18px;border-radius:10px;
  box-shadow:0 2px 8px rgba(0,0,0,0.05);margin-bottom:20px}
h1,h2,h3{margin-top:0}
p{line-height:1.6}
a.button{display:inline-block;padding:10px 16px;background:#f2f2f2;
  border:1px solid #ccc;text-decoration:none;color:#000;border-radius:6px;
  margin:0 10px 10px 0}
a.button:hover{background:#e8e8e8}
.table-wrap{background:white;padding:15px;border-radius:10px;
  box-shadow:0 2px 8px rgba(0,0,0,0.05);overflow-x:auto;margin-bottom:20px}
table.main-table{border-collapse:collapse;width:100%;
  table-layout:fixed;background:white}
.main-table th,.main-table td{border:1px solid #ddd;padding:10px;
  vertical-align:top;text-align:left;word-wrap:break-word}
.main-table th{background:#f2f2f2;position:sticky;top:0;z-index:1}
.main-table tr:hover{background:#fafafa}
.pset-block{border:1px solid #e2e2e2;border-radius:6px;padding:8px;
  margin-bottom:8px;background:#fcfcfc}
.pset-title{font-weight:bold;margin-bottom:6px;color:#333}
table.prop-table{border-collapse:collapse;width:100%;font-size:12px}
.prop-table th,.prop-table td{border:1px solid #ddd;padding:6px;
  text-align:left;vertical-align:top}
.prop-table th{background:#f8f8f8}
.muted{color:#777;font-style:italic}
.summary-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.summary-item{background:#fafafa;border:1px solid #e5e5e5;border-radius:8px;padding:10px}
input[type=text],input[type=number],input[type=file],select{
  width:420px;max-width:100%;padding:8px 10px;border:1px solid #ccc;border-radius:6px}
button{padding:10px 16px;border:1px solid #ccc;background:#f2f2f2;
  border-radius:6px;cursor:pointer}
button:hover{background:#e8e8e8}
.danger{color:#b00020;font-weight:bold}
.success{color:green;font-weight:bold}
pre{white-space:pre-wrap;word-break:break-word;margin:0;font-size:12px}
.diff-box{background:#fafafa;border:1px solid #e5e5e5;border-radius:8px;
  padding:14px;margin-bottom:14px}
.small{font-size:13px;color:#666}
.info-banner{background:#fff8e1;border:1px solid #ffe082;border-radius:8px;
  padding:10px 14px;margin-bottom:16px;font-size:13px;color:#555}
</style>"""


def _footer():
    return (
        '<footer style="text-align:center;margin-top:40px;padding:20px 0 10px;'
        'border-top:1px solid #e5e5e5;color:#888;font-size:13px;">'
        '<p>BIMPruef Platform by Foad Amini · '
        '<a href="mailto:amini.foad@gmail.com" style="color:#888">amini.foad@gmail.com</a></p>'
        '<p><a href="/impressum" style="color:#888;margin:0 10px">Impressum</a>'
        '<a href="/datenschutz" style="color:#888;margin:0 10px">Datenschutz</a></p>'
        '</footer>'
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
    return html.escape(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _session_nav(session_id: str) -> str:
    sid = html.escape(session_id)
    return (
        '<div class="box"><h3>Navigation</h3>'
        f'<a class="button" href="/viewer/?session_id={sid}">3D-Viewer</a>'
        f'<a class="button" href="/session/{sid}">Session-Dashboard</a>'
        f'<a class="button" href="/objects/?session_id={sid}&source=both">Objekte</a>'
        f'<a class="button" href="/compare-elements/?session_id={sid}">Vergleich</a>'
        f'<a class="button" href="/compare-clashes/?session_id={sid}">Clash</a>'
        '</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Upload (zwei Dateien gleichzeitig)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/upload-session/")
async def upload_session(
    file_1: UploadFile = File(...),
    file_2: UploadFile = File(...),
):
    try:
        for f, n in [(file_1, "1"), (file_2, "2")]:
            if not f.filename.lower().endswith((".ifc", ".ifczip")):
                return _render_error("Ungültige Datei", f"Datei {n} ist keine IFC/IFCZIP-Datei.")

        content_1 = await file_1.read()
        content_2 = await file_2.read()

        for c, n in [(content_1, "1"), (content_2, "2")]:
            if len(c) > MAX_FILE_SIZE_BYTES:
                return _render_error("Datei zu groß",
                    f"Modell {n} überschreitet {MAX_FILE_SIZE_MB} MB.")

        from app.storage import save_ifc_file
        session_id = create_upload_session()
        save_ifc_file(session_id, 1, content_1, file_1.filename)
        save_ifc_file(session_id, 2, content_2, file_2.filename)

        return RedirectResponse(url=f"/session/{session_id}", status_code=303)
    except Exception as exc:
        return _render_error("Upload fehlgeschlagen", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Routen
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/session/{session_id}", response_class=HTMLResponse)
def session_dashboard(session_id: str):
    try:
        loaded = load_ifc_models_from_session(session_id)
        body   = [
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
    session_id: str  = Query(...),
    source: str      = Query(default="both"),
    ifc_type: str    = Query(default=None),
    global_id: str   = Query(default=None),
):
    try:
        loaded      = load_ifc_models_from_session(session_id)
        all_objects = []
        if source in ["model_1", "both"]:
            all_objects.extend(get_objects_from_model(loaded["model_1"], loaded["file_label_1"]))
        if source in ["model_2", "both"]:
            all_objects.extend(get_objects_from_model(loaded["model_2"], loaded["file_label_2"]))
        filtered = filter_objects(all_objects, ifc_type=ifc_type, global_id=global_id)

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
        loaded     = load_ifc_models_from_session(session_id)
        comparison = compare_models(
            loaded["model_1"], loaded["model_2"],
            file_label_1=loaded["file_label_1"],
            file_label_2=loaded["file_label_2"],
        )
        summary = comparison["summary"]
        body    = [
            "<h1>Elementvergleich</h1>",
            '<p><a class="button" href="/">Zurück</a></p>',
            _session_nav(session_id),
            '<div class="summary-box"><h3>Zusammenfassung</h3><div class="summary-grid">',
        ]
        for k, v in summary.items():
            body.append(
                f'<div class="summary-item"><strong>{html.escape(str(k))}</strong>'
                f'<br>{html.escape(str(v))}</div>'
            )
        body.append("</div></div>")
        return _build_page("Elementvergleich", "".join(body))
    except Exception as exc:
        return _render_error("Elementvergleich fehlgeschlagen", str(exc))


@app.get("/compare-clashes/", response_class=HTMLResponse)
def compare_clashes(
    session_id: str  = Query(...),
    tolerance: float = Query(default=0.0),
):
    try:
        loaded  = load_ifc_models_from_session(session_id)
        clashes = compare_models_for_clashes(
            loaded["model_1"], loaded["model_2"], tolerance=tolerance
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
            for i, c in enumerate(clashes, 1):
                body.append(
                    f"<tr><td>{i}</td>"
                    f"<td>{html.escape(str(c.get('type_1','')))}</td>"
                    f"<td>{html.escape(str(c.get('name_1','')))}</td>"
                    f"<td>{html.escape(str(c.get('global_id_1','')))}</td>"
                    f"<td>{html.escape(str(c.get('type_2','')))}</td>"
                    f"<td>{html.escape(str(c.get('name_2','')))}</td>"
                    f"<td>{html.escape(str(c.get('global_id_2','')))}</td></tr>"
                )
            body.append("</table></div>")
        return _build_page("Clash-Erkennung", "".join(body))
    except Exception as exc:
        return _render_error("Clash-Erkennung fehlgeschlagen", str(exc))


@app.get("/download-clashes-bcf/")
def download_clashes_bcf(
    session_id: str  = Query(...),
    tolerance: float = Query(default=0.0),
):
    try:
        clashes = load_clash_cache(session_id, tolerance)
        if clashes is None:
            loaded  = load_ifc_models_from_session(session_id)
            clashes = compare_models_for_clashes(
                loaded["model_1"], loaded["model_2"], tolerance=tolerance
            )
            save_clash_cache(session_id, tolerance, clashes)
        data = create_bcf_zip_from_clashes(clashes[:BCF_CLASH_LIMIT])
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="ifc_clashes.bcfzip"'},
        )
    except Exception as exc:
        return _render_error("BCF-Export fehlgeschlagen", str(exc))


# ─────────────────────────────────────────────────────────────────────────────
# Impressum & Datenschutz
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/impressum", response_class=HTMLResponse)
def impressum(request: Request):
    # Da kein Session-Cookie mehr gesetzt wird, verlinkt "Zurück" auf die Root.
    content = render_impressum_module(back_link="/")
    return _build_page("Impressum – BIMPruef", content)


@app.get("/datenschutz", response_class=HTMLResponse)
def datenschutz(request: Request):
    content = render_datenschutz_module(back_link="/")
    return _build_page("Datenschutz – BIMPruef", content)


# ─────────────────────────────────────────────────────────────────────────────
# Session-Löschung (wird beim Schließen des Browser-Fensters aufgerufen)
# ─────────────────────────────────────────────────────────────────────────────

@app.delete("/session/delete/")
async def session_delete_endpoint(request: Request):
    """
    Löscht eine Session sofort, wenn der Browser das Fenster/Tab schließt.
    Wird per navigator.sendBeacon() oder fetch() mit keepalive aus dem
    beforeunload/visibilitychange-Handler aufgerufen.
    """
    try:
        body = await request.json()
        session_id = str(body.get("session_id", "")).strip()
    except Exception:
        session_id = request.query_params.get("session_id", "").strip()

    if session_id:
        delete_session(session_id)

    return Response(status_code=204)
