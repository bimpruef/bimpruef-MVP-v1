"""
project_rulecheck.py – BIMPruef Rule-Check als eigenständiges Projektmodul

Dieses Modul löst den bisherigen /viewer/rulecheck/-Pfad ab.
Die Rule-Check (Checking) ist kein Bestandteil des Viewers mehr.
Sie lädt IFC/IFCZIP-Dateien ausschließlich aus dem Documents-Modul –
identisches Muster wie project_clash.py.

Routen:
  GET  /projects/{project_id}/checking          → Rule-Check UI (Konfiguration + Ergebnisse)
  POST /projects/{project_id}/checking/load     → Modelle aus Documents in Cache laden
  POST /projects/{project_id}/checking/run      → Regelprüfung ausführen (JSON-API)
  GET  /projects/{project_id}/checking/export   → Ergebnisse als JSON exportieren

Legacy-Redirect:
  GET  /viewer/rulecheck/  →  /projects/{project_id}/checking  (falls project_id bekannt)
"""

import html
import json
import os
from urllib.parse import quote_plus

import ifcopenshell
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import require_user
from app.document_storage import (
    list_project_ifc_documents,
    prepare_viewer_session_from_project_documents,
)
from app.project_storage import get_or_create_project_session, get_project
from app.project_ifc_cache import (
    document_index_by_slot,
    ensure_document_ifc_cache,
    get_cached_ifc_path,
    get_project_ifc_index,
)
from app.rulecheck import ALL_RULES, _RULE_FUNCTIONS, run_rules_on_model
from app.storage import get_ifc_label, get_ifc_path, get_session_slots, session_exists

project_rulecheck_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    return html.escape(str(s or ""))


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


# ─────────────────────────────────────────────────────────────────────────────
# Documents-Panel (Modelle aus Documents für Checking laden)
# ─────────────────────────────────────────────────────────────────────────────

def _documents_panel(account: dict, project_id: str, slots: list[int],
                      session_id: str, saved: str = "", error: str = "") -> str:
    docs = list_project_ifc_documents(account["account_id"], project_id)

    if slots:
        try:
            pidx = get_project_ifc_index(account["account_id"], project_id)
            slot_hint = "Project IFC Index: " + ", ".join(
                f"{i+1}: {_e(d.get('original_filename', ''))}"
                for i, d in enumerate(pidx.get("documents", []) or [])
            )
        except Exception:
            slot_hint = "Project IFC Index konnte noch nicht gelesen werden."
    else:
        slot_hint = "Project IFC Index noch nicht aufgebaut."

    flash = ""
    if error:
        flash = f'<div class="flash-err" style="margin-bottom:10px">⚠ {_e(error)}</div>'
    elif saved:
        flash = '<div class="flash-ok" style="margin-bottom:10px">✓ Modelle wurden aus Documents für die Prüfung geladen.</div>'

    if not docs:
        return f"""
        {flash}
        <div class="card" style="border-color:var(--accent2)">
          <h3 style="font-size:15px;margin-bottom:8px">Keine IFC-Dateien im Documents-Modul</h3>
          <p style="color:var(--muted);font-size:12px;margin-bottom:12px">
            Die Rule-Check liest keine Viewer-Uploads mehr. Lade zuerst .ifc oder .ifczip
            im Documents-Modul hoch.
          </p>
          <a class="btn btn-primary" href="/projects/{_e(project_id)}/documents"
             style="text-decoration:none">Zu Documents</a>
        </div>
        """

    rows = ""
    for d in docs:
        rows += f"""
        <tr>
          <td style="width:36px;text-align:center">
            <input type="checkbox" name="document_ids" value="{_e(d['document_id'])}" checked>
          </td>
          <td style="font-weight:600;color:var(--accent)">{_e(d['original_filename'])}</td>
          <td>{_e(d['file_extension'])}</td>
          <td>{_fmt_size(d.get('file_size', 0))}</td>
          <td style="color:var(--muted)">{_e(d.get('folder_path') or 'Root')}</td>
        </tr>"""

    return f"""
    {flash}
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;
                  gap:16px;margin-bottom:12px">
        <div>
          <h3 style="font-size:15px;margin-bottom:4px">IFC-Quelle: Documents</h3>
          <p style="color:var(--muted);font-size:12px">{slot_hint}</p>
        </div>
        <a class="btn" href="/projects/{_e(project_id)}/documents"
           style="text-decoration:none;font-size:12px">Documents öffnen</a>
      </div>
      <form method="POST" action="/projects/{_e(project_id)}/checking/load">
        <div style="overflow-x:auto;max-height:220px;overflow-y:auto">
          <table>
            <tr><th></th><th>Datei</th><th>Typ</th><th>Größe</th><th>Ordner</th></tr>
            {rows}
          </table>
        </div>
        <button class="btn btn-primary" type="submit" style="margin-top:12px">
          Ausgewählte Modelle für Prüfung laden
        </button>
      </form>
    </div>
    """


