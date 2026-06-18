"""
project_clash.py – eigenständiges Projektmodul Clash-Analyse
"""

import html
import io
import json
import os
import tempfile
import zipfile
from urllib.parse import quote_plus

import ifcopenshell
import ifcopenshell.geom
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import require_user
from app.document_storage import (
    get_document,
    list_project_ifc_documents,
)
from app.extractors import apply_filters, extract_element_data, get_candidate_products, get_psets_safe
from app.issue_storage import save_clash_issues
from app.project_storage import get_project

try:
    from app.r2_storage import download_file_from_r2, r2_enabled
except Exception:
    download_file_from_r2 = None
    r2_enabled = lambda: False

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
        return None, None
    return account, project


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


def _open_ifc_from_document(account_id: str, project_id: str, document_id: str):
    if not (r2_enabled() and download_file_from_r2):
        raise ValueError("Cloudflare R2 ist nicht konfiguriert.")

    doc = get_document(account_id, project_id, document_id)
    label = doc.get("original_filename", document_id)
    ext = (doc.get("file_extension") or ".ifc").lower()

    fd, tmp_path = tempfile.mkstemp(prefix="bp_clash_", suffix=ext)
    os.close(fd)

    try:
        download_file_from_r2(doc["r2_key"], tmp_path)

        if ext == ".ifczip":
            with open(tmp_path, "rb") as f:
                raw = f.read()
            with zipfile.ZipFile(io.BytesIO(raw), "r") as zf:
                ifc_names = [n for n in zf.namelist() if n.lower().endswith(".ifc")]
                if not ifc_names:
                    raise ValueError(f"Keine .ifc-Datei in '{label}' gefunden.")
                ifc_bytes = zf.read(ifc_names[0])

            fd2, ifc_tmp = tempfile.mkstemp(prefix="bp_clash_ifc_", suffix=".ifc")
            os.close(fd2)
            with open(ifc_tmp, "wb") as f:
                f.write(ifc_bytes)
            try:
                model = ifcopenshell.open(ifc_tmp)
            finally:
                try:
                    os.remove(ifc_tmp)
                except OSError:
                    pass
        else:
            model = ifcopenshell.open(tmp_path)

    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    return model, label


def _extract_elements_from_model(model, file_label: str, document_id: str, filters: list) -> list:
    all_elements = []
    for elem in get_candidate_products(model):
        data = extract_element_data(elem, file_label=file_label)
        data["document_id"] = document_id
        data["_ifc_element"] = elem
        all_elements.append(data)

    return apply_filters(all_elements, filters)


def _make_geometry_settings() -> ifcopenshell.geom.settings:
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    return settings


def _get_bbox(element, settings) -> dict | None:
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
        verts = shape.geometry.verts
        if not verts or len(verts) < 3:
            return None
        xs = verts[0::3]
        ys = verts[1::3]
        zs = verts[2::3]
        return {
            "min_x": min(xs), "min_y": min(ys), "min_z": min(zs),
            "max_x": max(xs), "max_y": max(ys), "max_z": max(zs),
        }
    except Exception:
        return None


def _bboxes_intersect(b1: dict, b2: dict, tolerance: float = 0.0) -> bool:
    if b1 is None or b2 is None:
        return False
    return (
        b1["min_x"] <= b2["max_x"] + tolerance
        and b1["max_x"] >= b2["min_x"] - tolerance
        and b1["min_y"] <= b2["max_y"] + tolerance
        and b1["max_y"] >= b2["min_y"] - tolerance
        and b1["min_z"] <= b2["max_z"] + tolerance
        and b1["max_z"] >= b2["min_z"] - tolerance
    )


