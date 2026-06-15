"""
main.py – BIMPruef FastAPI-Applikation
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.auth import auth_router, get_current_user_optional
from app.list_module import list_router
from app.projects import projects_router
from app.project_clash import project_clash_router
from app.project_rulecheck import project_rulecheck_router
from app.project_viewer import project_viewer_router
from app.r2_storage import download_file_from_r2, r2_enabled, upload_file_to_r2


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(
    title="BIMPruef – IFC Comparison Platform",
    lifespan=lifespan,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    public_prefixes = (
        "/",
        "/auth",
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


app.include_router(auth_router)
app.include_router(projects_router)
app.include_router(project_clash_router)
app.include_router(project_rulecheck_router)
app.include_router(list_router)
app.include_router(project_viewer_router)


@app.get("/")
async def root(request: Request):
    user = get_current_user_optional(request)
    if user:
        return RedirectResponse("/projects", status_code=302)
    return RedirectResponse("/auth/login", status_code=302)


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