# ─────────────────────────────────────────────────────────────────────────────
# Haupt-UI-Render
# ─────────────────────────────────────────────────────────────────────────────

def _checking_page(project: dict, account: dict, session_id: str,
                   saved: str = "", error: str = "") -> HTMLResponse:
    from app.projects import _page, _project_subnav, _topbar_global

    project_id = project["project_id"]
    try:
        pidx = get_project_ifc_index(account["account_id"], project_id)
        project_docs = pidx.get("documents", []) or []
    except Exception:
        project_docs = []
    slots = list(range(1, len(project_docs) + 1))
    slot_labels = {i + 1: d.get("original_filename", f"model_{i+1}.ifc") for i, d in enumerate(project_docs)}
    docs_panel = _documents_panel(account, project_id, slots, session_id,
                                  saved=saved, error=error)

    # ── Slot-Auswahl-HTML ────────────────────────────────────────────────────
    if not slots:
        slots_html = (
            '<div class="flash-err">'
            'Keine IFC-Dateien geladen. Wähle oben Modelle aus Documents.'
            '</div>'
        )
    else:
        slots_html = '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px">'
        for s in slots:
            label = _e(slot_labels.get(s, f"model_{s}.ifc"))
            slots_html += (
                f'<label style="display:flex;align-items:center;gap:6px;'
                f'background:var(--surface2);border:1px solid var(--border);'
                f'border-radius:8px;padding:8px 14px;font-size:13px;cursor:pointer">'
                f'<input type="checkbox" class="slot-cb" value="{s}" checked> '
                f'Slot&nbsp;{s}&nbsp;–&nbsp;<strong>{label}</strong>'
                f'</label>'
            )
        slots_html += '</div>'

    # ── Regel-Auswahl-HTML ──────────────────────────────────────────────────
    rules_html = '<div style="display:flex;flex-direction:column;gap:8px">'
    SEV_LABEL = {"error": "Fehler", "warning": "Warnung", "info": "Hinweis"}
    SEV_CLS   = {"error": "badge-error", "warning": "badge-warning", "info": "badge-info"}
    for rule in ALL_RULES:
        sev     = rule["severity"]
        badge_c = SEV_CLS.get(sev, "")
        badge_l = SEV_LABEL.get(sev, sev)
        rules_html += (
            f'<label style="display:flex;align-items:flex-start;gap:10px;'
            f'background:var(--surface2);border:1px solid var(--border);'
            f'border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer">'
            f'<input type="checkbox" class="rule-cb" value="{_e(rule["id"])}" checked '
            f'style="margin-top:2px">'
            f'<div>'
            f'<div style="font-weight:600">{_e(rule["label"])} '
            f'<span class="badge {badge_c}">{badge_l}</span></div>'
            f'<div style="color:var(--muted);font-size:11px;margin-top:2px">'
            f'{_e(rule["description"])}</div>'
            f'</div>'
            f'</label>'
        )
    rules_html += '</div>'

    no_slots_warning = ""
    if not slots:
        no_slots_warning = (
            '<div class="flash-err" style="margin-bottom:16px">'
            'Bitte zuerst Modelle aus Documents laden (siehe oben).'
            '</div>'
        )

    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "checking")}