def compare_element_groups_for_clashes(
    group_a: list,
    group_b: list,
    tolerance: float = 0.0,
) -> list:
    settings = _make_geometry_settings()

    def _attach_bboxes(group: list) -> list:
        result = []
        for elem_data in group:
            ifc_elem = elem_data.get("_ifc_element")
            if ifc_elem is None:
                continue
            bbox = _get_bbox(ifc_elem, settings)
            if bbox is not None:
                result.append((elem_data, bbox))
        return result

    group_a_bboxes = _attach_bboxes(group_a)
    group_b_bboxes = _attach_bboxes(group_b)

    clashes: list = []
    seen_pairs: set = set()

    for data_a, bbox_a in group_a_bboxes:
        gid_a = data_a.get("global_id") or ""
        doc_a = data_a.get("document_id") or ""

        for data_b, bbox_b in group_b_bboxes:
            gid_b = data_b.get("global_id") or ""
            doc_b = data_b.get("document_id") or ""

            if doc_a == doc_b and gid_a == gid_b:
                continue

            if doc_a < doc_b or (doc_a == doc_b and gid_a <= gid_b):
                pair_key = (doc_a, gid_a, doc_b, gid_b)
            else:
                pair_key = (doc_b, gid_b, doc_a, gid_a)

            if pair_key in seen_pairs:
                continue

            if _bboxes_intersect(bbox_a, bbox_b, tolerance=tolerance):
                seen_pairs.add(pair_key)
                clashes.append({
                    "type_1":        data_a.get("type", ""),
                    "name_1":        data_a.get("name", ""),
                    "global_id_1":   gid_a,
                    "express_id_1":  str(data_a.get("express_id", "")),
                    "file_label_1":  data_a.get("file_label", ""),
                    "document_id_1": doc_a,
                    "type_2":        data_b.get("type", ""),
                    "name_2":        data_b.get("name", ""),
                    "global_id_2":   gid_b,
                    "express_id_2":  str(data_b.get("express_id", "")),
                    "file_label_2":  data_b.get("file_label", ""),
                    "document_id_2": doc_b,
                })

    return clashes


