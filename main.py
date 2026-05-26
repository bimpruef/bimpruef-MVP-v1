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
  /projects/{id}/model/clash     → Clash-Analyse (Redirect → /projects/{id}/clash)
  /projects/{id}/model/list      → Redirect → /projects/{id}/list
  /projects/{id}/model/rulecheck → Redirect → /projects/{id}/checking
  /projects/{id}/checking        → Rule-Check (eigenständiges Projektmodul)
  /projects/{id}/list            → Elementliste (eigenständiges Projektmodul)

Technische API-Endpunkte:
  /viewer/file/                  → IFC-Datei ausliefern
  /viewer/ai-chat/               → KI-Assistent
  /viewer/list/data/             → JSON-Daten-API Elementliste
  /viewer/list/export/           → Excel-Export Elementliste
  /projects/{id}/checking/run    → Rule-Check ausführen (JSON-API)
  /projects/{id}/checking/export → Rule-Check JSON-Export

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
import logging
import os
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Query, Request, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app.auth import auth_router, get_current_user_optional
from app.bcf_export import create_bcf_zip_from_clashes
from app.clash import compare_models_for_clashes
from app.extractors import (
    build_object_rows,
    extract_element_data,
    filter_objects,
    get_candidate_products,
    get_objects_from_model,
)
from app.ifc_loader import load_ifc_models_from_session
from app.legal_modules import render_datenschutz_module, render_impressum_module
from app.list_module import list_router
from app.projects import projects_router
from app.project_clash import project_clash_router
from app.project_rulecheck import project_rulecheck_router
from app.r2_storage import download_file_from_r2, r2_enabled, upload_file_to_r2
from app.storage import (
    cleanup_old_sessions,
    create_upload_session,
    delete_session,
    load_clash_cache,
    save_clash_cache,
)
from app.viewer import router as viewer_router
from app.templates import (
    _base_styles as _bp_base_styles,
    _footer_html as _bp_footer_html,
    _build_page as _bp_build_page,
    _render_error as _bp_render_error,
)

logger = logging.getLogger(__name__)


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

# Unified UI static assets
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


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
        "/static",
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
app.include_router(project_clash_router)
app.include_router(project_rulecheck_router)
app.include_router(viewer_router)
app.include_router(list_router)


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
    return _bp_base_styles()


def _footer():
    return _bp_footer_html()


def _build_page(title: str, body_html: str) -> HTMLResponse:
    return _bp_build_page(title, body_html)


def _render_error(title: str, message: str) -> HTMLResponse:
    return _bp_render_error(title, message)


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
# Elementvergleich – eigenständige Implementierung (keine Abhängigkeit zu
# compare.py).  Die gesamte Vergleichslogik lebt hier, nah am Endpunkt der
# sie benötigt.
# ─────────────────────────────────────────────────────────────────────────────

def _normalize(value):
    """
    Normalisiert einen Wert für einen stabilen, reihenfolgeunabhängigen
    Vergleich, indem er als JSON mit sortierten Schlüsseln serialisiert wird.
    """
    try:
        return json.loads(json.dumps(value, sort_keys=True, default=str))
    except Exception as exc:
        logger.warning("_normalize: Normalisierung fehlgeschlagen für %r – %s", value, exc)
        return value


def _build_element_index(model, file_label: str) -> tuple[dict, dict]:
    """
    Erstellt einen GlobalId → Element-Dict Index für alle Kandidaten-Elemente.

    Gibt (index, duplicates) zurück; beide sind Dicts mit GlobalId als Schlüssel.
    Duplikate entstehen bei fehlerhaften IFC-Dateien mit doppelten GlobalIds.
    """
    index: dict = {}
    duplicates: dict = defaultdict(list)

    for element in get_candidate_products(model):
        data = extract_element_data(element, file_label=file_label)
        gid = data.get("global_id", "")
        if not gid:
            continue

        if gid in index:
            duplicates[gid].append(data)
        else:
            index[gid] = data

    return index, duplicates


def _diff_elements(a: dict, b: dict) -> dict:
    """
    Gibt ein Dict mit Feldunterschieden zwischen zwei Element-Dicts zurück.
    Ein leeres Dict bedeutet: Elemente sind identisch.
    """
    differences: dict = {}

    for field in ("type", "name", "object_type", "predefined_type"):
        if _normalize(a.get(field)) != _normalize(b.get(field)):
            differences[field] = {
                "model_1": a.get(field),
                "model_2": b.get(field),
            }

    psets_a = _normalize(a.get("psets", {}))
    psets_b = _normalize(b.get("psets", {}))
    if psets_a != psets_b:
        differences["psets"] = {
            "model_1": psets_a,
            "model_2": psets_b,
        }

    return differences


def _run_element_comparison(
    model1,
    model2,
    file_label_1: str = "Modell 1",
    file_label_2: str = "Modell 2",
) -> dict:
    """
    Vergleicht zwei IFC-Modelle anhand der GlobalId und gibt ein strukturiertes
    Ergebnis zurück.

    Rückgabe-Keys:
        summary               – Elementzähler je Kategorie
        missing_in_model2     – nur in Modell 1 vorhandene Elemente
        new_in_model2         – nur in Modell 2 vorhandene Elemente
        changed               – in beiden vorhanden, aber unterschiedlich
        unchanged             – in beiden vorhanden und identisch
        duplicates            – doppelte GlobalIds je Modell
    """
    index1, duplicates1 = _build_element_index(model1, file_label=file_label_1)
    index2, duplicates2 = _build_element_index(model2, file_label=file_label_2)

    gids1 = set(index1)
    gids2 = set(index2)

    missing_in_model2: list = []
    new_in_model2: list = []
    changed: list = []
    unchanged: list = []

    for gid in sorted(gids1 - gids2):
        missing_in_model2.append(index1[gid])

    for gid in sorted(gids2 - gids1):
        new_in_model2.append(index2[gid])

    for gid in sorted(gids1 & gids2):
        a = index1[gid]
        b = index2[gid]
        diffs = _diff_elements(a, b)

        if diffs:
            changed.append(
                {
                    "global_id": gid,
                    "model_1": a,
                    "model_2": b,
                    "differences": diffs,
                }
            )
        else:
            unchanged.append(a)

    return {
        "summary": {
            "model_1_count": len(index1),
            "model_2_count": len(index2),
            "missing_in_model2": len(missing_in_model2),
            "new_in_model2": len(new_in_model2),
            "changed": len(changed),
            "unchanged": len(unchanged),
            "duplicates_in_model1": len(duplicates1),
            "duplicates_in_model2": len(duplicates2),
        },
        "missing_in_model2": missing_in_model2,
        "new_in_model2": new_in_model2,
        "changed": changed,
        "unchanged": unchanged,
        "duplicates": {
            "model_1": dict(duplicates1),
            "model_2": dict(duplicates2),
        },
    }


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

        comparison = _run_element_comparison(
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
            f'<div class="box"><p><strong>Gefundene Clashes:</strong> {len(clashes)}</p>'
            '<p class="small">BCF-Export gehört jetzt zum Issues-Modul.</p></div>',
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
def download_clashes_bcf_removed():
    return Response(
        content="BCF-Export wurde aus dem Clash-Modul entfernt. Bitte Issues-Modul verwenden.",
        status_code=410,
    )


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
