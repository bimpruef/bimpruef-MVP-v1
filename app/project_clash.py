"""
project_clash.py – eigenständiges Projektmodul Clash-Analyse

Die Clash-Analyse ist kein Bestandteil des Viewers mehr. Sie arbeitet
projektbasiert, lädt IFC/IFCZIP-Dateien aus dem Documents-Modul in den
technischen Viewer-Cache und speichert ausgewählte Clash-Zeilen als Issues.
"""

import html
import json
import os
from urllib.parse import quote_plus

import ifcopenshell
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_user
from app.document_storage import (
    list_project_ifc_documents,
    prepare_viewer_session_from_project_documents,
)
from app.extractors import get_candidate_products, get_psets_safe
from app.issue_storage import save_clash_issues
from app.project_storage import get_or_create_project_session, get_project
from app.project_ifc_cache import (
    document_index_by_slot,
    ensure_document_ifc_cache,
    get_cached_ifc_path,
    get_project_ifc_index,
)
from app.extractors import apply_filters, extract_element_data
from app.storage import get_ifc_label, get_ifc_path, get_session_slots, session_exists

project_clash_router = APIRouter()


def _e(value) -> str:
    return html.escape(str(value or ""))


def _account_from_request(request: Request) -> dict:
    user = require_user(request)
    return {
        "account_id": user["user_id"],
        "account_name": user["email"],
        "workspace": "Personal",
    }


def _load_context(request: Request, project_id: str):
    account = _account_from_request(request)
    project = get_project(account["account_id"], project_id)
    if not project:
        return None, None, None
    session_id = get_or_create_project_session(account["account_id"], project_id)
    return account, project, session_id


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


def _load_documents_panel(account: dict, project_id: str, slots: list[int], saved: str = "", error: str = "") -> str:
    docs = list_project_ifc_documents(account["account_id"], project_id)
    slot_hint = "Project IFC Index noch nicht aufgebaut."
    if slots:
        try:
            pidx = get_project_ifc_index(account["account_id"], project_id)
            slot_hint = "Project IFC Index: " + ", ".join(
                f"{i+1}: {_e(d.get('original_filename', ''))}"
                for i, d in enumerate(pidx.get("documents", []) or [])
            )
        except Exception:
            slot_hint = "Project IFC Index konnte noch nicht gelesen werden."

    flash = ""
    if error:
        flash = f'<div class="flash-err" style="margin-bottom:10px">⚠ {_e(error)}</div>'
    elif saved:
        flash = '<div class="flash-ok" style="margin-bottom:10px">✓ Project IFC Cache / Index wurde vorbereitet.</div>'

    if not docs:
        return f"""
        {flash}
        <div class="card" style="border-color:var(--accent2)">
          <h3 style="font-size:15px;margin-bottom:8px">Keine IFC-Dateien im Documents-Modul</h3>
          <p style="color:var(--muted);font-size:12px;margin-bottom:12px">
            Die Clash-Analyse liest ausschließlich aus Documents und dem Project IFC Cache.
          </p>
          <a class="btn btn-primary" href="/projects/{_e(project_id)}/documents" style="text-decoration:none">Zu Documents</a>
        </div>
        """

    rows = ""
    for d in docs:
        rows += f"""
        <tr>
          <td style="width:36px;text-align:center"><input type="checkbox" name="document_ids" value="{_e(d['document_id'])}" checked></td>
          <td style="font-weight:600;color:var(--accent)">{_e(d['original_filename'])}</td>
          <td>{_e(d['file_extension'])}</td>
          <td>{_fmt_size(d.get('file_size', 0))}</td>
          <td style="color:var(--muted)">{_e(d.get('folder_path') or 'Root')}</td>
        </tr>"""

    return f"""
    {flash}
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;margin-bottom:12px">
        <div>
          <h3 style="font-size:15px;margin-bottom:4px">IFC-Quelle: Documents → Project IFC Cache/Index</h3>
          <p style="color:var(--muted);font-size:12px">{slot_hint}</p>
        </div>
        <a class="btn" href="/projects/{_e(project_id)}/documents" style="text-decoration:none;font-size:12px">Documents öffnen</a>
      </div>
      <form method="POST" action="/projects/{_e(project_id)}/clash/load">
        <div style="overflow-x:auto;max-height:220px;overflow-y:auto">
          <table>
            <tr><th></th><th>Datei</th><th>Typ</th><th>Größe</th><th>Ordner</th></tr>
            {rows}
          </table>
        </div>
        <button class="btn btn-primary" type="submit" style="margin-top:12px">Project IFC Cache vorbereiten</button>
      </form>
    </div>
    """

