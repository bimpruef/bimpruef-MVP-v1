"""issues.py – Project Issues UI and BCF export routes."""

import html
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import require_user
from app.bcf_export import create_bcf_zip_from_issues
from app.issue_storage import delete_issue, list_project_issues, update_issue_status
from app.project_storage import get_project

issues_router = APIRouter()


def _e(value) -> str:
    return html.escape(str(value or ""))


def _account(request: Request) -> dict:
    user = require_user(request)
    return {"account_id": user["user_id"], "account_name": user["email"], "workspace": "Personal"}


@issues_router.get("/projects/{project_id}/issues", response_class=HTMLResponse)
def project_issues(request: Request, project_id: str, saved: str = Query(default=""), error: str = Query(default="")):
    from app.projects import _page, _project_subnav, _topbar_global
    account = _account(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    issues = list_project_issues(account["account_id"], project_id)
    flash = ""
    if saved:
        flash = f'<div class="flash-ok">{_e(saved)} Issue(s) gespeichert.</div>'
    if error:
        flash = f'<div class="flash-err">{_e(error)}</div>'
    if issues:
        rows = ""
        for issue in issues:
            els = issue.get("elements") or {}
            e1 = els.get("element_1", {})
            e2 = els.get("element_2", {})
            rows += f"""
            <tr>
              <td><input type="checkbox" form="export-form" name="issue_ids" value="{_e(issue['issue_id'])}"></td>
              <td><strong>{_e(issue['title'])}</strong><br><span style="color:var(--muted);font-size:11px">{_e(issue['source_type'])} · { _e(issue['created_at'][:10])}</span></td>
              <td>{_e(issue['status'])}</td>
              <td>{_e(issue['priority'])}</td>
              <td><code>{_e(e1.get('global_id',''))}</code><br>{_e(e1.get('ifc_type',''))} · {_e(e1.get('document_name',''))}</td>
              <td><code>{_e(e2.get('global_id',''))}</code><br>{_e(e2.get('ifc_type',''))} · {_e(e2.get('document_name',''))}</td>
              <td>
                <form method="POST" action="/projects/{_e(project_id)}/issues/update" style="display:flex;gap:4px;margin-bottom:5px">
                  <input type="hidden" name="issue_id" value="{_e(issue['issue_id'])}">
                  <select name="status"><option>open</option><option>in_progress</option><option>resolved</option><option>closed</option></select>
                  <button class="btn" type="submit">Status</button>
                </form>
                <form method="POST" action="/projects/{_e(project_id)}/issues/delete" onsubmit="return confirm('Issue löschen?')">
                  <input type="hidden" name="issue_id" value="{_e(issue['issue_id'])}">
                  <button class="btn btn-danger" type="submit">Löschen</button>
                </form>
              </td>
            </tr>"""
        table = f"""
        <form id="export-form" method="GET" action="/projects/{_e(project_id)}/issues/bcf">
          <div style="display:flex;gap:8px;margin-bottom:12px">
            <button class="btn btn-primary" type="submit">Ausgewählte als BCF exportieren</button>
            <a class="btn" href="/projects/{_e(project_id)}/issues/bcf?all=1">Alle Issues als BCF exportieren</a>
          </div>
        </form>
        <div class="card" style="overflow:auto"><table>
          <tr><th></th><th>Issue</th><th>Status</th><th>Priorität</th><th>Element 1</th><th>Element 2</th><th>Aktionen</th></tr>
          {rows}
        </table></div>"""
    else:
        table = '<div class="card"><p style="color:var(--muted)">Noch keine Issues vorhanden. Speichere zuerst Clash-Ergebnisse im Clash-Modul als Issues.</p></div>'
    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "issues")}
<div style="padding:28px 32px;max-width:1320px;margin:0 auto">
  <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px">
    <div><h1 style="font-size:22px;font-weight:600">Issues</h1><p style="color:var(--muted);font-size:13px;margin-top:4px">Verwaltung gespeicherter Issues und BCF-Export.</p></div>
    <a class="btn" href="/projects/{_e(project_id)}/clash">Zur Clash-Analyse</a>
  </div>
  {flash}{table}
</div>"""
    return _page(f"{project['project_name']} – Issues", body)


@issues_router.post("/projects/{project_id}/issues/update")
def project_issue_update(request: Request, project_id: str, issue_id: str = Form(...), status: str = Form(...), priority: str = Form(default="")):
    account = _account(request)
    try:
        update_issue_status(account["account_id"], project_id, issue_id, status, priority)
        return RedirectResponse(f"/projects/{_e(project_id)}/issues", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/projects/{_e(project_id)}/issues?error={_e(str(exc))}", status_code=303)


@issues_router.post("/projects/{project_id}/issues/delete")
def project_issue_delete(request: Request, project_id: str, issue_id: str = Form(...)):
    account = _account(request)
    try:
        delete_issue(account["account_id"], project_id, issue_id)
        return RedirectResponse(f"/projects/{_e(project_id)}/issues", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/projects/{_e(project_id)}/issues?error={_e(str(exc))}", status_code=303)


@issues_router.get("/projects/{project_id}/issues/bcf")
def project_issues_bcf(request: Request, project_id: str, issue_ids: list[str] = Query(default=[]), all: int = Query(default=0)):
    account = _account(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return Response("Projekt nicht gefunden.", status_code=404)
    issues = list_project_issues(account["account_id"], project_id)
    if not all:
        selected = set(issue_ids or [])
        issues = [i for i in issues if i.get("issue_id") in selected]
    if not issues:
        return Response("Keine Issues ausgewählt.", status_code=400)
    data = create_bcf_zip_from_issues(issues, project_name=project.get("project_name") or "BIMPruef Issues")
    return Response(content=data, media_type="application/octet-stream", headers={"Content-Disposition": 'attachment; filename="bimpruef_issues.bcfzip"'})