def _clash_page_html(project: dict, account: dict) -> HTMLResponse:
    from app.projects import _page, _project_subnav, _topbar_global

    project_id = project["project_id"]
    pid = _e(project_id)
    account_id = account["account_id"]

    docs = list_project_ifc_documents(account_id, project_id)

    if not docs:
        body = f"""
        {_topbar_global(account)}
        {_project_subnav(project_id, "clash")}
        <div style="padding:28px 32px;max-width:900px;margin:0 auto">
          <h1 style="font-size:22px;font-weight:600;margin-bottom:14px">Clash-Analyse</h1>
          <div class="card" style="border-color:var(--accent2)">
            <h3 style="font-size:15px;margin-bottom:8px">Keine IFC-Dateien im Documents-Modul</h3>
            <p style="color:var(--muted);font-size:13px;margin-bottom:14px">
              Die Clash-Analyse lädt IFC-Dateien direkt aus dem Documents-Modul.
              Lade zuerst mindestens eine .ifc- oder .ifczip-Datei dort hoch.
            </p>
            <a class="btn btn-primary" href="/projects/{pid}/documents"
               style="text-decoration:none">Zu Documents</a>
          </div>
        </div>
        """
        return _page(f"{project['project_name']} – Clash", body)

    doc_options = ""
    for d in docs:
        size_label = _fmt_size(d.get("file_size", 0))
        folder = d.get("folder_path") or "Root"
        doc_options += (
            f'<option value="{_e(d["document_id"])}" '
            f'data-label="{_e(d["original_filename"])}">'
            f'{_e(d["original_filename"])} — {_e(folder)} ({size_label})'
            f'</option>'
        )

    body = f"""
{_topbar_global(account)}
{_project_subnav(project_id, "clash")}

<div style="padding:20px 24px;max-width:1400px;margin:0 auto">

  <div style="display:flex;align-items:flex-start;justify-content:space-between;
              gap:16px;margin-bottom:18px">
    <div>
      <h1 style="font-size:22px;font-weight:600">Clash-Analyse</h1>
      <p style="color:var(--muted);font-size:13px;margin-top:4px">
        Dateien werden direkt aus Documents geladen — kein Slot-Cache erforderlich.
      </p>
    </div>
    <a class="btn" href="/projects/{pid}" style="text-decoration:none">← Projekt</a>
  </div>

  <div style="display:grid;grid-template-columns:400px 1fr;gap:16px;align-items:start">

    <div style="display:flex;flex-direction:column;gap:12px">

      <div class="card" id="card-group-a">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <div style="width:10px;height:10px;border-radius:50%;background:#4fc3f7;flex-shrink:0"></div>
          <h3 style="font-size:15px;font-weight:600">Gruppe A — Datei</h3>
        </div>
        <label style="font-size:12px;color:var(--muted);margin-bottom:4px;display:block">IFC-Datei auswählen</label>
        <select id="sel-doc-a"
          style="width:100%;background:var(--surface2);border:1px solid var(--border);
                 color:var(--text);padding:8px 10px;border-radius:7px;font-size:13px;margin-bottom:12px;cursor:pointer">
          <option value="">— Datei wählen —</option>
          {doc_options}
        </select>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <span style="font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.5px">Filter A</span>
          <div style="display:flex;gap:6px;align-items:center">
            <button type="button" id="btn-load-psets-a" onclick="loadPsetKeys('a')"
              class="btn" style="font-size:10px;padding:3px 9px;color:var(--accent)">Pset-Felder laden</button>
            <button type="button" onclick="addFilter('a')"
              class="btn" style="font-size:10px;padding:3px 9px">+ Hinzufügen</button>
          </div>
        </div>
        <div id="filters-a" style="display:flex;flex-direction:column;gap:6px"></div>
        <div id="no-filter-hint-a" style="font-size:11px;color:var(--muted);font-style:italic;padding:4px 0">
          Kein Filter – alle Elemente werden geprüft.
        </div>
      </div>

      <div class="card" id="card-group-b">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:12px">
          <div style="width:10px;height:10px;border-radius:50%;background:#e94560;flex-shrink:0"></div>
          <h3 style="font-size:15px;font-weight:600">Gruppe B — Datei</h3>
        </div>
        <label style="font-size:12px;color:var(--muted);margin-bottom:4px;display:block">IFC-Datei auswählen</label>
        <select id="sel-doc-b"
          style="width:100%;background:var(--surface2);border:1px solid var(--border);
                 color:var(--text);padding:8px 10px;border-radius:7px;font-size:13px;margin-bottom:12px;cursor:pointer">
          <option value="">— Datei wählen —</option>
          {doc_options}
        </select>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <span style="font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.5px">Filter B</span>
          <div style="display:flex;gap:6px;align-items:center">
            <button type="button" id="btn-load-psets-b" onclick="loadPsetKeys('b')"
              class="btn" style="font-size:10px;padding:3px 9px;color:var(--accent)">Pset-Felder laden</button>
            <button type="button" onclick="addFilter('b')"
              class="btn" style="font-size:10px;padding:3px 9px">+ Hinzufügen</button>
          </div>
        </div>
        <div id="filters-b" style="display:flex;flex-direction:column;gap:6px"></div>
        <div id="no-filter-hint-b" style="font-size:11px;color:var(--muted);font-style:italic;padding:4px 0">
          Kein Filter – alle Elemente werden geprüft.
        </div>
      </div>

      <div class="card">
        <label style="font-size:12px;color:var(--muted);margin-bottom:4px;display:block">Toleranz (Meter)</label>
        <input id="inp-tolerance" type="number" step="0.001" min="0" value="0" style="margin-bottom:14px">
        <button id="btn-run" type="button" onclick="runClash()"
          class="btn btn-primary" style="width:100%;font-size:14px;padding:10px">
          ▶ Clash-Analyse starten
        </button>
        <div id="run-status" style="font-size:12px;color:var(--muted);margin-top:10px;min-height:18px;text-align:center"></div>
      </div>

    </div>

    <div id="clash-result">
      <div style="display:flex;flex-direction:column;align-items:center;
                  justify-content:center;padding:60px 40px;
                  border:1px dashed var(--border);border-radius:10px;
                  color:var(--muted);text-align:center;gap:12px">
        <svg width="40" height="40" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
          <circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>
        </svg>
        <div>
          <div style="font-size:14px;color:var(--text);margin-bottom:6px">Dateien auswählen und Analyse starten</div>
          <div style="font-size:12px">Wähle für Gruppe A und B je eine IFC-Datei,<br>optional Filter setzen, dann ▶ klicken.</div>
        </div>
      </div>
    </div>

  </div>
</div>

<style>
  .filter-row {{
    display: grid;
    grid-template-columns: 1fr 1fr 1.5fr auto;
    gap: 4px;
    align-items: center;
    padding: 6px 8px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 7px;
  }}
  .filter-row select, .filter-row input {{
    background: var(--surface2);
    border: 1px solid var(--border);
    color: var(--text);
    padding: 5px 7px;
    border-radius: 5px;
    font-size: 11px;
    font-family: inherit;
    width: 100%;
  }}
  .tag-a {{ background:#0a2a3e;color:#4fc3f7;border:1px solid #1a4a6e;
             display:inline-block;padding:2px 7px;border-radius:10px;font-size:11px;font-weight:600 }}
  .tag-b {{ background:#2a0a14;color:#e94560;border:1px solid #6e1a2e;
             display:inline-block;padding:2px 7px;border-radius:10px;font-size:11px;font-weight:600 }}
  #result-table {{ border-collapse:collapse;width:100% }}
  #result-table th {{ background:var(--surface2);color:#8ab;font-size:11px;
                      font-weight:600;padding:9px 12px;text-align:left;border:1px solid var(--border) }}
  #result-table td {{ padding:10px 12px;border:1px solid var(--border);font-size:12px;vertical-align:middle }}
  #result-table tr:hover td {{ background:rgba(79,195,247,.04) }}
</style>

<script>
(function () {{

const PROJECT_ID  = {json.dumps(project_id)};
const RUN_URL     = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/run`;
const PSET_URL    = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/pset-keys`;
const SAVE_URL    = `/projects/${{encodeURIComponent(PROJECT_ID)}}/clash/issues`;
// نام جدید cache key تا با cache قدیمی تداخل نداشته باشه
const STATE_KEY   = "bp_clash_v3_" + PROJECT_ID;

let lastClashes   = [];
let filterRows    = {{ a: [], b: [] }};
let psetKeys      = {{ a: [], b: [] }};
let filterCounter = 0;

function esc(s) {{
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

const BASE_FIELDS = [
  ["type","IFC-Typ"],["name","Name"],["file_label","Dateiname"],
  ["global_id","GlobalId"],["object_type","ObjectType"],["predefined_type","PredefinedType"],
];
const OPERATORS = [
  ["contains","enthält"],["not_contains","enthält nicht"],["equals","ist gleich"],
  ["not_equals","ist ungleich"],["starts_with","beginnt mit"],["ends_with","endet mit"],
];

function fieldOptions(group) {{
  let html = BASE_FIELDS.map(([v,l]) => `<option value="${{esc(v)}}">${{esc(l)}}</option>`).join("");
  if (psetKeys[group] && psetKeys[group].length) {{
    html += `<optgroup label="Eigenschaften (Psets)">`;
    psetKeys[group].forEach(k => {{
      html += `<option value="${{esc(k)}}">${{esc(k.replace(/^pset:/,""))}}</option>`;
    }});
    html += `</optgroup>`;
  }}
  return html;
}}

function opOptions() {{
  return OPERATORS.map(([v,l]) => `<option value="${{esc(v)}}">${{esc(l)}}</option>`).join("");
}}

window.addFilter = function(group) {{
  const id = `fr-${{group}}-${{++filterCounter}}`;
  const container = document.getElementById(`filters-${{group}}`);
  const hint = document.getElementById(`no-filter-hint-${{group}}`);
  const div = document.createElement("div");
  div.id = id; div.className = "filter-row";
  div.innerHTML = `
    <select class="filter-field">${{fieldOptions(group)}}</select>
    <select class="filter-op">${{opOptions()}}</select>
    <input class="filter-val" type="text" placeholder="Wert …">
    <button type="button" onclick="removeFilter('${{id}}','${{group}}')"
      style="background:none;border:none;color:var(--accent2);font-size:16px;cursor:pointer;padding:0 4px;line-height:1">✕</button>
  `;
  container.appendChild(div);
  filterRows[group].push(id);
  if (hint) hint.style.display = "none";
}};

window.removeFilter = function(id, group) {{
  const el = document.getElementById(id);
  if (el) el.remove();
  filterRows[group] = filterRows[group].filter(x => x !== id);
  const hint = document.getElementById(`no-filter-hint-${{group}}`);
  if (hint && !filterRows[group].length) hint.style.display = "";
}};

function collectFilters(group) {{
  return filterRows[group].map(id => {{
    const row = document.getElementById(id);
    if (!row) return null;
    return {{
      field: row.querySelector(".filter-field").value,
      operator: row.querySelector(".filter-op").value,
      value: row.querySelector(".filter-val").value.trim(),
    }};
  }}).filter(f => f && f.field && f.value);
}}

window.loadPsetKeys = async function(group) {{
  const docId = document.getElementById(`sel-doc-${{group}}`).value;
  if (!docId) {{ alert("Bitte zuerst eine Datei für Gruppe " + group.toUpperCase() + " auswählen."); return; }}
  const btn = document.getElementById(`btn-load-psets-${{group}}`);
  btn.disabled = true; btn.textContent = "⏳ …";
  try {{
    const resp = await fetch(PSET_URL + `?document_id=${{encodeURIComponent(docId)}}`);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    psetKeys[group] = data.pset_keys || [];
    filterRows[group].forEach(id => {{
      const row = document.getElementById(id);
      if (!row) return;
      const sel = row.querySelector(".filter-field");
      const cur = sel.value;
      sel.innerHTML = fieldOptions(group);
      sel.value = cur;
    }});
    btn.textContent = `${{psetKeys[group].length}} Pset-Felder geladen`;
  }} catch (e) {{
    btn.textContent = "Pset-Felder laden";
    alert("Fehler: " + e.message);
  }} finally {{
    btn.disabled = false;
  }}
}};

window.runClash = async function() {{
  const docIdA = document.getElementById("sel-doc-a").value;
  const docIdB = document.getElementById("sel-doc-b").value;
  if (!docIdA) {{ alert("Bitte eine Datei für Gruppe A auswählen."); return; }}
  if (!docIdB) {{ alert("Bitte eine Datei für Gruppe B auswählen."); return; }}

  const tolerance = parseFloat(document.getElementById("inp-tolerance").value) || 0;
  const btn = document.getElementById("btn-run");
  const status = document.getElementById("run-status");
  const result = document.getElementById("clash-result");

  btn.disabled = true;
  btn.textContent = "⏳ Dateien werden geladen …";
  status.innerHTML = "";
  result.innerHTML = `<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;padding:60px;gap:16px">
    <div style="width:36px;height:36px;border:3px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:bp-spin .7s linear infinite"></div>
    <div style="font-size:13px;color:var(--muted)">Dateien werden aus R2 geladen …</div>
  </div>`;

  try {{
    const resp = await fetch(RUN_URL, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{
        tolerance,
        group_a: {{document_id: docIdA, filters: collectFilters("a")}},
        group_b: {{document_id: docIdB, filters: collectFilters("b")}},
      }}),
    }});
    const data = await resp.json();
    if (data.error) throw new Error(data.error);

    // Cache leeren und neu befüllen – v3 key
    try {{ sessionStorage.removeItem("bp_clash_v2_" + PROJECT_ID); }} catch(_) {{}}
    lastClashes = data.clashes || [];
    try {{ sessionStorage.setItem(STATE_KEY, JSON.stringify(data)); }} catch(_) {{}}

    renderResult(data);
  }} catch (e) {{
    result.innerHTML = `<div class="flash-err" style="margin:0"><strong>⚠ Fehler:</strong> ${{esc(e.message)}}</div>`;
  }} finally {{
    btn.disabled = false;
    btn.textContent = "▶ Clash-Analyse starten";
    status.innerHTML = lastClashes.length
      ? `<span style="color:var(--accent2)">${{lastClashes.length}} Clash(es) gefunden</span>`
      : `<span style="color:var(--success)">✓ Keine Clashes</span>`;
  }}
}};

function renderResult(data) {{
  const result = document.getElementById("clash-result");
  const clashes = data.clashes || [];

  const summary = `<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">
    <div class="card" style="flex:1;min-width:130px;text-align:center;padding:14px">
      <div style="font-size:26px;font-weight:700;color:var(--text)">${{data.count_a ?? "–"}}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px">Elemente A</div>
    </div>
    <div class="card" style="flex:1;min-width:130px;text-align:center;padding:14px">
      <div style="font-size:26px;font-weight:700;color:var(--text)">${{data.count_b ?? "–"}}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px">Elemente B</div>
    </div>
    <div class="card" style="flex:1;min-width:130px;text-align:center;padding:14px;
         border-color:${{clashes.length ? "var(--accent2)" : "var(--success)"}}">
      <div style="font-size:26px;font-weight:700;color:${{clashes.length ? "var(--accent2)" : "var(--success)"}}">${{clashes.length}}</div>
      <div style="font-size:11px;color:var(--muted);margin-top:2px">Clashes</div>
    </div>
  </div>`;

  if (!clashes.length) {{
    result.innerHTML = summary + `<div class="flash-ok" style="text-align:center;padding:20px">✓ Keine geometrischen Überschneidungen gefunden.</div>`;
    return;
  }}

  let rows = "";
  clashes.forEach((c, idx) => {{
    rows += `<tr>
      <td style="text-align:center;width:36px">
        <input type="checkbox" class="issue-pick" value="${{idx}}" style="accent-color:var(--accent);width:13px;height:13px">
      </td>
      <td style="text-align:center;color:var(--muted);width:36px">${{idx + 1}}</td>
      <td>
        <span class="tag-a">${{esc(c.type_1)}}</span>
        <span style="margin-left:5px">${{esc(c.name_1 || "–")}}</span>
        <div style="font-family:monospace;font-size:10px;color:var(--muted);margin-top:3px">${{esc(c.global_id_1)}}</div>
        <div style="font-size:10px;color:var(--muted)">${{esc(c.file_label_1)}}</div>
      </td>
      <td>
        <span class="tag-b">${{esc(c.type_2)}}</span>
        <span style="margin-left:5px">${{esc(c.name_2 || "–")}}</span>
        <div style="font-family:monospace;font-size:10px;color:var(--muted);margin-top:3px">${{esc(c.global_id_2)}}</div>
        <div style="font-size:10px;color:var(--muted)">${{esc(c.file_label_2)}}</div>
      </td>
    </tr>`;
  }});

  result.innerHTML = summary + `
    <div class="card" style="padding:0;overflow:hidden">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border)">
        <span style="font-size:14px;font-weight:600">${{clashes.length}} Clash${{clashes.length !== 1 ? "es" : ""}} gefunden</span>
        <div style="display:flex;gap:8px">
          <button type="button" onclick="toggleAllIssues(true)" class="btn" style="font-size:11px;padding:4px 10px">Alle wählen</button>
          <button type="button" onclick="toggleAllIssues(false)" class="btn" style="font-size:11px;padding:4px 10px">Keine</button>
          <button type="button" onclick="saveSelectedIssues()" class="btn btn-primary" style="font-size:12px;padding:5px 14px">
            💾 Auswahl als Issues speichern
          </button>
        </div>
      </div>
      <div style="overflow:auto;max-height:60vh">
        <table id="result-table">
          <thead><tr>
            <th style="width:36px"></th><th style="width:36px">#</th>
            <th>Element A</th><th>Element B</th>
          </tr></thead>
          <tbody>${{rows}}</tbody>
        </table>
      </div>
    </div>`;
}}

window.toggleAllIssues = function(on) {{
  document.querySelectorAll(".issue-pick").forEach(c => c.checked = !!on);
}};

window.saveSelectedIssues = async function() {{
  const selected = [...document.querySelectorAll(".issue-pick:checked")]
    .map(c => lastClashes[parseInt(c.value, 10)])
    .filter(Boolean);

  if (!selected.length) {{
    alert("Bitte mindestens eine Clash-Zeile auswählen.");
    return;
  }}

  // Validierung vor dem Senden
  const valid = selected.filter(c => c && c.global_id_1 && c.global_id_2);
  if (!valid.length) {{
    alert("Die ausgewählten Clashes haben keine GlobalIds. Bitte Clash-Analyse neu starten (▶ klicken).");
    return;
  }}

  const resp = await fetch(SAVE_URL, {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{clashes: valid}}),
  }});
  const data = await resp.json();

  if (data.error) {{
    alert("Fehler: " + data.error);
    return;
  }}

  const neu = data.saved || 0;
  const gesamt = valid.length;
  const duplikate = gesamt - neu;
  if (neu > 0 && duplikate > 0) {{
    alert(`${{neu}} Issue(s) neu gespeichert. ${{duplikate}} waren bereits vorhanden.`);
  }} else if (neu > 0) {{
    alert(`${{neu}} Issue(s) gespeichert.`);
  }} else {{
    alert(`Alle ${{gesamt}} Clashes waren bereits als Issues vorhanden.`);
  }}
  window.location.href = `/projects/${{encodeURIComponent(PROJECT_ID)}}/issues`;
}};

// Restore – nur v3 cache, v2 löschen
(function restore() {{
  try {{ sessionStorage.removeItem("bp_clash_v2_" + PROJECT_ID); }} catch(_) {{}}
  try {{
    const raw = sessionStorage.getItem(STATE_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    if (!data || !Array.isArray(data.clashes)) return;
    // Nur laden wenn clashes gültige global_ids haben
    const valid = data.clashes.filter(c => c && c.global_id_1 && c.global_id_2);
    if (!valid.length) return;
    lastClashes = data.clashes;
    renderResult(data);
    const status = document.getElementById("run-status");
    if (status && lastClashes.length) {{
      status.innerHTML = `<span style="color:var(--accent2)">${{lastClashes.length}} Clash(es) – gecachtes Ergebnis</span>`;
    }}
  }} catch (_) {{}}
}})();

}})();
</script>
"""
    return _page(f"{project['project_name']} – Clash", body)