def _clash_page_html(project: dict, account: dict, session_id: str, saved: str = "", error: str = "") -> HTMLResponse:
    from app.projects import _page, _project_subnav, _topbar_global

    project_id = project["project_id"]
    try:
        pidx = get_project_ifc_index(account["account_id"], project_id)
        project_docs = pidx.get("documents", []) or []
    except Exception:
        project_docs = []
    slots = list(range(1, len(project_docs) + 1))
    labels = {i + 1: d.get("original_filename", f"model_{i+1}.ifc") for i, d in enumerate(project_docs)}
    documents_panel = _load_documents_panel(account, project_id, slots, saved=saved, error=error)

    if not slots:
        body = f"""
        {_topbar_global(account)}
        {_project_subnav(project_id, "clash")}
        <div style="padding:28px 32px;max-width:1100px;margin:0 auto">
          <h1 style="font-size:22px;font-weight:600;margin-bottom:14px">Clash-Analyse</h1>
          {documents_panel}
        </div>
        """
        return _page(f"{project['project_name']} – Clash", body)

    def slot_checkboxes(group: str, default_slot: int) -> str:
        html_parts = []
        for s in slots:
            checked = "checked" if s == default_slot else ""
            html_parts.append(
                f'<label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px;padding:3px 0">'
                f'<input type="checkbox" class="slot-chk-{group}" value="{s}" {checked} style="accent-color:var(--accent);width:13px;height:13px">'
                f'<span>{_e(labels[s])}</span><span style="color:var(--muted);font-size:10px">Slot {s}</span></label>'
            )
        return "".join(html_parts)

    default_a = slots[0]
    default_b = slots[1] if len(slots) > 1 else slots[0]
    slots_a_html = slot_checkboxes("a", default_a)
    slots_b_html = slot_checkboxes("b", default_b)

    body = f"""
    {_topbar_global(account)}
    {_project_subnav(project_id, "clash")}
    <div style="padding:20px 24px;max-width:1400px;margin:0 auto">
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:14px">
        <div>
          <h1 style="font-size:22px;font-weight:600">Clash-Analyse</h1>
          <p style="color:var(--muted);font-size:13px;margin-top:4px">Eigenständiges Projektmodul. IFC-Quelle ist Documents; BCF-Export liegt im Issues-Modul.</p>
        </div>
        <a class="btn" href="/projects/{_e(project_id)}" style="text-decoration:none">← Projekt</a>
      </div>
      {documents_panel}

      <div style="display:grid;grid-template-columns:360px 1fr;gap:16px;align-items:start">
        <div>
          <div class="card">
            <h3 style="font-size:15px;margin-bottom:10px">Gruppe A</h3>
            <div style="margin-bottom:10px">{slots_a_html}</div>
            <div id="filters-a" style="display:flex;flex-direction:column;gap:6px"></div>
            <button class="btn" type="button" onclick="addFilter('a')" style="font-size:12px;margin-top:8px">+ Filter A</button>
          </div>
          <div class="card">
            <h3 style="font-size:15px;margin-bottom:10px">Gruppe B</h3>
            <div style="margin-bottom:10px">{slots_b_html}</div>
            <div id="filters-b" style="display:flex;flex-direction:column;gap:6px"></div>
            <button class="btn" type="button" onclick="addFilter('b')" style="font-size:12px;margin-top:8px">+ Filter B</button>
          </div>
          <div class="card">
            <label style="margin-top:0">Toleranz</label>
            <input id="inp-tolerance" type="number" step="0.001" value="0" style="margin-bottom:12px">
            <button id="btn-run" class="btn btn-primary" type="button" onclick="runClash()" style="width:100%">Analyse starten</button>
            <div id="run-status" style="font-size:12px;color:var(--muted);margin-top:10px"></div>
          </div>
        </div>
        <div id="clash-result"></div>
      </div>
    </div>

<script>
const PROJECT_ID = {json.dumps(project_id)};
const RUN_URL = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/run`;
const PSET_URL = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/pset-keys`;
const SAVE_URL = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/issues`;
const STATE_KEY = "project_clash_state_" + PROJECT_ID;
let lastClashes = [];
let filterRows = {{a: [], b: []}};
let psetKeys = {{a: [], b: []}};
const BASE_FIELDS = [
  ["type", "IFC-Typ"], ["name", "Name"], ["file_label", "Dateiname"],
  ["global_id", "GlobalId"], ["object_type", "ObjectType"], ["predefined_type", "PredefinedType"]
];
const OPERATORS = [
  ["contains", "enthält"], ["not_contains", "enthält nicht"], ["equals", "ist gleich"],
  ["not_equals", "ist ungleich"], ["starts_with", "beginnt mit"], ["ends_with", "endet mit"]
];
function esc(s) {{ return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }}
function collectSlots(group) {{ return [...document.querySelectorAll(`.slot-chk-${{group}}:checked`)].map(c => parseInt(c.value, 10)); }}
function fieldOptions(group) {{
  let html = BASE_FIELDS.map(([v,l]) => `<option value="${{v}}">${{esc(l)}}</option>`).join("");
  if (psetKeys[group] && psetKeys[group].length) {{
    html += '<option disabled>── Psets ──</option>';
    psetKeys[group].forEach(k => html += `<option value="${{esc(k)}}">${{esc(k.replace(/^pset:/,''))}}</option>`);
  }}
  return html;
}}
function opOptions() {{ return OPERATORS.map(([v,l]) => `<option value="${{v}}">${{esc(l)}}</option>`).join(""); }}
async function loadPsetKeys(group) {{
  const slots = collectSlots(group);
  if (!slots.length) return;
  try {{
    const r = await fetch(PSET_URL + `?slots=${{encodeURIComponent(slots.join(','))}}`);
    const data = await r.json();
    psetKeys[group] = data.pset_keys || [];
    filterRows[group].forEach(id => {{
      const row = document.getElementById(id); if (!row) return;
      const select = row.querySelector('.filter-field'); const old = select.value;
      select.innerHTML = fieldOptions(group); select.value = old;
    }});
  }} catch(e) {{}}
}}
window.addFilter = function(group) {{
  const id = `filter-${{group}}-${{Date.now()}}`;
  const div = document.createElement('div');
  div.id = id;
  div.style.cssText = 'display:grid;grid-template-columns:1fr 1fr 1.4fr auto;gap:4px;align-items:center';
  div.innerHTML = `<select class="filter-field" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px;border-radius:5px;font-size:11px">${{fieldOptions(group)}}</select>
<select class="filter-op" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px;border-radius:5px;font-size:11px">${{opOptions()}}</select>
<input class="filter-val" type="text" placeholder="Wert" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:5px;border-radius:5px;font-size:11px">
<button type="button" onclick="removeFilter('${{id}}','${{group}}')" style="background:none;border:none;color:var(--accent2);font-size:14px">✕</button>`;
  document.getElementById('filters-' + group).appendChild(div);
  filterRows[group].push(id);
}};
window.removeFilter = function(id, group) {{ const el = document.getElementById(id); if (el) el.remove(); filterRows[group] = filterRows[group].filter(x => x !== id); }};
function collectFilters(group) {{
  const out = [];
  filterRows[group].forEach(id => {{
    const row = document.getElementById(id); if (!row) return;
    const field = row.querySelector('.filter-field').value;
    const operator = row.querySelector('.filter-op').value;
    const value = row.querySelector('.filter-val').value.trim();
    if (field && value) out.push({{field, operator, value}});
  }});
  return out;
}}
window.runClash = async function() {{
  const btn = document.getElementById('btn-run');
  const status = document.getElementById('run-status');
  const result = document.getElementById('clash-result');
  const slots_a = collectSlots('a'); const slots_b = collectSlots('b');
  if (!slots_a.length || !slots_b.length) {{ status.innerHTML = '<span style="color:var(--accent2)">Bitte für beide Gruppen mindestens ein Modell wählen.</span>'; return; }}
  const tolerance = parseFloat(document.getElementById('inp-tolerance').value) || 0;
  btn.disabled = true; btn.textContent = 'Berechne …'; result.innerHTML = ''; status.textContent = '';
  try {{
    const r = await fetch(RUN_URL, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{tolerance, group_a:{{selected_slots:slots_a, filters:collectFilters('a')}}, group_b:{{selected_slots:slots_b, filters:collectFilters('b')}}}})}});
    const data = await r.json(); if (data.error) throw new Error(data.error);
    lastClashes = data.clashes || [];
    sessionStorage.setItem(STATE_KEY, JSON.stringify({{data, tolerance, slots_a, slots_b}}));
    renderResult(data, slots_a, slots_b);
  }} catch(e) {{ result.innerHTML = `<div class="flash-err">⚠ ${{esc(e.message)}}</div>`; }}
  finally {{ btn.disabled = false; btn.textContent = 'Analyse starten'; }}
}};
function renderResult(data, slots_a, slots_b) {{
  const result = document.getElementById('clash-result');
  const clashes = data.clashes || [];
  if (!clashes.length) {{ result.innerHTML = '<div class="flash-ok">✓ Keine Clashes gefunden.</div>'; return; }}
  let rows = '';
  clashes.forEach((c, idx) => {{
    const sa = c.slot_1 || slots_a[0]; const sb = c.slot_2 || slots_b[0];
    const gid1 = c.global_id_1 || ''; const gid2 = c.global_id_2 || '';
    const detail = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/detail?slot_a=${{sa}}&slot_b=${{sb}}&gid1=${{encodeURIComponent(gid1)}}&gid2=${{encodeURIComponent(gid2)}}`;
    rows += `<tr>
<td><input type="checkbox" class="issue-pick" value="${{idx}}"></td>
<td>${{idx+1}}</td>
<td><span class="tag tag-1">${{esc(c.type_1)}}</span> ${{esc(c.name_1)}}<div style="font-family:monospace;font-size:10px;color:var(--muted)">${{esc(gid1)}}</div></td>
<td><span class="tag tag-2">${{esc(c.type_2)}}</span> ${{esc(c.name_2)}}<div style="font-family:monospace;font-size:10px;color:var(--muted)">${{esc(gid2)}}</div></td>
<td><a class="btn" style="font-size:11px;padding:3px 8px;text-decoration:none" href="${{detail}}">3D-Ansicht</a></td>
</tr>`;
  }});
  result.innerHTML = `<div class="card"><div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:12px"><h3 style="font-size:15px">${{clashes.length}} Clashes gefunden</h3><div><button class="btn" onclick="toggleAllIssues(true)" type="button">Alle wählen</button> <button class="btn btn-primary" onclick="saveSelectedIssues()" type="button">Auswahl als Issues speichern</button></div></div><div style="overflow:auto;max-height:65vh"><table><tr><th></th><th>#</th><th>Element A</th><th>Element B</th><th>Aktion</th></tr>${{rows}}</table></div></div>`;
}}
window.toggleAllIssues = function(on) {{ document.querySelectorAll('.issue-pick').forEach(c => c.checked = !!on); }};
window.saveSelectedIssues = async function() {{
  const selected = [...document.querySelectorAll('.issue-pick:checked')].map(c => lastClashes[parseInt(c.value, 10)]).filter(Boolean);
  if (!selected.length) {{ alert('Bitte mindestens eine Clash-Zeile auswählen.'); return; }}
  const r = await fetch(SAVE_URL, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{clashes:selected}})}});
  const data = await r.json();
  if (data.error) {{ alert(data.error); return; }}
  alert(data.saved + ' Issue(s) gespeichert.');
  window.location.href = `/projects/${{encodeURIComponent(PROJECT_ID)}}/issues`;
}};
['a','b'].forEach(g => document.querySelectorAll('.slot-chk-' + g).forEach(c => c.addEventListener('change', () => loadPsetKeys(g))));
loadPsetKeys('a'); loadPsetKeys('b');
(function restore() {{ try {{ const raw = sessionStorage.getItem(STATE_KEY); if (!raw) return; const state = JSON.parse(raw); lastClashes = state.data.clashes || []; renderResult(state.data, state.slots_a || [], state.slots_b || []); }} catch(e) {{}} }})();
</script>
    """
    return _page(f"{project['project_name']} – Clash", body)