<div style="padding:20px 24px;max-width:1100px;margin:0 auto">

  <div style="display:flex;align-items:flex-start;justify-content:space-between;
              gap:16px;margin-bottom:14px">
    <div>
      <h1 style="font-size:22px;font-weight:600">Rule-Check (Checking)</h1>
      <p style="color:var(--muted);font-size:13px;margin-top:4px">
        Eigenständiges Projektmodul. IFC-Quelle ist Documents; kein Viewer-Upload.
      </p>
    </div>
    <a class="btn" href="/projects/{_e(project_id)}" style="text-decoration:none">← Projekt</a>
  </div>

  {docs_panel}

  {no_slots_warning}

  <!-- Slot-Auswahl -->
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="font-size:15px">Modell-Slots</h2>
    </div>
    <div id="slot-selection">{slots_html}</div>
  </div>

  <!-- Regelauswahl -->
  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <h2 style="font-size:15px">Regeln</h2>
      <div style="display:flex;gap:8px">
        <button type="button" class="btn" style="font-size:11px;padding:4px 10px"
          onclick="toggleAll(true)">Alle aktivieren</button>
        <button type="button" class="btn" style="font-size:11px;padding:4px 10px"
          onclick="toggleAll(false)">Alle deaktivieren</button>
      </div>
    </div>
    <div id="rule-selection">{rules_html}</div>
  </div>

  <!-- Start-Button -->
  <div style="margin-bottom:24px">
    <button id="btn-run" class="btn btn-primary"
            style="font-size:14px;padding:10px 28px" onclick="runCheck()"
            {"disabled" if not slots else ""}>
      ▶ Prüfung starten
    </button>
    <span id="run-status" style="margin-left:14px;font-size:12px;color:var(--muted)"></span>
  </div>

  <!-- Ergebnis-Bereich -->
  <div id="result-area" style="display:none">

    <div class="card" id="result-summary"></div>

    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap" id="sev-filter">
      <button class="btn" data-sev="all"     onclick="filterSev('all')"     style="font-size:12px">Alle</button>
      <button class="btn" data-sev="error"   onclick="filterSev('error')"   style="font-size:12px">Nur Fehler</button>
      <button class="btn" data-sev="warning" onclick="filterSev('warning')" style="font-size:12px">Nur Warnungen</button>
      <button class="btn" data-sev="info"    onclick="filterSev('info')"    style="font-size:12px">Nur Hinweise</button>
      <a id="btn-export" href="#" style="display:none;margin-left:auto" class="btn"
         style="font-size:12px">⬇ JSON exportieren</a>
    </div>

    <div style="overflow-x:auto">
      <table id="result-table">
        <thead>
          <tr>
            <th>#</th>
            <th>Schwere</th>
            <th>Regel</th>
            <th>Slot</th>
            <th>Datei</th>
            <th>IFC-Typ</th>
            <th>Name</th>
            <th>GlobalId</th>
            <th>Meldung</th>
          </tr>
        </thead>
        <tbody id="result-tbody"></tbody>
      </table>
    </div>
  </div>
</div>

<script>
const PROJECT_ID  = {json.dumps(project_id)};
const RUN_URL     = `/projects/${{encodeURIComponent(PROJECT_ID)}}/checking/run`;
const EXPORT_BASE = `/projects/${{encodeURIComponent(PROJECT_ID)}}/checking/export`;
let _allResults   = [];
let _currentFilter = 'all';

function toggleAll(state) {{
  document.querySelectorAll('.rule-cb').forEach(cb => cb.checked = state);
}}

function severityLabel(sev) {{
  return {{error:'Fehler', warning:'Warnung', info:'Hinweis'}}[sev] || sev;
}}
function severityClass(sev) {{
  return {{error:'sev-error', warning:'sev-warning', info:'sev-info'}}[sev] || '';
}}
function esc(s) {{
  const d = document.createElement('div');
  d.textContent = s ?? '';
  return d.innerHTML;
}}

function renderTable(results) {{
  const tbody = document.getElementById('result-tbody');
  if (!results.length) {{
    tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted);padding:24px">Keine Ergebnisse.</td></tr>';
    return;
  }}
  tbody.innerHTML = results.map((r, i) =>
    `<tr data-sev="${{esc(r.severity)}}">
      <td>${{i + 1}}</td>
      <td><span class="${{severityClass(r.severity)}}">${{esc(severityLabel(r.severity))}}</span></td>
      <td style="font-size:11px;color:var(--muted)">${{esc(r.rule_id)}}</td>
      <td>${{esc(r.slot)}}</td>
      <td style="max-width:160px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
          title="${{esc(r.file_label)}}">${{esc(r.file_label)}}</td>
      <td>${{esc(r.ifc_type)}}</td>
      <td>${{esc(r.name)}}</td>
      <td style="font-family:monospace;font-size:10px">${{esc(r.global_id)}}</td>
      <td style="max-width:340px">${{esc(r.message)}}</td>
    </tr>`
  ).join('');
}}

function filterSev(sev) {{
  _currentFilter = sev;
  document.querySelectorAll('#sev-filter .btn').forEach(b => {{
    b.style.borderColor = b.dataset.sev === sev ? 'var(--accent)' : '';
  }});
  renderTable(sev === 'all' ? _allResults : _allResults.filter(r => r.severity === sev));
}}

