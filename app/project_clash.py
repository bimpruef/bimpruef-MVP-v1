"""
project_clash.py – Project-scoped Clash Analysis for BIMPruef

The Clash module is now a project module. It reads IFC/IFCZIP files directly
from Documents and only uses the Viewer for optional 3D visualization.
"""

import html
import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_user
from app.clash import compare_project_documents_for_clashes
from app.document_storage import list_project_ifc_documents, prepare_viewer_session_from_project_documents
from app.extractors import flatten_psets
from app.issue_storage import create_issues_from_clashes
from app.project_storage import get_project

project_clash_router = APIRouter()


def _e(value) -> str:
    return html.escape(str(value or ""))


def _account(request: Request) -> dict:
    user = require_user(request)
    return {"account_id": user["user_id"], "account_name": user["email"], "workspace": "Personal"}


def _project_page(title: str, body: str) -> HTMLResponse:
    from app.projects import _page
    return _page(title, body)


def _topbar(account: dict) -> str:
    from app.projects import _topbar_global
    return _topbar_global(account)


def _subnav(project_id: str, active: str) -> str:
    from app.projects import _project_subnav
    return _project_subnav(project_id, active)


def _doc_options(docs: list[dict]) -> str:
    if not docs:
        return '<div class="flash-err">Keine IFC/IFCZIP-Dokumente vorhanden. Bitte zuerst im Documents-Modul hochladen.</div>'
    rows = []
    for d in docs:
        label = f"{d.get('original_filename','')} ({d.get('file_extension','')})"
        rows.append(
            f'<label style="display:flex;gap:8px;align-items:center;margin:4px 0;color:var(--text)">'
            f'<input type="checkbox" value="{_e(d["document_id"])}" checked> '
            f'<span>{_e(label)}</span>'
            f'<span style="color:var(--muted);font-size:11px">{_e(d.get("folder_path") or "Root")}</span>'
            f'</label>'
        )
    return "".join(rows)