@project_clash_router.get("/projects/{project_id}/clash", response_class=HTMLResponse)
def project_clash_page(request: Request, project_id: str, saved: str = Query(default=""), error: str = Query(default="")):
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _clash_page_html(project, account, session_id, saved=saved, error=error)


@project_clash_router.post("/projects/{project_id}/clash/load")
def project_clash_load(request: Request, project_id: str, document_ids: list[str] = Form(default=[])):
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    try:
        for did in document_ids:
            ensure_document_ifc_cache(account["account_id"], project_id, did)
        return RedirectResponse(f"/projects/{_e(project_id)}/clash?saved=load", status_code=303)
    except Exception as exc:
        return RedirectResponse(f"/projects/{_e(project_id)}/clash?error={quote_plus(str(exc))}", status_code=303)


@project_clash_router.get("/projects/{project_id}/clash/pset-keys")
def project_clash_pset_keys(request: Request, project_id: str, slots: str = Query(default="")):
    account, project, _session_id = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)
    try:
        pidx = get_project_ifc_index(account["account_id"], project_id)
        selected_slots = [int(s) for s in slots.split(",") if s.strip()] if slots.strip() else list(range(1, len(pidx.get("documents", []) or []) + 1))
        pset_keys: set[str] = set()
        for slot in selected_slots:
            didx = document_index_by_slot(pidx, slot)
            path = didx.get("local_ifc_path")
            if not path or not os.path.exists(path):
                continue
            model = ifcopenshell.open(path)
            for elem in get_candidate_products(model):
                psets = get_psets_safe(elem)
                for pset_name, props in (psets or {}).items():
                    if isinstance(props, dict):
                        for prop_name in props:
                            if prop_name != "id":
                                pset_keys.add(f"pset:{pset_name}.{prop_name}")
        return JSONResponse({"pset_keys": sorted(pset_keys)})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@project_clash_router.post("/projects/{project_id}/clash/run")