@project_clash_router.get("/projects/{project_id}/clash", response_class=HTMLResponse)
def project_clash_page(
    request: Request,
    project_id: str,
    saved: str = Query(default=""),
    error: str = Query(default=""),
):
    account, project = _load_context(request, project_id)
    if not project:
        return RedirectResponse("/", status_code=302)
    return _clash_page_html(project, account)


@project_clash_router.post("/projects/{project_id}/clash/run")
async def project_clash_run(request: Request, project_id: str):
    account, project = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiger JSON-Body."}, status_code=400)

    tolerance = float(body.get("tolerance", 0.0))
    group_a   = body.get("group_a", {})
    group_b   = body.get("group_b", {})
    doc_id_a  = str(group_a.get("document_id", "")).strip()
    doc_id_b  = str(group_b.get("document_id", "")).strip()
    filters_a = group_a.get("filters", [])
    filters_b = group_b.get("filters", [])

    if not doc_id_a:
        return JSONResponse({"error": "Gruppe A: kein Dokument ausgewählt."}, status_code=400)
    if not doc_id_b:
        return JSONResponse({"error": "Gruppe B: kein Dokument ausgewählt."}, status_code=400)

    try:
        model_a, label_a = _open_ifc_from_document(account["account_id"], project_id, doc_id_a)
    except Exception as exc:
        return JSONResponse({"error": f"Gruppe A – Datei konnte nicht geladen werden: {exc}"}, status_code=500)

    try:
        model_b, label_b = _open_ifc_from_document(account["account_id"], project_id, doc_id_b)
    except Exception as exc:
        return JSONResponse({"error": f"Gruppe B – Datei konnte nicht geladen werden: {exc}"}, status_code=500)

    try:
        elements_a = _extract_elements_from_model(model_a, label_a, doc_id_a, filters_a)
        elements_b = _extract_elements_from_model(model_b, label_b, doc_id_b, filters_b)
        clashes    = compare_element_groups_for_clashes(elements_a, elements_b, tolerance)
        return JSONResponse({"count_a": len(elements_a), "count_b": len(elements_b), "clashes": clashes})
    except Exception as exc:
        return JSONResponse({"error": f"Analyse-Fehler: {exc}"}, status_code=500)