async function runCheck() {{
  const slots = [...document.querySelectorAll('.slot-cb:checked')].map(cb => parseInt(cb.value));
  const rules = [...document.querySelectorAll('.rule-cb:checked')].map(cb => cb.value);

  if (!slots.length) {{ alert('Bitte mindestens einen Slot auswählen.'); return; }}
  if (!rules.length) {{ alert('Bitte mindestens eine Regel auswählen.'); return; }}

  const btn    = document.getElementById('btn-run');
  const status = document.getElementById('run-status');
  btn.disabled = true;
  status.textContent = 'Prüfung läuft …';
  document.getElementById('result-area').style.display = 'none';

  try {{
    const resp = await fetch(RUN_URL, {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{slots, rules}}),
    }});

    let data;
    try {{ data = await resp.json(); }}
    catch (_) {{
      alert(`HTTP ${{resp.status}} – Antwort konnte nicht geparst werden.`);
      status.textContent = '';
      btn.disabled = false;
      return;
    }}

    if (!resp.ok || data.error) {{
      alert('Fehler: ' + (data.error || data.detail || `HTTP ${{resp.status}}`));
      status.textContent = '';
      btn.disabled = false;
      return;
    }}

    _allResults = data.results || [];
    const counts = data.summary || {{}};

    document.getElementById('result-summary').innerHTML = `
      <h2 style="font-size:15px;margin-bottom:12px">Ergebnis-Zusammenfassung</h2>
      <div style="display:flex;gap:14px;flex-wrap:wrap">
        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;
          padding:10px 20px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:var(--text)">${{counts.total ?? 0}}</div>
          <div style="font-size:11px;color:var(--muted)">Gesamt</div>
        </div>
        <div style="background:#2a0a10;border:1px solid #6e1a2e;border-radius:8px;
          padding:10px 20px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#ef9a9a">${{counts.errors ?? 0}}</div>
          <div style="font-size:11px;color:var(--muted)">Fehler</div>
        </div>
        <div style="background:#3e2800;border:1px solid #6e4800;border-radius:8px;
          padding:10px 20px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#ffb74d">${{counts.warnings ?? 0}}</div>
          <div style="font-size:11px;color:var(--muted)">Warnungen</div>
        </div>
        <div style="background:#0d2a3e;border:1px solid #1a4a6e;border-radius:8px;
          padding:10px 20px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#64b5f6">${{counts.infos ?? 0}}</div>
          <div style="font-size:11px;color:var(--muted)">Hinweise</div>
        </div>
        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:8px;
          padding:10px 20px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:var(--success)">${{counts.slots_checked ?? 0}}</div>
          <div style="font-size:11px;color:var(--muted)">Slots geprüft</div>
        </div>
      </div>`;

    const exportParams = new URLSearchParams({{
      slots: slots.join(','),
      rules: rules.join(','),
    }});
    const exportBtn = document.getElementById('btn-export');
    exportBtn.href  = EXPORT_BASE + '?' + exportParams.toString();
    exportBtn.style.display = '';

    document.getElementById('result-area').style.display = '';
    filterSev('all');
    status.textContent = `Fertig – ${{_allResults.length}} Befunde.`;
  }} catch (err) {{
    alert('Netzwerkfehler: ' + err.message);
    status.textContent = '';
  }} finally {{
    btn.disabled = false;
  }}
}}
</script>
"""
    return _page(f"{project['project_name']} – Checking", body)


# ─────────────────────────────────────────────────────────────────────────────
# Routen
# ─────────────────────────────────────────────────────────────────────────────

@project_rulecheck_router.get("/projects/{project_id}/checking", response_class=HTMLResponse)
def project_checking_page(
    request: Request,
    project_id: str,
    saved: str = Query(default=""),
    error: str = Query(default=""),
):
    """Hauptseite: Rule-Check Konfiguration + Ergebnisanzeige."""
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _checking_page(project, account, session_id, saved=saved, error=error)


@project_rulecheck_router.post("/projects/{project_id}/checking/load")
def project_checking_load(
    request: Request,
    project_id: str,
    document_ids: list[str] = Form(default=[]),
):
    """Modelle aus Documents in den Checking-Cache (Viewer-Session) laden."""
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    try:
        for did in document_ids:
            ensure_document_ifc_cache(account["account_id"], project_id, did)
        return RedirectResponse(
            f"/projects/{_e(project_id)}/checking?saved=load",
            status_code=303,
        )
    except Exception as exc:
        return RedirectResponse(
            f"/projects/{_e(project_id)}/checking?error={quote_plus(str(exc))}",
            status_code=303,
        )


@project_rulecheck_router.post("/projects/{project_id}/checking/run")
async def project_checking_run(request: Request, project_id: str):
    """
    Regelprüfung ausführen.

    Request-Body (JSON):
      { "slots": [1, 2], "rules": ["missing_names", ...] }

    Response (JSON):
      { "summary": {...}, "results": [...] }
    """
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiges JSON."}, status_code=400)

    slots = body.get("slots", [])
    rules = body.get("rules", [])

    if not slots:
        return JSONResponse({"error": "Keine Slots angegeben."}, status_code=400)
    if not rules:
        return JSONResponse({"error": "Keine Regeln angegeben."}, status_code=400)

    try:
        slots = [int(s) for s in slots]
    except (TypeError, ValueError):
        return JSONResponse({"error": "Ungültige Slot-Angabe."}, status_code=400)

    pidx = get_project_ifc_index(account["account_id"], project_id)
    available_slots = list(range(1, len(pidx.get("documents", []) or []) + 1))
    slots = [s for s in slots if s in available_slots]
    if not slots:
        return JSONResponse({"error": "Keine der angegebenen Dokumente sind im Project IFC Index vorhanden."}, status_code=400)

    all_results:   list = []
    slots_checked: int  = 0
    slot_errors:   list = []

    for slot in slots:
        try:
            didx = document_index_by_slot(pidx, slot)
            ifc_path = get_cached_ifc_path(account["account_id"], project_id, didx["document_id"])
        except Exception as exc:
            slot_errors.append(f"Dokument {slot}: Datei nicht gefunden – {exc}")
            continue

        file_label = didx.get("original_filename", f"model_{slot}.ifc")
        try:
            model = ifcopenshell.open(ifc_path)
        except Exception as exc:
            slot_errors.append(f"Slot {slot}: IFC konnte nicht geöffnet werden – {exc}")
            continue

        results = run_rules_on_model(model, slot=slot, file_label=file_label, rules=rules)
        all_results.extend(results)
        slots_checked += 1

    errors   = sum(1 for r in all_results if r["severity"] == "error")
    warnings = sum(1 for r in all_results if r["severity"] == "warning")
    infos    = sum(1 for r in all_results if r["severity"] == "info")

    response_body: dict = {
        "summary": {
            "total":         len(all_results),
            "errors":        errors,
            "warnings":      warnings,
            "infos":         infos,
            "slots_checked": slots_checked,
        },
        "results": all_results,
    }
    if slot_errors:
        response_body["slot_errors"] = slot_errors

    return JSONResponse(response_body)


@project_rulecheck_router.get("/projects/{project_id}/checking/export")
def project_checking_export(
    request: Request,
    project_id: str,
    slots: str = Query(default=""),
    rules: str = Query(default=""),
):
    """Ergebnisse als JSON-Datei exportieren."""
    account, project, session_id = _load_context(request, project_id)
    if not project:
        return Response(content="Projekt nicht gefunden.", status_code=404)
    slot_list: list[int] = []
    for s in slots.split(","):
        s = s.strip()
        if s.isdigit():
            slot_list.append(int(s))

    rule_list = [r.strip() for r in rules.split(",") if r.strip()]

    pidx = get_project_ifc_index(account["account_id"], project_id)
    if not slot_list:
        slot_list = list(range(1, len(pidx.get("documents", []) or []) + 1))
    if not rule_list:
        rule_list = list(_RULE_FUNCTIONS.keys())

    all_results: list = []
    for slot in slot_list:
        try:
            didx = document_index_by_slot(pidx, slot)
            ifc_path = get_cached_ifc_path(account["account_id"], project_id, didx["document_id"])
        except Exception:
            continue
        file_label = didx.get("original_filename", f"model_{slot}.ifc")
        try:
            model   = ifcopenshell.open(ifc_path)
            results = run_rules_on_model(model, slot=slot, file_label=file_label, rules=rule_list)
            all_results.extend(results)
        except Exception:
            continue

    export_data = {
        "project_id":   project_id,
        "project_name": project.get("project_name", ""),
        "summary": {
            "total":    len(all_results),
            "errors":   sum(1 for r in all_results if r["severity"] == "error"),
            "warnings": sum(1 for r in all_results if r["severity"] == "warning"),
            "infos":    sum(1 for r in all_results if r["severity"] == "info"),
        },
        "results": all_results,
    }

    content = json.dumps(export_data, ensure_ascii=False, indent=2, default=str)
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="rulecheck_export.json"'},
    )