@project_clash_router.get("/projects/{project_id}/clash", response_class=HTMLResponse)
def project_clash_page(request: Request, project_id: str, error: str = Query(default="")):
    account = _account(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    docs = list_project_ifc_documents(account["account_id"], project_id)
    docs_json = json.dumps(docs, ensure_ascii=False)
    error_html = f'<div class="flash-err">{_e(error)}</div>' if error else ""

    body = f"""
{_topbar(account)}
{_subnav(project_id, "clash")}
<div style="padding:28px 32px;max-width:1320px;margin:0 auto">
  <div style="display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:18px">
    <div>
      <h1 style="font-size:22px;font-weight:600">Clash-Analyse</h1>
      <p style="color:var(--muted);font-size:13px;margin-top:4px">
        Eigenständiges Projektmodul. Datenquelle: Documents / project_documents, nicht Viewer-Sessions.
      </p>
    </div>
    <div style="display:flex;gap:8px">
      <a class="btn" href="/projects/{_e(project_id)}/documents">Documents</a>
      <a class="btn" href="/projects/{_e(project_id)}/issues">Issues</a>
    </div>
  </div>
  {error_html}
  <div class="card">
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:18px">
      <section>
        <h2 style="font-size:16px;color:var(--accent);margin-bottom:8px">Gruppe A – Dokumente</h2>
        <div id="docs-a">{_doc_options(docs)}</div>
        <h3 style="font-size:13px;margin:14px 0 6px">Filter Gruppe A</h3>
        <div id="filters-a"></div>
        <button type="button" class="btn" onclick="addFilter('a')">+ Filter A</button>
      </section>
      <section>
        <h2 style="font-size:16px;color:var(--accent2);margin-bottom:8px">Gruppe B – Dokumente</h2>
        <div id="docs-b">{_doc_options(docs)}</div>
        <h3 style="font-size:13px;margin:14px 0 6px">Filter Gruppe B</h3>
        <div id="filters-b"></div>
        <button type="button" class="btn" onclick="addFilter('b')">+ Filter B</button>
      </section>
    </div>
    <div style="display:flex;align-items:center;gap:12px;margin-top:18px;border-top:1px solid var(--border);padding-top:16px">
      <label style="margin:0;color:var(--muted);font-size:12px;width:120px">Toleranz</label>
      <input id="tolerance" type="number" value="0" step="0.001" style="max-width:160px">
      <button id="run-btn" class="btn btn-primary" onclick="runClash()">Clash berechnen</button>
      <span id="run-status" style="color:var(--muted);font-size:12px"></span>
    </div>
  </div>
  <div id="result"></div>
</div>
<script>
const PROJECT_ID = {json.dumps(project_id)};
const DOCS = {docs_json};
let lastClashes = [];
const baseFields = [
  ['file_label','Datei'], ['type','IFC-Typ'], ['name','Name'], ['global_id','GlobalId'],
  ['object_type','ObjectType'], ['predefined_type','PredefinedType']
];
const operators = [
  ['contains','enthält'], ['not_contains','enthält nicht'], ['equals','ist gleich'],
  ['not_equals','ist nicht gleich'], ['starts_with','beginnt mit'], ['ends_with','endet mit']
];
function esc(s) {{ return String(s ?? '').replace(/[&<>"']/g, m => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}}[m])); }}
function addFilter(group) {{
  const wrap = document.getElementById('filters-' + group);
  const row = document.createElement('div');
  row.className = 'filter-row';
  row.style.cssText = 'display:grid;grid-template-columns:1.2fr 1fr 1.5fr auto;gap:6px;margin-bottom:6px';
  row.innerHTML = `
    <select class="f-field">${{baseFields.map(f=>`<option value="${{f[0]}}">${{f[1]}}</option>`).join('')}}</select>
    <select class="f-op">${{operators.map(o=>`<option value="${{o[0]}}">${{o[1]}}</option>`).join('')}}</select>
    <input class="f-value" type="text" placeholder="Suchwert">
    <button type="button" class="btn btn-danger" onclick="this.closest('.filter-row').remove()">×</button>`;
  wrap.appendChild(row);
}}
function selectedDocs(group) {{ return Array.from(document.querySelectorAll('#docs-' + group + ' input:checked')).map(x=>x.value); }}
function filters(group) {{ return Array.from(document.querySelectorAll('#filters-' + group + ' .filter-row')).map(r=>({{
  field:r.querySelector('.f-field').value, operator:r.querySelector('.f-op').value, value:r.querySelector('.f-value').value
}})).filter(f=>f.value.trim()); }}
async function runClash() {{
  const btn = document.getElementById('run-btn'); const status = document.getElementById('run-status');
  btn.disabled = true; status.textContent = 'Berechnung läuft …';
  document.getElementById('result').innerHTML = '<div class="card">Berechnung läuft …</div>';
  try {{
    const resp = await fetch(`/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/run`, {{
      method:'POST', headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{document_ids_a:selectedDocs('a'), document_ids_b:selectedDocs('b'), filters_a:filters('a'), filters_b:filters('b'), tolerance:parseFloat(document.getElementById('tolerance').value || '0')}})
    }});
    const data = await resp.json();
    if(!resp.ok || data.error) throw new Error(data.error || 'Unbekannter Fehler');
    lastClashes = data.clashes || [];
    renderResult(data);
    status.textContent = `${{lastClashes.length}} Clash(es) gefunden`;
  }} catch(e) {{
    document.getElementById('result').innerHTML = `<div class="flash-err">${{esc(e.message)}}</div>`;
    status.textContent = 'Fehler';
  }} finally {{ btn.disabled = false; }}
}}
function renderResult(data) {{
  const clashes = data.clashes || [];
  if(!clashes.length) {{ document.getElementById('result').innerHTML = '<div class="flash-ok">Keine Clashes gefunden.</div>'; return; }}
  const rows = clashes.map((c,i)=>{{
    const detail = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/detail?document_id_1=${{encodeURIComponent(c.document_id_1||'')}}&document_id_2=${{encodeURIComponent(c.document_id_2||'')}}&gid1=${{encodeURIComponent(c.global_id_1||'')}}&gid2=${{encodeURIComponent(c.global_id_2||'')}}`;
    return `<tr><td><input type="checkbox" class="clash-select" value="${{i}}"></td><td>${{i+1}}</td><td><strong>${{esc(c.type_1)}}</strong><br><span style="color:var(--muted)">${{esc(c.name_1)}}</span><br><code>${{esc(c.global_id_1)}}</code><br><small>${{esc(c.document_name_1||c.file_label_1)}}</small></td><td><strong>${{esc(c.type_2)}}</strong><br><span style="color:var(--muted)">${{esc(c.name_2)}}</span><br><code>${{esc(c.global_id_2)}}</code><br><small>${{esc(c.document_name_2||c.file_label_2)}}</small></td><td><a class="btn" href="${{detail}}">3D öffnen</a></td></tr>`;
  }}).join('');
  document.getElementById('result').innerHTML = `<div class="card"><div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px"><h2 style="font-size:16px">${{clashes.length}} Clash(es)</h2><div><button class="btn" onclick="document.querySelectorAll('.clash-select').forEach(x=>x.checked=true)">Alle wählen</button> <button class="btn btn-primary" onclick="saveSelectedIssues()">Als Issues speichern</button></div></div><table><tr><th></th><th>#</th><th>Element A</th><th>Element B</th><th>Ansicht</th></tr>${{rows}}</table></div>`;
}}
async function saveSelectedIssues() {{
  const selected = Array.from(document.querySelectorAll('.clash-select:checked')).map(x => lastClashes[parseInt(x.value)]).filter(Boolean);
  if(!selected.length) {{ alert('Bitte mindestens eine Clash-Zeile auswählen.'); return; }}
  const resp = await fetch(`/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/save-issues`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{clashes:selected}})}});
  const data = await resp.json();
  if(!resp.ok || data.error) {{ alert(data.error || 'Speichern fehlgeschlagen'); return; }}
  window.location.href = `/projects/${{encodeURIComponent(PROJECT_ID)}}/issues?saved=${{data.created || 0}}`;
}}
addFilter('a'); addFilter('b');
</script>
"""
    return _project_page(f"{project['project_name']} – Clash", body)


@project_clash_router.post("/projects/{project_id}/clash/run")
async def project_clash_run(request: Request, project_id: str):
    account = _account(request)
    if not get_project(account["account_id"], project_id):
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)
    try:
        payload = await request.json()
        data = compare_project_documents_for_clashes(
            account["account_id"], project_id,
            payload.get("document_ids_a") or [], payload.get("document_ids_b") or [],
            payload.get("filters_a") or [], payload.get("filters_b") or [],
            float(payload.get("tolerance") or 0.0),
        )
        return JSONResponse(data)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@project_clash_router.post("/projects/{project_id}/clash/save-issues")