async def project_clash_run(request: Request, project_id: str):
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)
    try:
        body = await request.json()
        tolerance = float(body.get("tolerance", 0.0))
        group_a = body.get("group_a", {})
        group_b = body.get("group_b", {})
        slots_a = [int(s) for s in group_a.get("selected_slots", [])]
        slots_b = [int(s) for s in group_b.get("selected_slots", [])]
        filters_a = group_a.get("filters", [])
        filters_b = group_b.get("filters", [])

        if not slots_a:
            return JSONResponse({"error": "Gruppe A: kein Modell ausgewählt."}, status_code=400)
        if not slots_b:
            return JSONResponse({"error": "Gruppe B: kein Modell ausgewählt."}, status_code=400)

        from app.clash import compare_element_groups_for_clashes

        elements_a = _load_project_group_elements(account["account_id"], project_id, slots_a, filters_a)
        elements_b = _load_project_group_elements(account["account_id"], project_id, slots_b, filters_b)
        clashes = compare_element_groups_for_clashes(elements_a, elements_b, tolerance=tolerance)
        return JSONResponse({"count_a": len(elements_a), "count_b": len(elements_b), "clashes": clashes})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@project_clash_router.post("/projects/{project_id}/clash/issues")
async def project_clash_save_issues(request: Request, project_id: str):
    account, project, _session_id = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)
    try:
        body = await request.json()
        saved = save_clash_issues(account["account_id"], project_id, body.get("clashes", []))
        return JSONResponse({"ok": True, "saved": len(saved), "issues": saved})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@project_clash_router.get("/projects/{project_id}/clash/detail", response_class=HTMLResponse)
