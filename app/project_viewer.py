"""
project_viewer.py – Project-scoped BIMPruef Viewer routes

This module makes the Viewer a real project module. The Viewer no longer owns
an upload flow in project context. IFC/IFCZIP files are discovered from the
project Documents module, downloaded from permanent document storage, and copied
into the existing viewer/session cache only as a derived runtime cache.
"""

from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.document_storage import (
    list_project_ifc_documents,
    prepare_viewer_session_from_project_documents,
)
from app.project_storage import get_or_create_project_session, get_project
from app.projects import (
    _account_from_request,
    _e,
    _fmt_size,
    _page,
    _project_subnav,
    _topbar_global,
)
from app.viewer import viewer_main

project_viewer_router = APIRouter()


def _render_empty_model_page(project: dict, account: dict, error: str = "") -> HTMLResponse:
    """Render the project Model module when no IFC/IFCZIP document exists yet."""
    pid = project["project_id"]
    flash = f'<div class="flash-err">{_e(error)}</div>' if error else ""
    body = f"""
    {_topbar_global(account)}
    {_project_subnav(pid, "model")}
    <div style="padding:28px 32px;max-width:920px;margin:0 auto">
      {flash}
      <div class="card" style="text-align:center;padding:34px 28px">
        <div style="font-size:32px;margin-bottom:10px">🏗</div>
        <h1 style="font-size:22px;font-weight:600;margin-bottom:8px">Model Viewer</h1>
        <p style="color:var(--muted);font-size:13px;max-width:620px;margin:0 auto 20px">
          Im Viewer gibt es keinen separaten Upload mehr. Lade zuerst im Documents-Modul eine IFC- oder IFCZIP-Datei hoch. Danach wird sie hier automatisch aus Documents geladen.
        </p>
        <a class="btn btn-primary" href="/projects/{_e(pid)}/documents" style="text-decoration:none">Zu Documents wechseln</a>
      </div>
    </div>
    """
    return _page(f"{project['project_name']} – Model", body)


def _render_model_source_page(project: dict, account: dict, error: str = "") -> HTMLResponse:
    """Optional source overview. It is not an upload page; Documents remains the source."""
    pid = project["project_id"]
    docs = list_project_ifc_documents(account["account_id"], pid)
    rows = ""
    for d in docs:
        rows += f"""
        <tr>
          <td style="font-weight:600;color:var(--accent)">{_e(d['original_filename'])}</td>
          <td>{_e(d['file_extension'])}</td>
          <td>{_fmt_size(d['file_size'])}</td>
          <td style="color:var(--muted)">{_e(d.get('folder_path') or 'Root')}</td>
          <td style="font-size:12px;color:var(--muted)">{_e(d.get('created_at','')[:10])}</td>
        </tr>
        """
    flash = f'<div class="flash-err">{_e(error)}</div>' if error else ""
    body = f"""
    {_topbar_global(account)}
    {_project_subnav(pid, "model")}
    <div style="padding:28px 32px;max-width:1050px;margin:0 auto">
      {flash}
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:18px">
        <div>
          <h1 style="font-size:22px;font-weight:600">Model Viewer</h1>
          <p style="color:var(--muted);font-size:13px;margin-top:4px">
            Permanente Quelle: Documents. Der Viewer verwendet daraus nur einen temporären Laufzeit-Cache.
          </p>
        </div>
        <div style="display:flex;gap:8px">
          <a class="btn" href="/projects/{_e(pid)}/documents" style="text-decoration:none">Documents</a>
          <a class="btn btn-primary" href="/projects/{_e(pid)}/model" style="text-decoration:none">Viewer öffnen</a>
        </div>
      </div>
      <div class="card" style="overflow-x:auto">
        <table>
          <tr><th>Name</th><th>Typ</th><th>Größe</th><th>Ordner</th><th>Upload</th></tr>
          {rows or '<tr><td colspan="5" style="color:var(--muted)">Keine IFC/IFCZIP-Dokumente vorhanden.</td></tr>'}
        </table>
      </div>
    </div>
    """
    return _page(f"{project['project_name']} – Model Sources", body)


def _load_project_context(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return account, None, ""
    session_id = get_or_create_project_session(account["account_id"], project_id)
    return account, project, session_id


@project_viewer_router.get("/projects/{project_id}/model", response_class=HTMLResponse)
def project_viewer(
    request: Request,
    project_id: str,
    error: str = Query(default=""),
    mode: str = Query(default=""),
):
    """
    Project-integrated Viewer.

    The route synchronizes the current project's IFC/IFCZIP documents into the
    existing viewer cache and then renders the preserved 3D viewer UI. This keeps
    the viewer functionality while making Documents the single file source.
    """
    account, project, session_id = _load_project_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    if mode in {"sources", "select"}:
        return _render_model_source_page(project, account, error=error)

    docs = list_project_ifc_documents(account["account_id"], project_id)
    if not docs:
        return _render_empty_model_page(project, account, error=error)

    document_ids = [d["document_id"] for d in docs]
    try:
        prepare_viewer_session_from_project_documents(
            account["account_id"],
            project_id,
            document_ids,
            session_id=session_id,
        )
    except Exception as exc:
        return _render_model_source_page(project, account, error=str(exc))

    return viewer_main(request, session_id=session_id, error=error, project_id=project_id)


@project_viewer_router.post("/projects/{project_id}/model/load")
def project_viewer_reload(
    request: Request,
    project_id: str,
    document_ids: list[str] = Form(default=[]),
):
    """
    Backward-compatible reload endpoint.

    If older forms still submit selected document IDs, they are accepted. If no
    IDs are submitted, all current IFC/IFCZIP project documents are loaded.
    """
    account, project, session_id = _load_project_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)

    try:
        ids = document_ids or [
            d["document_id"]
            for d in list_project_ifc_documents(account["account_id"], project_id)
        ]
        prepare_viewer_session_from_project_documents(
            account["account_id"],
            project_id,
            ids,
            session_id=session_id,
        )
        return RedirectResponse(f"/projects/{_e(project_id)}/model", status_code=303)
    except Exception as exc:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/model?mode=sources&error={quote_plus(str(exc))}",
            status_code=303,
        )


@project_viewer_router.post("/projects/{project_id}/model/upload")
async def project_viewer_upload_removed(request: Request, project_id: str):
    """Uploads are intentionally blocked in Viewer; Documents is the upload module."""
    return RedirectResponse(
        f"/projects/{_e(project_id)}/documents?error="
        + quote_plus("Der Viewer hat keinen eigenen Upload mehr. Bitte Dateien im Documents-Modul hochladen."),
        status_code=303,
    )


@project_viewer_router.post("/projects/{project_id}/model/remove")
def project_viewer_remove_removed(request: Request, project_id: str):
    """Closing single cache slots is disabled for project viewer consistency."""
    return RedirectResponse(
        f"/projects/{_e(project_id)}/model?mode=sources&error="
        + quote_plus("Modelle werden projektbezogen aus Documents geladen. Entferne Dateien im Documents-Modul."),
        status_code=303,
    )