async def project_clash_save_issues(request: Request, project_id: str):
    account = _account(request)
    if not get_project(account["account_id"], project_id):
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)
    try:
        payload = await request.json()
        clashes = payload.get("clashes") or []
        created = create_issues_from_clashes(account["account_id"], project_id, clashes)
        return JSONResponse({"ok": True, "created": len(created), "issues": created})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@project_clash_router.get("/projects/{project_id}/clash/detail")
def project_clash_detail(
    request: Request,
    project_id: str,
    document_id_1: str = Query(...),
    document_id_2: str = Query(...),
    gid1: str = Query(...),
    gid2: str = Query(...),
):
    account = _account(request)
    if not get_project(account["account_id"], project_id):
        return RedirectResponse("/", status_code=302)
    try:
        docs = [document_id_1]
        if document_id_2 and document_id_2 != document_id_1:
            docs.append(document_id_2)
        session_id = prepare_viewer_session_from_project_documents(account["account_id"], project_id, docs)
        slot_a = 1
        slot_b = 1 if document_id_2 == document_id_1 else 2
        url = (
            f"/viewer/clash/detail/?session_id={quote_plus(session_id)}&slot_a={slot_a}&slot_b={slot_b}"
            f"&gid1={quote_plus(gid1)}&gid2={quote_plus(gid2)}&project_id={quote_plus(project_id)}"
        )
        return RedirectResponse(url, status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/projects/{_e(project_id)}/clash?error={quote_plus(str(exc))}", status_code=303)


@project_clash_router.get("/projects/{project_id}/model/clash")
def legacy_project_model_clash_redirect(project_id: str):
    return RedirectResponse(f"/projects/{_e(project_id)}/clash", status_code=302)