def project_clash_detail(
    request: Request,
    project_id: str,
    slot_a: int = Query(...),
    slot_b: int = Query(...),
    gid1: str = Query(...),
    gid2: str = Query(...),
):
    from app.viewer import _brand_logo, _page, _slot_color, _viewer_js

    account, project, session_id = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    try:
        pidx = get_project_ifc_index(account["account_id"], project_id)
        didx_a = document_index_by_slot(pidx, slot_a)
        didx_b = document_index_by_slot(pidx, slot_b)
    except Exception as exc:
        return _page("Fehler", f'<div style="padding:40px;color:var(--accent2)"><h2>{_e(exc)}</h2></div>')

    sid = _e(session_id)
    label_a = _e(didx_a.get("original_filename", f"model_{slot_a}.ifc"))
    label_b = _e(didx_b.get("original_filename", f"model_{slot_b}.ifc"))
    col_a = _slot_color(slot_a)
    col_b = _slot_color(slot_b)
    if slot_a == slot_b:
        model_urls_js = f'{{url:"/viewer/file/?project_id={_e(project_id)}&document_id={_e(didx_a.get("document_id", ""))}",label:{json.dumps(label_a)},slot:{slot_a},color:{json.dumps(col_a)}}}'
    else:
        model_urls_js = (
            f'{{url:"/viewer/file/?project_id={_e(project_id)}&document_id={_e(didx_a.get("document_id", ""))}",label:{json.dumps(label_a)},slot:{slot_a},color:{json.dumps(col_a)}}},'
            f'{{url:"/viewer/file/?project_id={_e(project_id)}&document_id={_e(didx_b.get("document_id", ""))}",label:{json.dumps(label_b)},slot:{slot_b},color:{json.dumps(col_b)}}}'
        )
    back_url = f"/projects/{_e(project_id)}/clash"
    body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">
  <div style="display:flex;align-items:center;gap:6px;padding:6px 14px;background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">
    {_brand_logo(24)}
    <a href="{back_url}" class="btn" style="font-size:12px;text-decoration:none;margin-left:6px">← Clash-Liste</a>
    <span style="margin-left:10px;font-size:12px;color:var(--muted)">Nur die beiden Clash-Elemente sind hervorgehoben</span>
    <div style="margin-left:auto;display:flex;gap:4px">
      <button id="btn-fit" class="btn" style="font-size:11px;padding:4px 9px">⊡ Einpassen</button>
      <button id="btn-reset" class="btn" style="font-size:11px;padding:4px 9px">⟳ Kamera</button>
    </div>
  </div>
  <div style="display:flex;flex:1;overflow:hidden">
    <div style="width:240px;min-width:240px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;color:#8ab;text-transform:uppercase;letter-spacing:.7px">Clash-Elemente</div>
      <div style="padding:10px;font-size:11px">
        <div style="margin-bottom:10px"><span class="tag tag-1">A</span><div style="font-family:monospace;color:var(--muted);word-break:break-all;margin-top:4px">{_e(gid1)}</div></div>
        <div><span class="tag tag-2">B</span><div style="font-family:monospace;color:var(--muted);word-break:break-all;margin-top:4px">{_e(gid2)}</div></div>
      </div>
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0"><div style="padding:8px 10px;font-size:11px;color:var(--muted)">Wird geladen …</div></div>
    </div>
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden">
      <canvas id="three-canvas" style="width:100%!important;height:100%!important;display:block"></canvas>
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;background:rgba(14,14,26,.93);z-index:20">
        <div style="width:40px;height:40px;border:4px solid #0f3460;border-top-color:var(--accent2);border-radius:50%;animation:spin .7s linear infinite;margin-bottom:12px"></div>
        <p id="load-txt" style="color:#889;font-size:13px">Clash-Elemente werden geladen …</p>
      </div>
    </div>
    <div id="info-panel" style="width:300px;min-width:300px;background:var(--surface);border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;flex-shrink:0">
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;color:#8ab;text-transform:uppercase;letter-spacing:.7px;display:flex;justify-content:space-between"><span>Element-Info</span><span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px">✕</span></div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:10px;font-size:12px"><div style="color:var(--muted);font-style:italic">Klick auf ein Element für Details.</div></div>
    </div>
  </div>
</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>{_viewer_js(model_urls_js, highlight_gids=[gid1, gid2], session_id=session_id)}</script>
"""
    return _page("Clash-Detail – BIMPruef", body)
