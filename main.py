"""
main.py – BIMPruef FastAPI-Applikation

Primäre Struktur (projects_router):
  /                              → Account-/Projektübersicht
  /projects/new                  → Neues Projekt anlegen
  /projects/create               → Projekt erstellen (POST)
  /projects/{id}                 → Projekt-Dashboard
  /projects/{id}/model           → Integrierter 3D-Viewer
  /projects/{id}/model/clash     → Redirect → /projects/{id}/clash
  /projects/{id}/model/list      → Redirect → /projects/{id}/list
  /projects/{id}/model/rulecheck → Redirect → /projects/{id}/checking
  /projects/{id}/checking        → Rule-Check (eigenständiges Projektmodul)
  /projects/{id}/list            → Elementliste (eigenständiges Projektmodul)
  /projects/{id}/clash           → Clash-Analyse (eigenständiges Projektmodul)
  /projects/{id}/view            → Direct Viewer

Technische API-Endpunkte:
  /viewer/list/data/             → JSON-Daten-API Elementliste
  /viewer/list/export/           → Excel-Export Elementliste
  /projects/{id}/checking/run    → Rule-Check ausführen (JSON-API)
  /projects/{id}/checking/export → Rule-Check JSON-Export
  /projects/{id}/clash/run       → Clash ausführen (JSON-API)
  /projects/{id}/view/file/{id}  → IFC-Stream aus R2

Debug:
  /debug/r2-test                 → Temporärer R2 upload/download test
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

from app.auth import auth_router, get_current_user_optional
from app.legal_modules import render_datenschutz_module, render_impressum_module
from app.list_module import list_router
from app.projects import projects_router
from app.project_clash import project_clash_router
from app.project_rulecheck import project_rulecheck_router
from app.project_viewer import project_viewer_router
from app.r2_storage import download_file_from_r2, r2_enabled, upload_file_to_r2
from app.storage import cleanup_old_sessions, delete_session
from app.project_storage import list_projects

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

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ─────────────────────────────────────────────────────────────────────────────
# Middleware – Authentifizierung
# ─────────────────────────────────────────────────────────────────────────────

@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    public_prefixes = (
        "/",
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
app.include_router(projects_router)
app.include_router(project_clash_router)
app.include_router(project_rulecheck_router)
app.include_router(list_router)
app.include_router(project_viewer_router)


# ─────────────────────────────────────────────────────────────────────────────
# Landing Page
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    user = get_current_user_optional(request)
    if user:
        return RedirectResponse("/projects", status_code=302)
    landing_path = os.path.join(STATIC_DIR, "landing.html")
    with open(landing_path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ─────────────────────────────────────────────────────────────────────────────
# Debug – Cloudflare R2 Verbindung testen
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/debug/r2-test")
def r2_test():
    if not r2_enabled():
        return PlainTextResponse("R2 is not configured.", status_code=500)

    test_path = "/tmp/bimpruef-r2-test.txt"
    with open(test_path, "w", encoding="utf-8") as f:
        f.write("BIMPruef R2 test OK")

    storage_key = "debug/bimpruef-r2-test.txt"
    upload_file_to_r2(local_path=test_path, storage_key=storage_key, content_type="text/plain")

    download_path = "/tmp/bimpruef-r2-test-downloaded.txt"
    download_file_from_r2(storage_key=storage_key, local_path=download_path)

    with open(download_path, "r", encoding="utf-8") as f:
        content = f.read()

    return PlainTextResponse(f"R2 upload/download OK: {content}")


# ─────────────────────────────────────────────────────────────────────────────
# HTML-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _base_styles():
    return _bp_base_styles()


def _footer():
    return _bp_footer_html()


def _build_page(title: str, body_html: str) -> HTMLResponse:
    return _bp_build_page(title, body_html)


def _render_error(title: str, message: str) -> HTMLResponse:
    return _bp_render_error(title, message)


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