@project_clash_router.get("/projects/{project_id}/clash/pset-keys")
def project_clash_pset_keys(
    request: Request,
    project_id: str,
    document_id: str = Query(...),
):
    account, project = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)

    if not document_id.strip():
        return JSONResponse({"error": "document_id fehlt."}, status_code=400)

    try:
        model, _ = _open_ifc_from_document(account["account_id"], project_id, document_id)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    pset_keys: set[str] = set()
    for elem in get_candidate_products(model):
        psets = get_psets_safe(elem)
        for pset_name, props in (psets or {}).items():
            if isinstance(props, dict):
                for prop_name in props:
                    pset_keys.add(f"pset:{pset_name}.{prop_name}")

    return JSONResponse({"pset_keys": sorted(pset_keys)})


@project_clash_router.post("/projects/{project_id}/clash/issues")
async def project_clash_save_issues(request: Request, project_id: str):
    """Speichert ausgewählte Clashes als Issues im Issues-Modul."""
    account, project = _load_context(request, project_id)
    if not project:
        return JSONResponse({"error": "Projekt nicht gefunden."}, status_code=404)

    try:
        body = await request.json()
        clashes = body.get("clashes", [])

        if not clashes:
            return JSONResponse({"error": "Keine Clashes im Request-Body."}, status_code=400)

        # Nur Clashes mit gültigen GlobalIds weiterleiten
        valid_clashes = [
            c for c in clashes
            if str(c.get("global_id_1") or "").strip()
            and str(c.get("global_id_2") or "").strip()
        ]

        if not valid_clashes:
            return JSONResponse({
                "error": f"Keine gültigen GlobalIds in den {len(clashes)} Clashes. "
                         "Bitte Clash-Analyse neu starten.",
            }, status_code=400)

        saved = save_clash_issues(account["account_id"], project_id, valid_clashes)
        return JSONResponse({
            "ok": True,
            "saved": len(saved),
            "issues": saved,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@project_clash_router.get("/projects/{project_id}/clash/detail", response_class=HTMLResponse)
def project_clash_detail(
    request: Request,
    project_id: str,
    gid1: str = Query(default=""),
    gid2: str = Query(default=""),
):
    return RedirectResponse(f"/projects/{_e(project_id)}/view", status_code=302)
