"""
viewer.py  –  BIMPruef 3D-Viewer

Routen:
  GET  /viewer/                    → Viewer-Hauptseite (Upload + 3D)
  POST /viewer/upload/             → IFC / IFCZIP hochladen (bis zu 10, max 500 MB)
  POST /viewer/remove/             → Einzelne Datei aus Session entfernen
  GET  /viewer/file/               → Rohe IFC-Datei ausliefern (immer .ifc)
  GET  /viewer/clash/              → Clash-Analyse (zwei Modelle wählen)
  GET  /viewer/clash/detail/       → Clash-Detail: nur Clash-Elemente hervorgehoben
  GET  /viewer/clash/bcf/          → Alle Clashes als BCF-ZIP
  GET  /viewer/clash/bcf-single/   → Einzelnen Clash als BCF-ZIP
  POST /viewer/element/update/     → Element-Eigenschaften (Name, Typ, PSet) aktualisieren
  GET  /viewer/export-ifc/         → Modifizierte IFC-Datei herunterladen
"""


import html
import io
import json
import os
import urllib.parse

import httpx
from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.storage import (
    ALLOWED_EXTENSIONS,
    MAX_FILES_PER_SESSION,
    create_upload_session,
    get_ifc_label,
    get_ifc_path,
    get_session_slots,
    load_clash_cache,
    remove_ifc_slot,
    save_clash_cache,
    save_ifc_file,
    session_exists,
)

router = APIRouter()

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL   = "claude-sonnet-4-20250514"
ALLOW_FREE_TEST_MODE = os.environ.get("ALLOW_FREE_TEST_MODE", "1").strip().lower() in {
    "1", "true", "yes", "on"
}


def _build_ifc_context(session_id: str, slot: int) -> str:
    """
    Reads the IFC model for the given slot and builds a structured text
    summary of all elements (type, name, GlobalId, psets) for the AI prompt.
    """
    try:
        import ifcopenshell
        import ifcopenshell.util.element

        path = get_ifc_path(session_id, slot)
        if not os.path.exists(path):
            return ""

        model = ifcopenshell.open(path)
        label = get_ifc_label(session_id, slot)

        lines = [f"IFC-Modell: {label}"]

        # Project / Site / Building / Storey hierarchy
        for proj in model.by_type("IfcProject"):
            lines.append(f"\nProjekt: {getattr(proj, 'Name', '') or ''}")

        for storey in model.by_type("IfcBuildingStorey"):
            elev = getattr(storey, "Elevation", None)
            elev_str = f", Elevation={elev}" if elev is not None else ""
            lines.append(f"Geschoss: {getattr(storey, 'Name', '') or ''}{elev_str}")

        # All products (grouped by type)
        from app.extractors import get_candidate_products, get_psets_safe

        elements_by_type: dict = {}
        for elem in get_candidate_products(model):
            t = elem.is_a()
            if t not in elements_by_type:
                elements_by_type[t] = []
            elements_by_type[t].append(elem)

        lines.append(f"\nElementübersicht ({sum(len(v) for v in elements_by_type.values())} Elemente gesamt):")
        for ifc_type, elems in sorted(elements_by_type.items()):
            lines.append(f"\n## {ifc_type} ({len(elems)} Stück)")
            for elem in elems:
                name     = getattr(elem, "Name", "") or ""
                gid      = getattr(elem, "GlobalId", "") or ""
                obj_type = getattr(elem, "ObjectType", "") or ""
                # Predefined type
                try:
                    pdt = str(getattr(elem, "PredefinedType", "") or "")
                except Exception:
                    pdt = ""

                elem_line = f"  - Name='{name}' GlobalId={gid}"
                if obj_type:
                    elem_line += f" ObjectType='{obj_type}'"
                if pdt and pdt not in ("None", "NOTDEFINED", ""):
                    elem_line += f" PredefinedType={pdt}"

                # Include relevant psets (height, area, volume, etc.)
                try:
                    psets = get_psets_safe(elem)
                    relevant_props = {}
                    RELEVANT_KEYS = {
                        "height", "höhe", "hoehe", "width", "breite", "length", "länge", "laenge",
                        "area", "fläche", "flaeche", "volume", "volumen",
                        "nettoraumfläche", "nettoflaeche", "grossfloor",
                        "loadbearing", "isexternal", "firerating",
                        "longname", "description", "function", "funktion",
                    }
                    for pset_name, props in psets.items():
                        if not isinstance(props, dict):
                            continue
                        for prop_name, value in props.items():
                            if any(k in prop_name.lower() for k in RELEVANT_KEYS):
                                relevant_props[f"{pset_name}.{prop_name}"] = value
                    if relevant_props:
                        props_str = ", ".join(f"{k}={v}" for k, v in list(relevant_props.items())[:8])
                        elem_line += f" [{props_str}]"
                except Exception:
                    pass

                lines.append(elem_line)

        return "\n".join(lines)
    except Exception as exc:
        return f"(Fehler beim Lesen des Modells: {exc})"


def _answer_in_free_test_mode(question: str, ifc_context: str, slot: int) -> str:
    """
    Fallback-Antwort ohne externe API, wenn kein Anthropic-Key vorhanden ist.
    Diese Antwort ist bewusst transparent als lokaler Testmodus gekennzeichnet.
    """
    q = (question or "").strip()
    q_lower = q.lower()
    lines = ifc_context.splitlines()

    element_types = []
    for line in lines:
        if line.startswith("## ") and "(" in line:
            # z. B. "## IfcWall (12 Stück)"
            type_name = line[3:].split("(", 1)[0].strip()
            if type_name:
                element_types.append(type_name)

    hints = []
    if any(k in q_lower for k in ("wie viele", "anzahl", "count")):
        if "Elementübersicht (" in ifc_context:
            for line in lines:
                if "Elementübersicht (" in line:
                    hints.append(f"Im Modell (Slot {slot}) steht: {line.strip()}")
                    break
        if element_types:
            hints.append(
                "Vorhandene IFC-Typen (Auszug): " +
                ", ".join(element_types[:8]) +
                (" …" if len(element_types) > 8 else "")
            )
    else:
        if element_types:
            hints.append(
                "Ich sehe u. a. folgende IFC-Typen: " +
                ", ".join(element_types[:10]) +
                (" …" if len(element_types) > 10 else "")
            )

    if not hints:
        hints.append("Das Modell wurde geladen, aber es konnten nur wenige strukturierte Details extrahiert werden.")

    return (
        "⚠ Lokaler Testmodus (ohne Anthropic API):\n"
        "Für einen echten KI-Dialog brauchst du einen gültigen API-Key "
        "(Umgebungsvariable ANTHROPIC_API_KEY).\n\n"
        f"Deine Frage: {q or '(leer)'}\n\n"
        + "\n".join(f"- {h}" for h in hints)
    )


# ─────────────────────────────────────────────────────────────────────────────
# AI Chat Endpoint
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/ai-chat/")
async def viewer_ai_chat(
    session_id: str = Form(...),
    slot: int       = Form(default=1),
    question: str   = Form(...),
):
    """
    Receives a question about the IFC model, builds a structured context
    from the model data, and queries the Anthropic API for an answer.
    """
    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    ifc_context = _build_ifc_context(session_id, slot)

    if not ifc_context:
        return JSONResponse(
            {"error": f"Kein Modell in Slot {slot} gefunden oder Modell ist leer."},
            status_code=404,
        )

    if not ANTHROPIC_API_KEY:
        if not ALLOW_FREE_TEST_MODE:
            return JSONResponse(
                {"error": "Kein ANTHROPIC_API_KEY gesetzt. Bitte Umgebungsvariable konfigurieren."},
                status_code=500,
            )
        fallback_answer = _answer_in_free_test_mode(question, ifc_context, slot)
        return JSONResponse({"answer": fallback_answer, "mode": "free_test"})

    system_prompt = (
        "Du bist ein BIM-Experte und IFC-Assistent für die Plattform BIMPruef. "
        "Dir wird eine strukturierte Zusammenfassung eines IFC-Gebäudemodells bereitgestellt. "
        "Beantworte Fragen des Nutzers präzise und hilfreich auf Basis dieser Modelldaten. "
        "Wenn du eine Information nicht im Modell findest, sage das ehrlich. "
        "Antworte immer auf Deutsch, außer der Nutzer fragt auf Englisch. "
        "Sei prägnant, fachlich korrekt und benutzerfreundlich."
    )

    user_message = (
        f"Hier sind die Daten des IFC-Modells:\n\n"
        f"{ifc_context}\n\n"
        f"---\n\n"
        f"Frage des Nutzers: {question}"
    )

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": ANTHROPIC_MODEL,
                    "max_tokens": 1024,
                    "system": system_prompt,
                    "messages": [
                        {"role": "user", "content": user_message}
                    ],
                },
            )
        data = response.json()
        if response.status_code != 200:
            err = data.get("error", {}).get("message", str(data))
            return JSONResponse({"error": f"API-Fehler: {err}"}, status_code=500)

        answer = ""
        for block in data.get("content", []):
            if block.get("type") == "text":
                answer += block["text"]

        return JSONResponse({"answer": answer.strip()})

    except httpx.TimeoutException:
        return JSONResponse({"error": "Timeout beim Kontakt mit der KI-API."}, status_code=504)
    except Exception as exc:
        return JSONResponse({"error": f"Unbekannter Fehler: {exc}"}, status_code=500)

MAX_FILE_SIZE_MB    = 500
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
BCF_CLASH_LIMIT     = int(os.environ.get("BCF_CLASH_LIMIT", "500"))

# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsame CSS-Basis
# ─────────────────────────────────────────────────────────────────────────────
DARK_STYLES = """\
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#f6f8fb;
  --bg-soft:#eef3f8;
  --surface:#ffffff;
  --surface2:#f8fafc;
  --surface3:#eef4f9;
  --border:#d8e2ec;
  --border-strong:#b9c8d8;
  --accent:#2563eb;
  --accent-hover:#1d4ed8;
  --accent-soft:#e8f0ff;
  --accent2:#dc2626;
  --danger:#dc2626;
  --danger-soft:#fef2f2;
  --success:#16a34a;
  --success-soft:#ecfdf3;
  --warn:#b7791f;
  --warn-soft:#fff7e6;
  --info:#0f6fae;
  --info-soft:#e8f5ff;
  --text:#172033;
  --text-strong:#0f172a;
  --muted:#65758b;
  --muted2:#94a3b8;
  --shadow-sm:0 1px 2px rgba(15,23,42,.04);
  --shadow-md:0 10px 30px rgba(15,23,42,.08);
  --radius-sm:8px;
  --radius-md:12px;
  --radius-lg:18px;
  --topbar-h:56px;
}
html{height:100%;background:var(--bg)}
body{
  font-family:Inter,'Segoe UI',Roboto,Arial,sans-serif;
  background:
    radial-gradient(circle at 8% -12%,rgba(37,99,235,.10),transparent 34%),
    linear-gradient(180deg,#fbfdff 0%,var(--bg) 56%,#f3f6fa 100%);
  color:var(--text);
  min-height:100vh;
  line-height:1.5;
  -webkit-font-smoothing:antialiased;
  text-rendering:optimizeLegibility;
}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:none;color:var(--accent-hover)}
h1,h2,h3{color:var(--text-strong);letter-spacing:-.025em;line-height:1.22}
p{line-height:1.65}
button,.btn,a.button{
  display:inline-flex;align-items:center;justify-content:center;gap:7px;
  min-height:34px;padding:8px 14px;
  background:var(--surface);
  border:1px solid var(--border);
  color:var(--text);
  border-radius:var(--radius-sm);
  cursor:pointer;
  font-size:13px;
  font-weight:650;
  letter-spacing:.005em;
  transition:background .16s,border-color .16s,color .16s,box-shadow .16s,transform .16s;
  text-decoration:none;
  box-shadow:var(--shadow-sm);
}
button:hover,.btn:hover,a.button:hover{
  background:var(--surface2);
  border-color:var(--border-strong);
  color:var(--text-strong);
  box-shadow:0 8px 20px rgba(15,23,42,.08);
  transform:translateY(-1px);
  text-decoration:none;
}
button:active,.btn:active,a.button:active{transform:translateY(0);box-shadow:var(--shadow-sm)}
button:focus-visible,.btn:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{
  outline:3px solid rgba(37,99,235,.16);
  outline-offset:2px;
}
.btn-primary,button.main-btn{
  background:var(--accent);
  border-color:var(--accent);
  color:#fff;
  box-shadow:0 10px 22px rgba(37,99,235,.20);
}
.btn-primary:hover,button.main-btn:hover{
  background:var(--accent-hover);
  border-color:var(--accent-hover);
  color:#fff;
}
.btn-danger{
  background:#fff;
  border-color:#fecaca;
  color:var(--danger);
}
.btn-danger:hover{
  background:var(--danger-soft);
  border-color:#fca5a5;
  color:#991b1b;
}
.card,.box,.summary-box,.diff-box{
  background:rgba(255,255,255,.92);
  border:1px solid var(--border);
  border-radius:var(--radius-md);
  padding:18px;
  margin-bottom:16px;
  box-shadow:var(--shadow-sm);
}
.card:hover,.box:hover,.summary-box:hover{box-shadow:0 8px 26px rgba(15,23,42,.06)}
input[type=text],input[type=email],input[type=password],input[type=tel],input[type=number],textarea,select{
  width:100%;
  background:#fff;
  border:1px solid var(--border);
  color:var(--text);
  padding:9px 12px;
  border-radius:var(--radius-sm);
  font-size:14px;
  font-family:inherit;
  outline:none;
  transition:border-color .16s,box-shadow .16s,background .16s;
  box-shadow:0 1px 1px rgba(15,23,42,.02);
}
input:hover,textarea:hover,select:hover{border-color:var(--border-strong)}
input:focus,textarea:focus,select:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 4px rgba(37,99,235,.12);
}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px;margin-top:12px;font-weight:650}
input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px;cursor:pointer}
table{
  border-collapse:separate;
  border-spacing:0;
  width:100%;
  background:#fff;
  border:1px solid var(--border);
  border-radius:var(--radius-md);
  overflow:hidden;
  box-shadow:var(--shadow-sm);
}
th,td{
  border:0;
  border-bottom:1px solid var(--border);
  padding:10px 12px;
  text-align:left;
  vertical-align:top;
  font-size:12px;
}
th{
  background:linear-gradient(180deg,#f8fafc,#eef4f9);
  color:#42526a;
  font-weight:750;
  position:sticky;
  top:0;
  z-index:1;
  text-transform:uppercase;
  letter-spacing:.035em;
  font-size:11px;
}
tr:last-child td{border-bottom:0}
tr:hover td{background:#f8fbff}
.badge,.tag{
  display:inline-flex;align-items:center;gap:4px;
  padding:3px 9px;
  border-radius:999px;
  font-size:11px;
  font-weight:750;
  line-height:1.2;
}
.badge-active,.flash-ok{background:var(--success-soft);color:#166534;border:1px solid #bbf7d0}
.badge-inactive{background:var(--warn-soft);color:#92400e;border:1px solid #fed7aa}
.badge-error,.tag-2{background:var(--danger-soft);color:#991b1b;border:1px solid #fecaca}
.badge-warning{background:var(--warn-soft);color:#92400e;border:1px solid #fed7aa}
.badge-info,.tag-1{background:var(--info-soft);color:#075985;border:1px solid #bae6fd}
.flash-err{
  background:var(--danger-soft);
  border:1px solid #fecaca;
  border-radius:var(--radius-md);
  padding:11px 14px;
  color:#991b1b;
  font-size:13px;
  margin-bottom:14px;
  box-shadow:var(--shadow-sm);
}
.flash-ok{
  border-radius:var(--radius-md);
  padding:11px 14px;
  font-size:13px;
  margin-bottom:14px;
  box-shadow:var(--shadow-sm);
}
.small{font-size:12px;color:var(--muted)}
footer{
  text-align:center;
  padding:24px 0 14px;
  border-top:1px solid var(--border);
  color:var(--muted);
  font-size:12px;
  margin-top:40px;
}
pre{
  white-space:pre-wrap;
  word-break:break-word;
  margin:0;
  font-size:12px;
  background:#f8fafc;
  border:1px solid var(--border);
  border-radius:var(--radius-md);
  padding:14px;
}
.model-card,.cat-item{
  background:#fff;
  border-bottom:1px solid var(--border);
}
.cat-item:hover{background:#f1f6ff}
.sev-error{color:#991b1b;font-weight:750}
.sev-warning{color:#92400e;font-weight:750}
.sev-info{color:#075985}
.danger{color:#991b1b;font-weight:750}
.success{color:#166534;font-weight:750}
.info-banner{
  background:var(--warn-soft);
  border:1px solid #fed7aa;
  border-radius:var(--radius-md);
  padding:11px 14px;
  margin-bottom:16px;
  font-size:13px;
  color:#6b4a0f;
}

/* Project/viewer shell refinement */
[style*="height:calc(100vh - 47px)"]{height:calc(100vh - var(--topbar-h)) !important}
[style*="height:100vh"]{background:transparent}
[style*="background:var(--surface)"]{background:var(--surface) !important}
[style*="background:var(--surface2)"]{background:var(--surface2) !important}
[style*="background:#0f2040"],
[style*="background:#12192e"],
[style*="background:#0e1e38"],
[style*="background:#0e1a30"],
[style*="background:#1a3a6e"],
[style*="background:#223a5e"]{
  background:var(--surface2) !important;
}
[style*="background:#0e0e1a"],
[style*="background:rgba(14,14,26"]{
  background:rgba(248,250,252,.94) !important;
}
[style*="border-left:1px solid var(--border)"],
[style*="border-right:1px solid var(--border)"],
[style*="border-bottom:1px solid var(--border)"],
[style*="border-top:1px solid var(--border)"]{
  border-color:var(--border) !important;
}
[style*="color:#8ab"],[style*="color:#889"],[style*="color:#4a6080"],[style*="color:#445"]{
  color:var(--muted) !important;
}
[style*="color:#4fc3f7"]{color:var(--accent) !important}
[style*="color:#ffb3b3"],[style*="color:#ffaaaa"]{color:#991b1b !important}
[style*="box-shadow:0 8px 40px rgba(0,0,0,.7)"]{
  box-shadow:0 24px 70px rgba(15,23,42,.16) !important;
}
#loading{backdrop-filter:blur(6px)}
#loading [style*="border:4px"]{
  border-color:#dbeafe !important;
  border-top-color:var(--accent) !important;
}
#info-panel{
  background:#fff !important;
  box-shadow:-16px 0 40px rgba(15,23,42,.06);
}
#ai-chat-panel{
  background:#fff !important;
  border-color:var(--border) !important;
  box-shadow:0 24px 70px rgba(15,23,42,.18) !important;
}
.ai-header{
  background:linear-gradient(180deg,#fff,#f8fafc) !important;
  border-bottom:1px solid var(--border) !important;
}
.ai-header-icon{background:linear-gradient(135deg,#2563eb,#0ea5e9) !important;color:#fff}
.ai-header-title{color:var(--text-strong) !important}
.ai-header-sub,.ai-slot-label{color:var(--muted) !important}
.ai-slot-row,.ai-suggestions{border-color:var(--border) !important;background:#fff !important}
.ai-slot-select{
  background:#fff !important;
  border:1px solid var(--border) !important;
  color:var(--text) !important;
}
.ai-msg.user{
  background:var(--accent) !important;
  color:#fff !important;
}
.ai-msg.assistant,.ai-msg.typing{
  background:#f8fafc !important;
  color:var(--text) !important;
  border:1px solid var(--border) !important;
}
.ai-msg.error{
  background:var(--danger-soft) !important;
  color:#991b1b !important;
  border:1px solid #fecaca !important;
}
.ai-close{color:var(--muted) !important;background:transparent !important;border:0 !important;box-shadow:none !important}
@keyframes spin{to{transform:rotate(360deg)}}
@media (max-width:900px){
  .card,.box,.summary-box{padding:16px}
  button,.btn,a.button{width:auto}
  table{font-size:12px}
}
</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
{DARK_STYLES}
</head>
<body>
{body}
<footer>
  <p>BIMPruef Platform by Foad Amini &nbsp;·&nbsp;
  <a href="/impressum">Impressum</a> &nbsp;·&nbsp;
  <a href="/datenschutz">Datenschutz</a></p>
</footer>
</body>
</html>""")


def _e(s) -> str:
    return html.escape(str(s or ""))

def _brand_logo(height_px: int = 28) -> str:
    """Brand-Element (Icon + Text) im Stil des gewünschten BIMPRUEF-Logos."""
    icon_size = max(27, int(height_px * 1.28))
    text_size = max(18, int(height_px * 0.92))
    return f"""<div style="display:flex;align-items:center;gap:8px;margin-right:12px;line-height:1">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 54" role="img" aria-label="BIMpruef icon" style="height:{icon_size}px;width:auto;display:block">
    <g fill="none" stroke-linejoin="round" stroke-linecap="round" stroke-width="2.8">
      <g stroke="#1f5f9f">
        <path d="M8 18 24 8l16 10-16 10z"/>
        <path d="M8 18v18l16 10V28z"/>
        <path d="M40 18v18L24 46V28z"/>
      </g>
      <g stroke="#d8192f">
        <path d="M29 18 45 8l16 10-16 10z"/>
        <path d="M29 18v18l16 10V28z"/>
        <path d="M61 18v18L45 46V28z"/>
      </g>
      <g stroke="#8f4399">
        <path d="M50 18 66 8l16 10-16 10z"/>
        <path d="M50 18v18l16 10V28z"/>
        <path d="M82 18v18L66 46V28z"/>
      </g>
      <g stroke="#27a6ad">
        <path d="M71 18 87 8l16 10-16 10z"/>
        <path d="M71 18v18l16 10V28z"/>
        <path d="M103 18v18L87 46V28z"/>
      </g>
    </g>
  </svg>
  <span style="font-family:'Avenir Next','Montserrat','Segoe UI',sans-serif;font-weight:300;letter-spacing:1.2px;color:var(--text);font-size:{text_size}px;white-space:nowrap;text-transform:uppercase">BIMPRUEF</span>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
# Navigationsleiste
# ─────────────────────────────────────────────────────────────────────────────

def _project_url(project_id: str, area: str = "viewer", extra: str = "") -> str:
    """Return the project-scoped UI URL for a viewer-related area."""
    pid = _e(project_id)
    base_map = {
        "dashboard": f"/projects/{pid}",
        "viewer":    f"/projects/{pid}/model",
        "clash":     f"/projects/{pid}/model/clash",
        "list":      f"/projects/{pid}/model/list",
        "rulecheck": f"/projects/{pid}/model/rulecheck",
    }
    base = base_map.get(area, base_map["viewer"])
    if extra:
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}{extra}"
    return base


def _viewer_url(session_id: str, area: str = "viewer", project_id: str = "", extra: str = "") -> str:
    """Build a UI URL. Prefer project-scoped routes when project_id is known."""
    if project_id:
        return _project_url(project_id, area, extra)
    sid = _e(session_id)
    base_map = {
        "viewer":    f"/viewer/?session_id={sid}",
        "clash":     f"/viewer/clash/?session_id={sid}",
        "list":      f"/viewer/list/?session_id={sid}",
        "rulecheck": f"/viewer/rulecheck/?session_id={sid}",
    }
    base = base_map.get(area, base_map["viewer"])
    if extra:
        base += "&" + extra
    return base


def _topbar(session_id: str, active: str = "", clash_params: str = "", project_id: str = "") -> str:
    sid = _e(session_id)
    project_back = (
        f'<a href="{_project_url(project_id, "dashboard")}" style="padding:8px 12px;font-size:12px;color:var(--muted);text-decoration:none">← Projekt</a>'
        if project_id else ""
    )
    nav = [
        ("viewer",    _viewer_url(session_id, "viewer", project_id),                    "🏗 Model"),
        ("clash",     _viewer_url(session_id, "clash", project_id, clash_params),        "⚡ Clash-Analyse"),
        ("list",      _viewer_url(session_id, "list", project_id),                      "📋 Liste"),
        ("rulecheck", _viewer_url(session_id, "rulecheck", project_id),                 "✅ Rule-Check"),
    ]
    items = project_back
    for key, href, label in nav:
        style = (
            "padding:8px 14px;font-size:13px;border-radius:6px;"
            "color:var(--accent);border-bottom:2px solid var(--accent);text-decoration:none"
            if active == key else
            "padding:8px 14px;font-size:13px;border-radius:6px;"
            "color:var(--text);text-decoration:none"
        )
        items += f'<a href="{href}" style="{style}">{label}</a>'
    context_label = f"Projekt: {_e(project_id)[:8]}…" if project_id else f"Session: {sid[:8]}…"
    return (
        f'<div style="display:flex;align-items:center;gap:4px;padding:6px 16px;'
        f'background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">'
        f'{_brand_logo(24)}'
        f'{items}'
        f'<span style="margin-left:auto;font-size:11px;color:var(--muted)">{context_label}</span>'
        f'</div>'
    )


# ─────────────────────────────────────────────────────────────────────────────
# Slot-Farbpalette
# ─────────────────────────────────────────────────────────────────────────────
SLOT_COLORS = [
    "#4fc3f7", "#ef9a9a", "#a5d6a7", "#ffcc80",
    "#ce93d8", "#80cbc4", "#f48fb1", "#ffab40",
    "#80deea", "#bcaaa4",
]

def _slot_color(slot: int) -> str:
    return SLOT_COLORS[(slot - 1) % len(SLOT_COLORS)]


# ─────────────────────────────────────────────────────────────────────────────
# IFC-Datei ausliefern (immer .ifc, IFCZIP wurde beim Upload bereits entpackt)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/file/")
def viewer_file(session_id: str = Query(...), slot: int = Query(default=1)):
    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)
    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return Response(content=f"Slot {slot} nicht gefunden.", status_code=404)
    with open(path, "rb") as f:
        data = f.read()
    label = get_ifc_label(session_id, slot, f"model_{slot}.ifc")
    # Dateiname für Download immer als .ifc (auch wenn Original .ifczip war)
    dl_name = label if label.lower().endswith(".ifc") else label.rsplit(".", 1)[0] + ".ifc"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{dl_name}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upload
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/upload/")
async def viewer_upload(
    session_id: str = Form(default=""),
    project_id: str = Form(default=""),
    files: list[UploadFile] = File(...),
):
    errors = []

    if not session_id or not session_exists(session_id):
        session_id = create_upload_session()

    existing_slots = get_session_slots(session_id)
    next_slot      = max(existing_slots, default=0) + 1

    for uf in files:
        fname = uf.filename or ""
        lower = fname.lower()

        if not any(lower.endswith(ext) for ext in ALLOWED_EXTENSIONS):
            errors.append(f"'{_e(fname)}': nur .ifc und .ifczip erlaubt.")
            continue

        if next_slot > MAX_FILES_PER_SESSION:
            errors.append(f"Maximale Anzahl von {MAX_FILES_PER_SESSION} Modellen erreicht.")
            break

        data = await uf.read()

        if len(data) > MAX_FILE_SIZE_BYTES:
            errors.append(f"'{_e(fname)}' überschreitet {MAX_FILE_SIZE_MB} MB.")
            continue

        try:
            save_ifc_file(session_id, next_slot, data, fname)
            next_slot += 1
        except ValueError as exc:
            errors.append(f"'{_e(fname)}': {_e(str(exc))}")
        except Exception as exc:
            errors.append(f"'{_e(fname)}': Speicherfehler – {_e(str(exc))}")

    target_url = _viewer_url(session_id, "viewer", project_id)
    if errors:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}error={urllib.parse.quote('; '.join(errors))}"
    response = RedirectResponse(url=target_url, status_code=303)
    # Kein bimpruef_session-Cookie mehr setzen – die Session-ID wird
    # ausschließlich im sessionStorage des Browsers verwaltet.
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Datei entfernen
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/remove/")
async def viewer_remove(session_id: str = Form(...), slot: int = Form(...), project_id: str = Form(default="")):
    if session_exists(session_id):
        remove_ifc_slot(session_id, slot)
    return RedirectResponse(url=_viewer_url(session_id, "viewer", project_id), status_code=303)


# ─────────────────────────────────────────────────────────────────────────────
# Element-Eigenschaften aktualisieren (JSON API)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/element/update/")
async def viewer_element_update(request: Request):
    """
    Aktualisiert Eigenschaften eines IFC-Elements (Name, ObjectType, Description,
    Tag, PredefinedType und beliebige Property Sets) und speichert die
    geänderte Datei zurück in den Session-Slot.

    Request-Body (JSON):
    {
      "session_id": "...",
      "slot": 1,
      "express_id": 42,
      "changes": {
        "Name": "Neuer Name",
        "ObjectType": "...",
        "Description": "...",
        "Tag": "...",
        "PredefinedType": "...",
        "psets": {
          "Pset_WallCommon": {"LoadBearing": "true", "IsExternal": "false"},
          "MeinNeuesPset":   {"Eigenschaft1": "Wert1"}
        }
      }
    }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiger JSON-Body."}, status_code=400)

    session_id = body.get("session_id", "")
    slot       = int(body.get("slot", 1))
    express_id = int(body.get("express_id", 0))
    changes    = body.get("changes", {})

    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return JSONResponse({"error": f"Slot {slot} nicht gefunden."}, status_code=404)

    try:
        import ifcopenshell
        import ifcopenshell.util.element
        import ifcopenshell.api

        model  = ifcopenshell.open(path)
        elem   = model.by_id(express_id)
        if elem is None:
            return JSONResponse({"error": f"Element #{express_id} nicht gefunden."}, status_code=404)

        ifc_type = elem.is_a()

        # ── Einfache Attribute ──────────────────────────────────────────────
        # IFC-Typ (is_a) ist unveränderlich – wird hier bewusst ausgelassen.
        simple_attrs = ["Name", "ObjectType", "Description", "Tag", "PredefinedType"]
        for attr in simple_attrs:
            if attr in changes:
                val = changes[attr]
                try:
                    setattr(elem, attr, val if val != "" else None)
                except Exception:
                    pass  # Attribut existiert nicht in diesem IFC-Typ → ignorieren

        # ── Property Sets ───────────────────────────────────────────────────
        pset_changes = changes.get("psets", {})
        for pset_name, props in pset_changes.items():
            # Vorhandenes PSet suchen
            existing_pset = None
            try:
                all_psets = ifcopenshell.util.element.get_psets(elem, psets_only=True)
                if pset_name in all_psets:
                    # PSet-Objekt aus dem Modell holen
                    for rel in model.by_type("IfcRelDefinesByProperties"):
                        pdef = rel.RelatingPropertyDefinition
                        if (pdef.is_a("IfcPropertySet") and
                                pdef.Name == pset_name and
                                elem in rel.RelatedObjects):
                            existing_pset = pdef
                            break
            except Exception:
                pass

            if existing_pset is None:
                # Neues PSet anlegen – erst manuell die Entität erzeugen,
                # dann mit IfcRelDefinesByProperties verknüpfen.
                # (ifcopenshell.api.run("pset.add_pset") ist nur in neueren
                #  Versionen verfügbar und kann den Fallback auslösen.)
                try:
                    existing_pset = ifcopenshell.api.run(
                        "pset.add_pset", model, product=elem, name=pset_name
                    )
                except Exception:
                    import uuid as _uuid
                    existing_pset = model.create_entity(
                        "IfcPropertySet",
                        GlobalId=ifcopenshell.guid.new(),
                        OwnerHistory=None,
                        Name=pset_name,
                        Description=None,
                        HasProperties=[],
                    )
                    model.create_entity(
                        "IfcRelDefinesByProperties",
                        GlobalId=ifcopenshell.guid.new(),
                        OwnerHistory=None,
                        Name=None,
                        Description=None,
                        RelatedObjects=[elem],
                        RelatingPropertyDefinition=existing_pset,
                    )

            # Eigenschaften im PSet setzen.
            # Leere Property-Namen überspringen.
            for prop_name, prop_val in props.items():
                prop_name = (prop_name or "").strip()
                if not prop_name:
                    continue

                # Vorhandene Property suchen
                found_prop = None
                current_props = list(existing_pset.HasProperties or [])
                for p in current_props:
                    try:
                        if (p.is_a("IfcPropertySingleValue") and
                                str(getattr(p, "Name", "") or "") == prop_name):
                            found_prop = p
                            break
                    except Exception:
                        pass

                # Nominalwert als IfcText-Entity verpacken
                # (IfcText ist in allen IFC-Versionen ein gültiger Measure-Typ)
                try:
                    nominal = model.create_entity("IfcText", str(prop_val))
                except Exception:
                    try:
                        nominal = model.create_entity("IfcLabel", str(prop_val))
                    except Exception:
                        nominal = str(prop_val)

                if found_prop is not None:
                    try:
                        found_prop.NominalValue = nominal
                    except Exception:
                        pass
                else:
                    try:
                        new_prop = model.create_entity(
                            "IfcPropertySingleValue",
                            Name=prop_name,
                            Description=None,
                            NominalValue=nominal,
                            Unit=None,
                        )
                        current_props.append(new_prop)
                        existing_pset.HasProperties = current_props
                    except Exception:
                        pass

        # ── Geändertes Modell speichern ─────────────────────────────────────
        model.write(path)

        # Aktualisierte Daten für die UI zurückgeben
        updated_psets = {}
        try:
            updated_psets = ifcopenshell.util.element.get_psets(elem) or {}
        except Exception:
            pass

        # Convert pset values to strings for JSON
        # ifcopenshell.util.element.get_psets() injects an "id" key (the PSet's
        # express-ID) into every property dict. We strip it here so it is never
        # shown in the UI and never written back as a real IFC property.
        clean_psets = {}
        for pn, pv in updated_psets.items():
            if isinstance(pv, dict):
                clean_psets[pn] = {k: str(v) for k, v in pv.items() if k != "id"}

        return JSONResponse({
            "ok":          True,
            "ifc_type":    ifc_type,
            "name":        str(getattr(elem, "Name", "") or ""),
            "object_type": str(getattr(elem, "ObjectType", "") or ""),
            "description": str(getattr(elem, "Description", "") or ""),
            "tag":         str(getattr(elem, "Tag", "") or ""),
            "psets":       clean_psets,
        })

    except Exception as exc:
        return JSONResponse({"error": f"Fehler beim Aktualisieren: {exc}"}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# Modifizierte IFC-Datei exportieren
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/export-ifc/")
def viewer_export_ifc(
    session_id: str = Query(...),
    slot: int       = Query(default=1),
    fmt: str        = Query(default="ifc"),   # "ifc" oder "ifczip"
):
    """Liefert die (ggf. bearbeitete) IFC-Datei als Download."""
    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)
    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        return Response(content=f"Slot {slot} nicht gefunden.", status_code=404)

    label = get_ifc_label(session_id, slot, f"model_{slot}.ifc")
    base  = label.rsplit(".", 1)[0] if "." in label else label

    with open(path, "rb") as f:
        ifc_bytes = f.read()

    if fmt == "ifczip":
        buf = io.BytesIO()
        import zipfile
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(base + ".ifc", ifc_bytes)
        buf.seek(0)
        return Response(
            content=buf.read(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{base}_bearbeitet.ifczip"'},
        )
    else:
        return Response(
            content=ifc_bytes,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{base}_bearbeitet.ifc"'},
        )


# ─────────────────────────────────────────────────────────────────────────────
# Viewer-Hauptseite
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/", response_class=HTMLResponse)
def viewer_main(request: Request, session_id: str = Query(default=""), error: str = Query(default=""), project_id: str = Query(default="")):

    # Keine Cookie-Session-Wiederverwendung mehr.
    # Falls session_id fehlt oder ungültig → neue Session erstellen.
    if not session_id or not session_exists(session_id):
        session_id = create_upload_session()

    sid    = _e(session_id)
    slots  = get_session_slots(session_id)
    labels = {s: _e(get_ifc_label(session_id, s)) for s in slots}

    # JS-Array mit Modell-URLs
    model_urls_js = ",\n".join(
        f'{{url:"/viewer/file/?session_id={sid}&slot={s}",'
        f'label:{repr(labels[s])},slot:{s},color:{repr(_slot_color(s))}}}'
        for s in slots
    )

    # Hinweis wenn leer
    empty_hint = "" if slots else """
<div class="flash-ok" style="margin:8px 16px 0">
  💡 Noch keine Dateien hochgeladen. Bitte unten IFC-Dateien hinzufügen.
</div>"""

    error_html = f'<div class="flash-err" style="margin:8px 16px 0">⚠ {_e(error)}</div>' if error else ""

    # Modell-Karten Sidebar
    model_cards = ""
    for s in slots:
        col = _slot_color(s)
        lbl = labels[s]
        escaped_lbl = lbl.replace("'", "\\'").replace("\\", "\\\\")
        model_cards += f"""
<div class="model-card">
  <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">
    <input type="checkbox" class="chk-model" data-slot="{s}" checked
      style="accent-color:{col};width:13px;height:13px;flex-shrink:0">
    <span style="width:10px;height:10px;border-radius:50%;border:2px solid {col};
      flex-shrink:0;display:inline-block"></span>
    <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
      title="{lbl}">{lbl}</span>
    <form method="post" action="/viewer/remove/" style="display:inline;margin:0"
      onsubmit="return confirm('Datei \\'{escaped_lbl}\\' wirklich schließen?')">
      <input type="hidden" name="session_id" value="{sid}">
      <input type="hidden" name="project_id" value="{_e(project_id)}">
      <input type="hidden" name="slot" value="{s}">
      <button type="submit" title="Entfernen"
        style="padding:1px 6px;font-size:10px;background:#2a0d14;
        border:1px solid #6e1a2e;color:#ff8080;border-radius:3px;cursor:pointer">✕</button>
    </form>
  </label>
</div>"""

    # Upload-Bereich
    remaining = MAX_FILES_PER_SESSION - len(slots)
    if remaining > 0:
        upload_html = f"""
<div style="padding:8px 10px;border-bottom:1px solid var(--border)">
  <form id="upload-form" method="post" action="/viewer/upload/" enctype="multipart/form-data">
    <input type="hidden" name="session_id" value="{sid}">
    <input type="hidden" name="project_id" value="{_e(project_id)}">
    <input type="file" id="upload-file-input" name="files" multiple accept=".ifc,.ifczip"
      style="display:none">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
      <span style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">
        Modelle ({len(slots)}/{MAX_FILES_PER_SESSION})
      </span>
      <button type="button" id="btn-upload-trigger" class="btn btn-primary"
        style="font-size:11px;padding:3px 10px">↑ Hochladen</button>
    </div>
  </form>
  <div style="font-size:10px;color:var(--muted);margin-top:2px">
    .ifc &amp; .ifczip · max. {MAX_FILE_SIZE_MB} MB
  </div>
</div>
<script>
(function(){{
  const btn   = document.getElementById('btn-upload-trigger');
  const inp   = document.getElementById('upload-file-input');
  const form  = document.getElementById('upload-form');
  if (btn && inp && form) {{
    btn.addEventListener('click', () => inp.click());
    inp.addEventListener('change', () => {{ if (inp.files.length > 0) form.submit(); }});
  }}
}})();
</script>"""
    else:
        upload_html = f'<div style="padding:6px 10px;font-size:11px;color:var(--muted)">Limit {MAX_FILES_PER_SESSION} erreicht.</div>'

    load_txt = "IFC-Dateien werden geladen …" if slots else "Keine Modelle – bitte Dateien hochladen."

    body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">

  {_topbar(session_id, "viewer", project_id=project_id)}
  {error_html}{empty_hint}

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- Sidebar -->
    <div style="width:220px;min-width:220px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between">
        <span>Modelle</span>
      </div>
      {upload_html}
      {model_cards}

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;margin-top:2px">
        <span>IFC-Struktur</span>
        <span>
          <button id="btn-cat-all"  style="font-size:10px;cursor:pointer;color:#6af;
            background:none;border:none;padding:0">Alle</button>&nbsp;
          <button id="btn-cat-none" style="font-size:10px;cursor:pointer;color:#6af;
            background:none;border:none;padding:0">Keine</button>
        </span>
      </div>
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0">
        <div style="padding:8px 10px;font-size:11px;color:var(--muted);font-style:italic">
          {load_txt}
        </div>
      </div>
    </div>

    <!-- Canvas -->
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden">
      <canvas id="three-canvas"
        style="width:100%!important;height:100%!important;display:block"></canvas>

      <!-- GlobalId-Suchfeld -->
      <div id="search-bar" style="position:absolute;top:8px;left:8px;z-index:10;width:320px">
        <div style="display:flex;gap:4px;align-items:center">
          <input id="gid-search" type="text" placeholder="🔍 GlobalId suchen …"
            style="flex:1;background:rgba(14,20,36,.92);border:1px solid var(--border);
            color:var(--text);padding:5px 10px;border-radius:6px;font-size:12px;
            outline:none;backdrop-filter:blur(4px)"
            autocomplete="off" spellcheck="false">
          <button id="search-clear" style="display:none;background:rgba(14,20,36,.92);
            border:1px solid var(--border);color:var(--muted);border-radius:6px;
            padding:5px 8px;font-size:12px;cursor:pointer" title="Suche leeren">✕</button>
        </div>
        <div id="search-results" style="display:none;margin-top:4px;
          background:rgba(14,20,36,.97);border:1px solid var(--border);
          border-radius:6px;max-height:260px;overflow-y:auto;
          backdrop-filter:blur(4px)"></div>
      </div>

      <!-- Overlay-Buttons -->
      <div style="position:absolute;top:8px;right:8px;display:flex;gap:4px;z-index:6">
        <button id="btn-fit"   class="btn" style="font-size:11px;padding:4px 9px">⊡ Einpassen</button>
        <button id="btn-reset" class="btn" style="font-size:11px;padding:4px 9px">⟳ Kamera</button>
        <button id="btn-show-all" class="btn btn-danger"
          style="font-size:11px;padding:4px 9px;display:none">👁 Alle einblenden</button>
        <span id="hidden-count"
          style="font-size:11px;color:var(--accent2);display:none;align-self:center"></span>
      </div>
      <div style="position:absolute;bottom:8px;right:8px;font-size:10px;
        color:#445;pointer-events:none">
        LMB Drehen · MMB Pan · Rad Zoom · Leertaste: ausblenden
      </div>

      <!-- Lade-Overlay -->
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:rgba(14,14,26,.93);z-index:20">
        <div style="width:40px;height:40px;border:4px solid #0f3460;
          border-top-color:var(--accent2);border-radius:50%;
          animation:spin .7s linear infinite;margin-bottom:12px"></div>
        <p id="load-txt" style="color:#889;font-size:13px">{load_txt}</p>
      </div>
    </div>

    <!-- Info-Panel -->
    <div id="info-panel" style="width:300px;min-width:300px;background:var(--surface);
      border-left:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <span>Element-Info</span>
        <span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px"
          title="Schließen">✕</span>
      </div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:10px;font-size:12px">
        <div style="color:var(--muted);font-style:italic">
          Klick auf ein Element für Details.
        </div>
      </div>
    </div>

  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_viewer_js(model_urls_js, session_id=session_id)}
</script>

{_ai_chat_widget(session_id, slots)}

<script>
/* ── Session-Isolierung: Session-ID im sessionStorage halten ──────────────
   sessionStorage ist Tab/Fenster-spezifisch und wird beim Schließen
   des Tabs/Fensters automatisch vom Browser gelöscht.
   Zusätzlich senden wir beim Schließen einen DELETE-Request an den
   Server, damit die hochgeladenen Dateien sofort bereinigt werden.   */
(function() {{
  const SESSION_ID = "{_e(session_id)}";

  // Session-ID im sessionStorage des aktuellen Tabs speichern
  try {{ sessionStorage.setItem("bimpruef_session", SESSION_ID); }} catch(e) {{}}

  // Hilfsfunktion: Session serverseitig löschen
  function deleteSession() {{
    const url = "/session/delete/";
    const body = JSON.stringify({{session_id: SESSION_ID}});
    // navigator.sendBeacon ist für beforeunload/pagehide zuverlässig
    if (navigator.sendBeacon) {{
      const blob = new Blob([body], {{type: "application/json"}});
      navigator.sendBeacon(url, blob);
    }} else {{
      // Synchroner Fallback für ältere Browser
      try {{
        const xhr = new XMLHttpRequest();
        xhr.open("DELETE", url, false);
        xhr.setRequestHeader("Content-Type", "application/json");
        xhr.send(body);
      }} catch(e) {{}}
    }}
  }}

  // pagehide ist zuverlässiger als beforeunload (funktioniert auch auf Mobile)
  window.addEventListener("pagehide", function(e) {{
    // Nur löschen wenn das Dokument nicht im BFCache gehalten wird
    // (persisted=false bedeutet: Tab/Fenster wird wirklich geschlossen
    //  oder auf eine externe Seite navigiert)
    if (!e.persisted) {{
      deleteSession();
    }}
  }});

  // Zusätzlich beforeunload als Fallback für Desktop-Browser
  window.addEventListener("beforeunload", function() {{
    deleteSession();
  }});
}})();
</script>"""

    return _page("BIMPruef 3D-Viewer", body)


# ─────────────────────────────────────────────────────────────────────────────
# KI-Chat-Widget
# ─────────────────────────────────────────────────────────────────────────────

def _ai_chat_widget(session_id: str, slots: list) -> str:
    """Renders the floating AI chat box for the viewer page."""
    if not slots:
        return ""

    sid = _e(session_id)

    # Build slot selector options
    slot_options = "".join(
        f'<option value="{s}">Slot {s}</option>'
        for s in slots
    )

    # Suggested questions
    suggestions = [
        "Wie viele Räume hat dieses Modell?",
        "Gibt es Flure und wie hoch sind diese?",
        "Welche Wandtypen gibt es?",
        "Wie viele Türen und Fenster sind vorhanden?",
        "Was sind die Abmessungen der Stockwerke?",
    ]
    suggestion_btns = "".join(
        f'<button class="ai-suggestion" onclick="aiSuggest({repr(s)})">{_e(s)}</button>'
        for s in suggestions
    )

    return f"""
<!-- ═══════════════════════════════════════════════════════════════════════════
     KI-Assistent Chat Widget
     ═══════════════════════════════════════════════════════════════════════════ -->
<style>
#ai-chat-btn {{
  position:fixed;bottom:24px;right:24px;z-index:1000;
  width:52px;height:52px;border-radius:50%;
  background:linear-gradient(135deg,#4fc3f7,#1565c0);
  border:none;cursor:pointer;box-shadow:0 4px 18px rgba(0,0,0,.5);
  display:flex;align-items:center;justify-content:center;
  font-size:22px;transition:transform .2s,box-shadow .2s;
  color:#fff;
}}
#ai-chat-btn:hover {{transform:scale(1.08);box-shadow:0 6px 24px rgba(79,195,247,.4)}}
#ai-chat-panel {{
  position:fixed;bottom:88px;right:24px;z-index:1000;
  width:380px;max-height:520px;
  background:#12192e;border:1px solid #1e3a6e;border-radius:14px;
  box-shadow:0 8px 40px rgba(0,0,0,.7);
  display:none;flex-direction:column;overflow:hidden;
  font-family:'Segoe UI',system-ui,sans-serif;
}}
#ai-chat-panel.open {{display:flex}}
.ai-header {{
  display:flex;align-items:center;gap:8px;
  padding:10px 14px;background:#0f2040;border-bottom:1px solid #1e3a6e;flex-shrink:0;
}}
.ai-header-icon {{
  width:28px;height:28px;border-radius:50%;
  background:linear-gradient(135deg,#4fc3f7,#1565c0);
  display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;
}}
.ai-header-title {{font-size:13px;font-weight:700;color:#4fc3f7;flex:1}}
.ai-header-sub {{font-size:10px;color:#4a6080}}
.ai-close {{cursor:pointer;color:#4a6080;font-size:18px;padding:0 2px;background:none;border:none;color:#6a8090}}
.ai-slot-row {{
  padding:6px 12px;border-bottom:1px solid #0f1e38;flex-shrink:0;
  display:flex;align-items:center;gap:6px;
}}
.ai-slot-label {{font-size:10px;color:#4a6080}}
.ai-slot-select {{
  background:#0e1a30;border:1px solid #1e3a6e;color:#a0c0d8;
  padding:3px 8px;border-radius:5px;font-size:11px;cursor:pointer;
}}
.ai-messages {{
  flex:1;overflow-y:auto;padding:10px 12px;display:flex;flex-direction:column;gap:8px;
}}
.ai-msg {{
  max-width:92%;padding:8px 11px;border-radius:10px;font-size:12px;line-height:1.5;
  word-break:break-word;
}}
.ai-msg.user {{
  background:#1a3a6e;color:#d0e8ff;align-self:flex-end;
  border-bottom-right-radius:3px;
}}
.ai-msg.assistant {{
  background:#0e1e38;color:#c8dcea;align-self:flex-start;
  border:1px solid #1a2e50;border-bottom-left-radius:3px;
}}
.ai-msg.error {{
  background:#2a0a10;color:#ffaaaa;border:1px solid #6e1a2e;
  align-self:flex-start;font-size:11px;
}}
.ai-msg.typing {{
  background:#0e1e38;color:#4a7090;border:1px solid #1a2e50;
  align-self:flex-start;font-style:italic;font-size:11px;
}}
.ai-suggestions {{
  padding:6px 10px 4px;border-top:1px solid #0f1e38;flex-shrink:0;overflow-x:auto;
  display:flex;gap:5px;flex-wrap:wrap;
}}
.ai-suggestion {{
  background:#0e1a30;border:1px solid #1e3a6e;color:#7aaec8;
  padding:3px 8px;border-radius:12px;font-size:10px;cursor:pointer;
  white-space:nowrap;transition:background .15s;
}}
.ai-suggestion:hover {{background:#1a2e50;color:#a0d0f0}}
.ai-input-row {{
  display:flex;gap:6px;padding:8px 10px;border-top:1px solid #0f1e38;flex-shrink:0;
}}
.ai-input {{
  flex:1;background:#0e1a30;border:1px solid #1e3a6e;color:#c8dcea;
  padding:7px 10px;border-radius:8px;font-size:12px;outline:none;
  resize:none;height:36px;font-family:inherit;line-height:1.4;
}}
.ai-input:focus {{border-color:#4fc3f7}}
.ai-send {{
  background:linear-gradient(135deg,#4fc3f7,#1565c0);border:none;
  color:#fff;padding:0 14px;border-radius:8px;cursor:pointer;font-size:16px;
  flex-shrink:0;transition:opacity .15s;
}}
.ai-send:hover {{opacity:.85}}
.ai-send:disabled {{opacity:.4;cursor:not-allowed}}
</style>

<!-- Floating Button -->
<button id="ai-chat-btn" title="KI-Assistent öffnen" onclick="toggleAiChat()">🤖</button>

<!-- Chat Panel -->
<div id="ai-chat-panel">
  <div class="ai-header">
    <div class="ai-header-icon">🤖</div>
    <div style="flex:1">
      <div class="ai-header-title">IFC-Assistent</div>
      <div class="ai-header-sub">Fragen zum Modell stellen</div>
    </div>
    <button class="ai-close" onclick="toggleAiChat()" title="Schließen">✕</button>
  </div>

  <div class="ai-slot-row">
    <span class="ai-slot-label">Modell:</span>
    <select id="ai-slot-select" class="ai-slot-select">
      {slot_options}
    </select>
  </div>

  <div id="ai-messages" class="ai-messages">
    <div class="ai-msg assistant">
      Hallo! Ich bin dein IFC-Assistent. Ich habe das Modell analysiert und kann dir
      Fragen dazu beantworten – z.&nbsp;B. über Räume, Abmessungen, Wandtypen, Türen,
      Fenster und vieles mehr. Was möchtest du wissen?
    </div>
  </div>

  <div class="ai-suggestions" id="ai-suggestions-bar">
    {suggestion_btns}
  </div>

  <div class="ai-input-row">
    <textarea
      id="ai-input"
      class="ai-input"
      placeholder="Frage stellen …"
      rows="1"
      onkeydown="aiInputKeydown(event)"></textarea>
    <button id="ai-send-btn" class="ai-send" onclick="aiSend()" title="Senden">➤</button>
  </div>
</div>

<script>
(function() {{
  const SESSION_ID = {repr(sid)};

  function toggleAiChat() {{
    const panel = document.getElementById('ai-chat-panel');
    panel.classList.toggle('open');
    if (panel.classList.contains('open')) {{
      setTimeout(() => document.getElementById('ai-input').focus(), 80);
    }}
  }}
  window.toggleAiChat = toggleAiChat;

  function aiSuggest(text) {{
    document.getElementById('ai-input').value = text;
    document.getElementById('ai-input').focus();
  }}
  window.aiSuggest = aiSuggest;

  function aiInputKeydown(e) {{
    if (e.key === 'Enter' && !e.shiftKey) {{
      e.preventDefault();
      aiSend();
    }}
  }}
  window.aiInputKeydown = aiInputKeydown;

  async function aiSend() {{
    const inputEl = document.getElementById('ai-input');
    const sendBtn = document.getElementById('ai-send-btn');
    const question = inputEl.value.trim();
    if (!question) return;

    const slot = parseInt(document.getElementById('ai-slot-select').value) || 1;

    appendMsg(question, 'user');
    inputEl.value = '';

    const typingId = appendMsg('Analysiere Modell …', 'typing');
    sendBtn.disabled = true;

    try {{
      const form = new FormData();
      form.append('session_id', SESSION_ID);
      form.append('slot', slot);
      form.append('question', question);

      const resp = await fetch('/viewer/ai-chat/', {{
        method: 'POST',
        body: form,
      }});

      const data = await resp.json();
      removeMsg(typingId);

      if (data.error) {{
        appendMsg('⚠ ' + data.error, 'error');
      }} else {{
        appendMsg(data.answer || '(Keine Antwort)', 'assistant');
      }}
    }} catch (err) {{
      removeMsg(typingId);
      appendMsg('⚠ Verbindungsfehler: ' + err.message, 'error');
    }} finally {{
      sendBtn.disabled = false;
      inputEl.focus();
    }}
  }}
  window.aiSend = aiSend;

  let _msgCounter = 0;
  function appendMsg(text, role) {{
    const id = 'aimsg-' + (++_msgCounter);
    const el = document.createElement('div');
    el.className = 'ai-msg ' + role;
    el.id = id;
    // Newlines → <br>
    el.innerHTML = text.replace(/\\n/g, '<br>');
    const container = document.getElementById('ai-messages');
    container.appendChild(el);
    container.scrollTop = container.scrollHeight;
    return id;
  }}

  function removeMsg(id) {{
    const el = document.getElementById(id);
    if (el) el.remove();
  }}
}})();
</script>"""


# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsamer 3D-Viewer JavaScript-Block
# ─────────────────────────────────────────────────────────────────────────────

def _viewer_js(model_urls_js: str, highlight_gids: list = None, session_id: str = "") -> str:
    """
    Vollständiger JS-Block für den 3D-Viewer.
    model_urls_js: komma-getrennter JS-Array-Inhalt mit {url, label, slot, color}
    highlight_gids: wenn gesetzt → alle anderen Elemente werden auf 5 % Opacity gedimmt
    session_id: wird als globale JS-Variable SESSION_ID eingebettet
    """

    if highlight_gids:
        gids_json = "[" + ",".join(f'"{g}"' for g in highlight_gids) + "]"
        highlight_block = f"""
const HIGHLIGHT_GIDS = new Set({gids_json});
function applyClashHighlight() {{
  // Kollisionselemente: Element 0 = leuchtendes Rot, Element 1 = leuchtendes Gelb
  const _clashArr = Array.from(HIGHLIGHT_GIDS);
  const _clashColorMap = {{}};
  _clashArr.forEach((gid, i) => {{
    _clashColorMap[gid] = i === 0 ? new THREE.Color(0xff3333) : new THREE.Color(0xffcc00);
  }});

  // scene.traverse statt allMeshes() – immun gegen modelMeshes-Dict-Fehler
  // (tritt auf wenn slot_a == slot_b und das Dict resettet wird)
  const sceneMeshes = [];
  scene.traverse(obj => {{ if (obj.isMesh) sceneMeshes.push(obj); }});

  for (const m of sceneMeshes) {{
    const gid = m.userData && m.userData.globalId;
    if (gid && HIGHLIGHT_GIDS.has(gid)) {{
      // Kollisionselement: vollständig sichtbar, Solid-Farbe, opak
      m.material.color.set(_clashColorMap[gid] || new THREE.Color(0xff3333));
      m.material.opacity = 1.0;
      m.material.transparent = false;
      m.material.wireframe = false;
      m.material.needsUpdate = true;
      m.visible = true;
    }} else {{
      // Alle anderen (inkl. Elemente ohne globalId wie Grid): stark transparent
      if (m.userData && m.userData.globalId !== undefined) {{
        // IFC-Element: auf 4 % abdunkeln
        m.material.opacity = 0.04;
        m.material.transparent = true;
        m.material.wireframe = false;
        m.material.needsUpdate = true;
        m.visible = true;
      }}
      // Grid, Hilfsobjekte etc. bleiben unverändert
    }}
  }}
  // Highlighted Elemente in Kamera-Fokus
  const box = new THREE.Box3();
  for (const m of sceneMeshes) {{
    if (m.userData && HIGHLIGHT_GIDS.has(m.userData.globalId)) box.expandByObject(m);
  }}
  if (!box.isEmpty()) {{
    const center = box.getCenter(new THREE.Vector3());
    const size   = box.getSize(new THREE.Vector3());
    orb.tgt.copy(center);
    orb.sph.radius = Math.max(size.x, size.y, size.z) * 4;
    applyOrb();
  }}
}}"""
    else:
        highlight_block = "function applyClashHighlight() {}"

    sid_js = repr(session_id)
    return f"const SESSION_ID = {sid_js};\n" + r"""
// ═══════════════════════════════════════════════════════════════════════════
// IFC-Farbpalette
// ═══════════════════════════════════════════════════════════════════════════
const TYPE_COLOR = {
  IfcWall:0xc8a057, IfcWallStandardCase:0xc8a057, IfcCurtainWall:0xf0c040,
  IfcColumn:0x2e6b9e, IfcColumnStandardCase:0x2e6b9e,
  IfcBeam:0x5b9bd5, IfcBeamStandardCase:0x5b9bd5,
  IfcSlab:0x7aaec8, IfcSlabStandardCase:0x7aaec8,
  IfcRoof:0x8b5de5, IfcDoor:0xe07040, IfcWindow:0x70d8f0,
  IfcStair:0xc87050, IfcStairFlight:0xc87050,
  IfcRamp:0xd4a030, IfcRailing:0x607080,
  IfcPlate:0x90b8d0, IfcMember:0x4898b0, IfcCovering:0x88b878,
  IfcFooting:0x2a5070, IfcPile:0x1e3850, IfcChimney:0x8b5e3c,
  IfcBuildingElementProxy:0x888888,
  IfcFurnishingElement:0xe08860, IfcFurniture:0xe08860,
  IfcReinforcingBar:0x708090,
  IfcSite:0x48a048, IfcBuilding:0x60b060,
  IfcBuildingStorey:0x80c880, IfcSpace:0xa8d8a8,
  IfcPipeSegment:0x188880, IfcDuctSegment:0x607080,
  IfcCableSegment:0xe09810, IfcPump:0x3040a0,
  IfcFan:0x4858a8, IfcValve:0x50b898, IfcSensor:0xe080a8,
};
const TYPE_FALLBACK = [
  [/^IfcWall/,0xc8a057],[/^IfcSlab/,0x7aaec8],[/^IfcColumn/,0x2e6b9e],
  [/^IfcBeam/,0x5b9bd5],[/^IfcStair/,0xc87050],[/^IfcRoof/,0x8b5de5],
  [/^IfcDoor/,0xe07040],[/^IfcWindow/,0x70d8f0],[/^IfcPipe/,0x188880],
  [/^IfcDuct/,0x607080],[/^IfcCable/,0xe09810],[/^IfcPump/,0x3040a0],
  [/^IfcFan/,0x4858a8],[/^IfcFurnish/,0xe08860],[/^IfcFurniture/,0xe08860],
  [/^IfcElectric/,0xe0b830],[/^IfcSanitary/,0x30a8d0],[/^IfcBoiler/,0xe05828],
];
const TYPE_COLOR_LOWER = {};
for (const k of Object.keys(TYPE_COLOR))
  TYPE_COLOR_LOWER[k.toLowerCase()] = TYPE_COLOR[k];

function getColor(t) {
  if (TYPE_COLOR[t] !== undefined) return new THREE.Color(TYPE_COLOR[t]);
  const l = t.toLowerCase();
  if (TYPE_COLOR_LOWER[l] !== undefined) return new THREE.Color(TYPE_COLOR_LOWER[l]);
  for (const [rx, hex] of TYPE_FALLBACK) if (rx.test(t)) return new THREE.Color(hex);
  return new THREE.Color(0x777788);
}

// ═══════════════════════════════════════════════════════════════════════════
// Three.js Setup
// ═══════════════════════════════════════════════════════════════════════════
const canvas    = document.getElementById("three-canvas");
const wrap      = document.getElementById("canvas-wrap");
const loadingEl = document.getElementById("loading");
const loadTxtEl = document.getElementById("load-txt");
const infoBody  = document.getElementById("info-body");
const infoPanel = document.getElementById("info-panel");

const renderer = new THREE.WebGLRenderer({ canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e0e1a);

const camera = new THREE.PerspectiveCamera(60, 1, 0.01, 10000);
camera.position.set(20, 20, 20);

scene.add(new THREE.AmbientLight(0xffffff, 0.55));
const dirL = new THREE.DirectionalLight(0xffffff, 0.85);
dirL.position.set(60, 100, 60);
scene.add(dirL);
scene.add(new THREE.HemisphereLight(0xddeeff, 0x100c08, 0.3));

const gridHelper = new THREE.GridHelper(200, 40, 0x1a2a3a, 0x151f30);
scene.add(gridHelper);

function onResize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", onResize);
onResize();

// ── Orbit-Steuerung ─────────────────────────────────────────────────────────
const orb = {
  sph: new THREE.Spherical(80, Math.PI / 4, Math.PI / 4),
  tgt: new THREE.Vector3(),
  drag: false, pan: false, lx: 0, ly: 0,
};
function applyOrb() {
  camera.position.copy(
    new THREE.Vector3().setFromSpherical(orb.sph).add(orb.tgt)
  );
  camera.lookAt(orb.tgt);
}
applyOrb();

canvas.addEventListener("mousedown", e => {
  if (e.button === 0) orb.drag = true;
  if (e.button === 1) { orb.pan = true; e.preventDefault(); }
  orb.lx = e.clientX; orb.ly = e.clientY;
});
window.addEventListener("mouseup", () => { orb.drag = false; orb.pan = false; });
window.addEventListener("mousemove", e => {
  const dx = e.clientX - orb.lx, dy = e.clientY - orb.ly;
  orb.lx = e.clientX; orb.ly = e.clientY;
  if (orb.drag) {
    orb.sph.theta -= dx * 0.005;
    orb.sph.phi = Math.max(0.04, Math.min(Math.PI - 0.04, orb.sph.phi + dy * 0.005));
    applyOrb();
  }
  if (orb.pan) {
    const r = new THREE.Vector3()
      .crossVectors(camera.getWorldDirection(new THREE.Vector3()), camera.up)
      .normalize();
    const sc = orb.sph.radius * 0.001;
    orb.tgt.addScaledVector(r, -dx * sc);
    orb.tgt.addScaledVector(camera.up.clone().normalize(), dy * sc);
    applyOrb();
  }
});
canvas.addEventListener("wheel", e => {
  orb.sph.radius = Math.max(0.5, Math.min(5000, orb.sph.radius * (1 + e.deltaY * 0.001)));
  applyOrb();
  e.preventDefault();
}, { passive: false });

// ═══════════════════════════════════════════════════════════════════════════
// Zustand
// ═══════════════════════════════════════════════════════════════════════════
const modelMeshes  = {};   // slot → Mesh[]
const modelGroups  = {};   // slot → THREE.Group
const catVisible   = {};   // "slot:typeName" → bool
const hiddenIds    = new Set();
const catCounts    = {};   // "slot:typeName" → count
const catElements  = {};   // "slot:typeName" → [{name, expressId, globalId}]
const slotMeta     = {};   // slot → {label, color}

// 2D / Void types – hidden by default, rendered as black wireframe lines
const FLAT_TYPES = new Set([
  "IfcOpeningElement","IfcVoidingFeature","IfcAnnotation",
  "IfcGrid","IfcGridAxis","IfcSpace",
]);

function catKey(slot, type) { return slot + ":" + type; }

function allMeshes() {
  return Object.values(modelMeshes).flat();
}

// ─── Sichtbarkeit ──────────────────────────────────────────────────────────
function applyVisibility() {
  for (const m of allMeshes()) {
    const key = catKey(m.userData.slot, m.userData.ifcType);
    const catOn = catVisible[key] !== false;
    const notHidden = !hiddenIds.has(m.userData.expressId);
    const isFlat = FLAT_TYPES.has(m.userData.ifcType);
    m.visible = catOn && notHidden;
    if (m.visible && isFlat) {
      // Render as black wireframe lines
      m.material.color.set(0x000000);
      m.material.wireframe = true;
      m.material.opacity = 1.0;
    } else if (m.visible && !m.userData._colorApplied) {
      // restore original color if it was changed
    }
  }
  updateHiddenCount();
}

function updateHiddenCount() {
  const n   = hiddenIds.size;
  const el  = document.getElementById("hidden-count");
  const btn = document.getElementById("btn-show-all");
  if (n > 0) {
    el.textContent  = `${n} ausgeblendet`;
    el.style.display = "inline";
    if (btn) btn.style.display = "inline";
  } else {
    el.style.display = "none";
    if (btn) btn.style.display = "none";
  }
}

// ─── Kategorie-UI (per-slot tree, 3 levels) ────────────────────────────────
function buildCategoryUI() {
  const list = document.getElementById("cat-scroll");
  if (!list) return;
  list.innerHTML = "";

  // Group catCounts by slot
  const bySlot = {};
  for (const key of Object.keys(catCounts)) {
    const colonIdx = key.indexOf(":");
    const slot = parseInt(key.slice(0, colonIdx));
    const type = key.slice(colonIdx + 1);
    if (!bySlot[slot]) bySlot[slot] = {};
    bySlot[slot][type] = catCounts[key];
  }

  for (const slot of Object.keys(bySlot).map(Number).sort()) {
    const meta = slotMeta[slot] || {};
    const col  = meta.color || "#4fc3f7";
    const lbl  = meta.label || ("Slot " + slot);

    // ── Level 1: File header ──
    const fileRow = document.createElement("div");
    fileRow.style.cssText = "padding:4px 8px;background:#0d1f38;border-bottom:1px solid #1a2e50;" +
      "display:flex;align-items:center;gap:5px;cursor:pointer;user-select:none;flex-shrink:0";
    fileRow.innerHTML =
      `<span class="ftog" style="color:#6af;font-size:10px;font-family:monospace;width:10px;flex-shrink:0">▼</span>` +
      `<span style="width:9px;height:9px;border-radius:50%;border:2px solid ${col};flex-shrink:0;display:inline-block"></span>` +
      `<span style="font-size:11px;font-weight:600;color:${col};flex:1;overflow:hidden;` +
        `text-overflow:ellipsis;white-space:nowrap" title="${esc(lbl)}">${esc(lbl)}</span>`;
    list.appendChild(fileRow);

    // ── Level 1 body: type list ──
    const typeList = document.createElement("div");
    typeList.dataset.slotTree = slot;

    for (const type of Object.keys(bySlot[slot]).sort()) {
      const key    = catKey(slot, type);
      const vis    = catVisible[key] !== false;
      const typeCol = "#" + getColor(type).getHexString();
      const count  = bySlot[slot][type];
      const isFlat = FLAT_TYPES.has(type);
      const elems  = catElements[key] || [];

      // ── Level 2: Category row ──
      const catRow = document.createElement("div");
      catRow.style.cssText = "display:flex;align-items:center;gap:5px;padding:3px 6px 3px 10px;" +
        "cursor:pointer;user-select:none;border-bottom:1px solid #111e35;" +
        `opacity:${vis ? "1" : ".45"};`;
      catRow.dataset.catKey = key;
      catRow.innerHTML =
        `<span class="ctog" style="color:#446;font-size:9px;font-family:monospace;width:10px;flex-shrink:0">▶</span>` +
        `<input class="cat-cb" type="checkbox" ${vis ? "checked" : ""}
           data-cat-key="${esc(key)}"
           style="width:12px;height:12px;accent-color:#4af;flex-shrink:0">` +
        `<span style="width:10px;height:10px;border-radius:50%;` +
          `background:${isFlat ? "#222" : typeCol};` +
          `border:${isFlat ? "1px solid #666" : "none"};` +
          `flex-shrink:0;display:inline-block"></span>` +
        `<span style="font-size:11px;color:${isFlat ? "#667" : "#bcd"};flex:1;overflow:hidden;` +
          `text-overflow:ellipsis;white-space:nowrap" title="${esc(type)}">${esc(type)}</span>` +
        `<span style="font-size:10px;color:#556;flex-shrink:0">${count}</span>`;
      typeList.appendChild(catRow);

      // ── Level 3: Element list (hidden by default) ──
      const elemList = document.createElement("div");
      elemList.style.display = "none";
      elemList.dataset.elemList = key;

      for (const el of elems) {
        const eRow = document.createElement("div");
        eRow.style.cssText = "padding:2px 8px 2px 34px;font-size:10px;color:#7a9ab8;" +
          "cursor:pointer;border-bottom:1px solid #0e1a2e;white-space:nowrap;" +
          "overflow:hidden;text-overflow:ellipsis;";
        eRow.title = el.name + (el.globalId ? " · " + el.globalId : "");
        eRow.dataset.expressId = el.expressId;
        eRow.textContent = el.name;
        eRow.addEventListener("mouseenter", () => eRow.style.background = "#162a48");
        eRow.addEventListener("mouseleave", () => eRow.style.background = "");
        eRow.addEventListener("click", e => {
          e.stopPropagation();
          // Find and select the mesh for this expressId
          const target = allMeshes().find(m => m.userData.expressId === el.expressId);
          if (target) {
            if (!target.visible) {
              target.visible = true; // temporarily show for selection
            }
            selectMesh(target);
            // Fly camera to element
            const box = new THREE.Box3().setFromObject(target);
            if (!box.isEmpty()) {
              orb.tgt.copy(box.getCenter(new THREE.Vector3()));
              orb.sph.radius = Math.max(box.getSize(new THREE.Vector3()).length() * 2.5, 2);
              applyOrb();
            }
          }
        });
        elemList.appendChild(eRow);
      }
      typeList.appendChild(elemList);

      // Category row toggle: expand/collapse element list
      catRow.addEventListener("click", e => {
        if (e.target.classList.contains("cat-cb")) return;
        const tog = catRow.querySelector(".ctog");
        const collapsed = elemList.style.display === "none";
        elemList.style.display = collapsed ? "block" : "none";
        tog.style.color = collapsed ? "#6af" : "#446";
        tog.textContent = collapsed ? "▼" : "▶";
      });
    }
    list.appendChild(typeList);

    // File row toggle: expand/collapse type list
    fileRow.addEventListener("click", () => {
      const tog = fileRow.querySelector(".ftog");
      const collapsed = typeList.style.display === "none";
      typeList.style.display = collapsed ? "" : "none";
      tog.textContent = collapsed ? "▼" : "▶";
    });
  }

  // Checkbox handler (delegated to the whole list)
  list.addEventListener("change", e => {
    if (!e.target.classList.contains("cat-cb")) return;
    const key = e.target.dataset.catKey;
    catVisible[key] = e.target.checked;
    const row = e.target.closest("[data-cat-key]");
    if (row) row.style.opacity = e.target.checked ? "1" : ".45";
    applyVisibility();
  });
}

function setCatAll(vis) {
  for (const key of Object.keys(catCounts)) catVisible[key] = vis;
  buildCategoryUI();
  applyVisibility();
}

const btnCatAll  = document.getElementById("btn-cat-all");
const btnCatNone = document.getElementById("btn-cat-none");
if (btnCatAll)  btnCatAll.addEventListener("click",  () => setCatAll(true));
if (btnCatNone) btnCatNone.addEventListener("click", () => setCatAll(false));

// Modell-Checkboxen (delegiert)
document.addEventListener("change", e => {
  if (!e.target.classList.contains("chk-model")) return;
  const slot = parseInt(e.target.dataset.slot);
  if (modelGroups[slot]) modelGroups[slot].visible = e.target.checked;
});

// ═══════════════════════════════════════════════════════════════════════════
// web-ifc laden
// ═══════════════════════════════════════════════════════════════════════════
let webIfc = null;

async function initWebIfc() {
  const mod = await import("https://esm.sh/web-ifc@0.0.57");
  webIfc = new mod.IfcAPI();
  webIfc.SetWasmPath("https://esm.sh/web-ifc@0.0.57/");
  await webIfc.Init();
}

// ═══════════════════════════════════════════════════════════════════════════
// IFC-Modell laden und in Three.js-Szene einbauen
// ═══════════════════════════════════════════════════════════════════════════
async function loadModel(cfg) {
  if (loadTxtEl) loadTxtEl.textContent = `${cfg.label} wird geladen …`;

  const resp = await fetch(cfg.url);
  if (!resp.ok) throw new Error(`HTTP ${resp.status} für ${cfg.label}`);
  const data = new Uint8Array(await resp.arrayBuffer());

  // web-ifc öffnet immer IFC-Daten (IFCZIP wurde serverseitig entpackt)
  const modelId = webIfc.OpenModel(data, {
    COORDINATE_TO_ORIGIN: false,
    USE_FAST_BOOLS: false,
  });

  // ── Alle Zeilen indexieren ──
  const elemIndex = {};
  const allLines  = webIfc.GetAllLines(modelId);
  for (let i = 0; i < allLines.size(); i++) {
    const id = allLines.get(i);
    try {
      const line = webIfc.GetLine(modelId, id, false);
      if (line) elemIndex[id] = line;
    } catch (_) {}
  }

  // ── Typnamen aus TypeCode auflösen ──
  const _typeNameCache = {};
  function resolveTypeCode(code) {
    if (_typeNameCache[code] !== undefined) return _typeNameCache[code];
    let name = "Unknown";
    try {
      const raw = webIfc.GetNameFromTypeCode(code);
      if (raw && raw.length > 0) {
        if (raw.includes("_")) {
          const parts = raw.toLowerCase().replace(/^ifc_/, "").split("_");
          name = "Ifc" + parts.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join("");
        } else {
          const low = raw.toLowerCase();
          name = "Ifc" + low.slice(3).charAt(0).toUpperCase() + low.slice(4);
        }
      }
    } catch (_) {}
    _typeNameCache[code] = name;
    return name;
  }

  function typeName(line) {
    if (!line) return "Unknown";
    if (typeof line.type === "number")  return resolveTypeCode(line.type);
    if (typeof line.type === "string" && line.type.startsWith("Ifc")) return line.type;
    const cn = line.constructor?.name ?? "";
    if (cn.startsWith("Ifc") && cn.length > 5) return cn;
    return "Unknown";
  }

  function sv(v) {
    if (v == null) return "";
    if (typeof v === "object" && v.value !== undefined) return String(v.value);
    return String(v);
  }

  // ── RelDefinesByProperties → PSets ──
  const relMap = {};
  for (const [id, line] of Object.entries(elemIndex)) {
    if (!typeName(line).toLowerCase().includes("reldefinesbyprop")) continue;
    const pref = line.RelatingPropertyDefinition;
    if (!pref) continue;
    const pid  = pref.value ?? pref;
    const rel  = line.RelatedObjects;
    if (!rel) continue;
    const ids  = Array.isArray(rel) ? rel : [rel];
    for (const r of ids) {
      const rid = r?.value ?? r;
      if (!relMap[rid]) relMap[rid] = [];
      relMap[rid].push(pid);
    }
  }

  function getPsets(eid) {
    const res = {};
    for (const pid of relMap[eid] ?? []) {
      const pset = elemIndex[pid];
      if (!pset) continue;
      const pn    = sv(pset.Name) || typeName(pset);
      const props = {};
      const hp    = pset.HasProperties;
      if (hp) {
        const list = Array.isArray(hp) ? hp : [hp];
        for (const ref of list) {
          const id   = ref?.value ?? ref;
          const prop = elemIndex[id];
          if (!prop) continue;
          props[sv(prop.Name) || String(id)] =
            prop.NominalValue != null ? sv(prop.NominalValue) : "–";
        }
      }
      res[pn] = props;
    }
    return res;
  }

  // ── Three.js-Gruppe für dieses Modell ──
  const group = new THREE.Group();
  group.name  = cfg.label;
  scene.add(group);
  modelGroups[cfg.slot] = group;
  modelMeshes[cfg.slot] = [];
  slotMeta[cfg.slot]    = { label: cfg.label, color: cfg.color };

  const fms        = webIfc.LoadAllGeometry(modelId);
  let   vertCount  = 0;
  const seenExpIds = new Set();

  for (let i = 0; i < fms.size(); i++) {
    const fm    = fms.get(i);
    const expId = fm.expressID;
    const line  = elemIndex[expId];
    const tName = typeName(line);
    const isFlat = FLAT_TYPES.has(tName);
    const tCol  = isFlat ? new THREE.Color(0x000000) : getColor(tName);
    const key   = catKey(cfg.slot, tName);

    if (!seenExpIds.has(expId)) {
      seenExpIds.add(expId);
      catCounts[key] = (catCounts[key] ?? 0) + 1;
      if (catVisible[key] === undefined) {
        // 2D/void types hidden by default
        catVisible[key] = !isFlat;
      }
      // Register element in category list
      if (!catElements[key]) catElements[key] = [];
      const eName = sv(line?.Name) || sv(line?.GlobalId) || String(expId);
      catElements[key].push({ name: eName, expressId: expId, globalId: sv(line?.GlobalId) });
    }

    const meta = {
      expressId:   expId,
      ifcType:     tName,
      name:        sv(line?.Name),
      globalId:    sv(line?.GlobalId),
      objectType:  sv(line?.ObjectType),
      description: sv(line?.Description),
      tag:         sv(line?.Tag),
      slot:        cfg.slot,
      modelLabel:  cfg.label,
      slotColor:   cfg.color,
      psets:       getPsets(expId),
      isFlat:      isFlat,
    };

    const mat = new THREE.MeshLambertMaterial({
      color:       tCol.clone(),
      transparent: true,
      opacity:     isFlat ? 1.0 : 0.90,
      wireframe:   isFlat,
      side:        THREE.DoubleSide,
    });

    const pgs = fm.geometries;
    for (let j = 0; j < pgs.size(); j++) {
      const pg  = pgs.get(j);
      const gd  = webIfc.GetGeometry(modelId, pg.geometryExpressID);
      const vs  = webIfc.GetVertexArray(gd.GetVertexData(), gd.GetVertexDataSize());
      const idx = webIfc.GetIndexArray(gd.GetIndexData(), gd.GetIndexDataSize());
      if (!vs || vs.length === 0) { gd.delete(); continue; }

      const S  = 6;
      const pa = new Float32Array(vs.length / S * 3);
      const na = new Float32Array(vs.length / S * 3);
      for (let k = 0; k < vs.length / S; k++) {
        pa[k*3]   = vs[k*S];   pa[k*3+1] = vs[k*S+1]; pa[k*3+2] = vs[k*S+2];
        na[k*3]   = vs[k*S+3]; na[k*3+1] = vs[k*S+4]; na[k*3+2] = vs[k*S+5];
      }

      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(pa, 3));
      geo.setAttribute("normal",   new THREE.BufferAttribute(na, 3));
      geo.setIndex(new THREE.BufferAttribute(idx, 1));

      const mesh = new THREE.Mesh(geo, mat.clone());
      mesh.applyMatrix4(new THREE.Matrix4().fromArray(pg.flatTransformation));
      mesh.userData = Object.assign({}, meta);
      // hide flat/2D types by default
      mesh.visible = !isFlat;

      group.add(mesh);
      modelMeshes[cfg.slot].push(mesh);
      vertCount += pa.length / 3;
      gd.delete();
    }
  }

  webIfc.CloseModel(modelId);

  const statusEl = document.getElementById(`status-m${cfg.slot}`);
  if (statusEl) statusEl.textContent = `✓ ${vertCount.toLocaleString()} Vertices`;
}

// ═══════════════════════════════════════════════════════════════════════════
// Einpassen (Kamera auf alle sichtbaren Meshes ausrichten)
// ═══════════════════════════════════════════════════════════════════════════
function fitAll() {
  const box = new THREE.Box3();
  scene.traverse(o => { if (o.isMesh && o.visible) box.expandByObject(o); });
  if (box.isEmpty()) scene.traverse(o => { if (o.isMesh) box.expandByObject(o); });
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const size   = box.getSize(new THREE.Vector3());
  orb.tgt.copy(center);
  orb.sph.radius = Math.max(size.x, size.y, size.z) * 1.9;
  applyOrb();
  gridHelper.position.y = box.min.y;
}

// HTML-Escape
function esc(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ═══════════════════════════════════════════════════════════════════════════
// Info-Panel  (mit Bearbeitungsmodus)
// ═══════════════════════════════════════════════════════════════════════════

// Merkt sich alle noch nicht gespeicherten In-Memory-Änderungen pro Slot
// Struktur: { slot: { expressId: { Name, ObjectType, …, psets: {…} } } }
const pendingEdits = {};

// Aktuelle Auswahl für den Speichern-Button
let _editSlot      = null;
let _editExpressId = null;

function _inpStyle() {
  return `background:#0e1a30;border:1px solid #1e3a6e;color:#d0dce8;
    padding:3px 6px;border-radius:4px;font-size:11px;width:100%;
    box-sizing:border-box;font-family:inherit;outline:none;`;
}

function showInfo(mesh) {
  const d  = mesh.userData;
  _editSlot      = d.slot;
  _editExpressId = d.expressId;

  _renderInfoPanel(d);
  infoPanel.style.width    = "300px";
  infoPanel.style.minWidth = "300px";
}

function _renderInfoPanel(d) {
  const tc = "#" + getColor(d.ifcType).getHexString();

  // Header
  let h = `
<div style="font-size:11px;font-weight:bold;color:${d.slotColor};margin-bottom:8px;
  display:flex;align-items:center;justify-content:space-between">
  <span>Slot ${d.slot} · ${esc(d.modelLabel)}</span>
  <span style="font-size:10px;color:#4a8;background:#0a2010;border:1px solid #1a5030;
    border-radius:4px;padding:1px 6px" id="edit-dirty-badge" style="display:none">✎ ungespeichert</span>
</div>`;

  // Read-only fields
  h += `
<div style="margin-bottom:10px">
  <div style="display:flex;gap:5px;margin-bottom:3px;align-items:center">
    <span style="color:#667;min-width:86px;flex-shrink:0;font-size:11px">IFC-Typ</span>
    <span style="color:#cce;font-size:11px;display:flex;align-items:center;gap:4px">
      <span style="display:inline-block;width:9px;height:9px;border-radius:50%;
        background:${tc};flex-shrink:0"></span>${esc(d.ifcType || "–")}</span>
  </div>
  <div style="display:flex;gap:5px;margin-bottom:3px;align-items:center">
    <span style="color:#667;min-width:86px;flex-shrink:0;font-size:11px">GlobalId</span>
    <span style="color:#bbd;word-break:break-all;font-size:11px;font-family:monospace">${esc(d.globalId || "–")}</span>
  </div>
  <div style="display:flex;gap:5px;margin-bottom:3px;align-items:center">
    <span style="color:#667;min-width:86px;flex-shrink:0;font-size:11px">Express-ID</span>
    <span style="color:#bbd;font-size:11px">${esc(d.expressId)}</span>
  </div>
</div>`;

  // ── Editable core attributes ─────────────────────────────────────────────
  h += `<div style="border-top:1px solid #0f3460;padding-top:8px;margin-bottom:4px;
    font-size:10px;font-weight:700;color:#6af;text-transform:uppercase;
    letter-spacing:.4px">Bearbeitbare Attribute</div>`;

  const editableAttrs = [
    { key: "name",        label: "Name",         field: "Name",         hint: "" },
    { key: "objectType",  label: "ObjectType",   field: "ObjectType",   hint: "" },
    { key: "tag",         label: "Tag",          field: "Tag",          hint: "" },
    { key: "description", label: "Beschreibung", field: "Description",  hint: "" },
  ];

  for (const a of editableAttrs) {
    const val = d[a.key] || "";
    h += `
<div style="margin-bottom:5px">
  <div style="color:#556;font-size:10px;margin-bottom:2px">${esc(a.label)}</div>
  <input type="text"
    data-edit-field="${esc(a.field)}"
    value="${esc(val)}"
    placeholder="${esc(a.hint || a.label)}"
    style="${_inpStyle()}"
    oninput="markDirty()">
</div>`;
  }

  // ── Property Sets ────────────────────────────────────────────────────────
  const psets = d.psets ?? {};
  h += `
<div style="border-top:1px solid #0f3460;padding-top:8px;margin-top:6px;margin-bottom:4px;
  display:flex;align-items:center;justify-content:space-between">
  <span style="font-size:10px;font-weight:700;color:#6af;text-transform:uppercase;letter-spacing:.4px">
    Property Sets</span>
  <button onclick="addNewPset()" style="font-size:10px;padding:2px 7px;
    background:#0a2a40;border:1px solid #1e4a6e;color:#4fc3f7;
    border-radius:4px;cursor:pointer">+ PSet</button>
</div>
<div id="pset-container">`;

  for (const pn of Object.keys(psets)) {
    h += _renderPset(pn, psets[pn]);
  }
  h += `</div>`;

  // ── Action Buttons ───────────────────────────────────────────────────────
  h += `
<div style="border-top:1px solid #0f3460;padding-top:10px;margin-top:10px;
  display:flex;flex-direction:column;gap:6px">
  <div style="display:flex;gap:5px">
    <button id="btn-save-elem" onclick="saveElementChanges()"
      style="flex:1;padding:7px;background:#0a3a20;border:1px solid #1a6040;
      color:#4caf50;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600">
      💾 Änderungen speichern
    </button>
    <button onclick="discardElementChanges()"
      style="padding:7px 10px;background:#2a0a14;border:1px solid #6e1a2e;
      color:#ff8080;border-radius:6px;cursor:pointer;font-size:11px">
      ✕
    </button>
  </div>
  <div style="display:flex;gap:5px">
    <a href="/viewer/export-ifc/?session_id=${esc(SESSION_ID)}&slot=${esc(String(_editSlot))}&fmt=ifc"
      style="flex:1;padding:6px;background:#0a1a30;border:1px solid #1e3a6e;color:#7ab8e8;
      border-radius:6px;font-size:11px;text-align:center;text-decoration:none">
      ⬇ Export .ifc
    </a>
    <a href="/viewer/export-ifc/?session_id=${esc(SESSION_ID)}&slot=${esc(String(_editSlot))}&fmt=ifczip"
      style="flex:1;padding:6px;background:#0a1a30;border:1px solid #1e3a6e;color:#7ab8e8;
      border-radius:6px;font-size:11px;text-align:center;text-decoration:none">
      ⬇ Export .ifczip
    </a>
  </div>
  <div id="save-status" style="font-size:11px;min-height:18px;text-align:center"></div>
</div>`;

  infoBody.innerHTML = h;
}

function _renderPset(psetName, props) {
  // ifcopenshell fügt ein "id"-Feld (Express-ID des PSets) in props ein –
  // dieses darf nicht als editierbare Eigenschaft angezeigt oder gespeichert werden.
  const filteredProps = {};
  for (const [k, v] of Object.entries(props)) {
    if (k === "id") continue;
    filteredProps[k] = v;
  }

  let propsHtml = "";
  const propEntries = Object.keys(filteredProps);

  if (propEntries.length === 0) {
    // Leeres PSet: zeige direkt eine befüllbare Zeile an
    propsHtml = _emptyPropRow();
  } else {
    for (const k of propEntries) {
      propsHtml += _propRowHtml(k, filteredProps[k]);
    }
  }

  return `
<div class="pset-edit-block" data-pset="${esc(psetName)}"
  style="background:#0c1a30;border:1px solid #1a2e50;border-radius:5px;
  margin-bottom:6px;overflow:hidden">
  <div style="display:flex;align-items:center;justify-content:space-between;
    padding:4px 7px;background:#0a1a2a;cursor:pointer"
    onclick="togglePsetBlock(this)">
    <span style="font-size:11px;font-weight:600;color:#4fc3f7">${esc(psetName)}</span>
    <div style="display:flex;gap:4px;align-items:center">
      <button onclick="event.stopPropagation();addPsetProp(this)"
        title="Eigenschaft hinzufügen"
        style="font-size:10px;padding:1px 7px;background:#0a2040;border:1px solid #1e4a6e;
        color:#4ab;border-radius:3px;cursor:pointer">+ Eigenschaft</button>
      <span class="pset-header-arrow" style="color:#446;font-size:12px">▼</span>
    </div>
  </div>
  <div class="pset-props" style="padding:5px 7px">${propsHtml}</div>
</div>`;
}

function _propRowHtml(k, v) {
  return `
<div class="prop-row" style="display:flex;gap:4px;margin-bottom:4px;align-items:center">
  <input type="text" data-prop-key value="${esc(k)}" placeholder="Eigenschaft"
    style="${_inpStyle()}flex:1;min-width:0" oninput="markDirty()">
  <span style="color:#446;flex-shrink:0">=</span>
  <input type="text" data-prop-val value="${esc(v)}" placeholder="Wert"
    style="${_inpStyle()}flex:1;min-width:0" oninput="markDirty()">
  <button onclick="removePropRow(this)" title="Zeile entfernen"
    style="padding:2px 5px;font-size:10px;background:#2a0a14;border:1px solid #6e1a2e;
    color:#f88;border-radius:3px;cursor:pointer;flex-shrink:0">✕</button>
</div>`;
}

function _emptyPropRow() {
  return _propRowHtml("", "");
}

function togglePsetBlock(header) {
  const propsDiv = header.nextElementSibling;
  const arrow = header.querySelector(".pset-header-arrow");
  const hidden = propsDiv.style.display === "none";
  propsDiv.style.display = hidden ? "" : "none";
  if (arrow) arrow.textContent = hidden ? "▼" : "▶";
}

function markDirty() {
  const badge = document.getElementById("edit-dirty-badge");
  if (badge) badge.style.display = "inline";
}

function addNewPset() {
  const name = prompt("Name des neuen Property Sets:", "Pset_Custom");
  if (!name || !name.trim()) return;
  const container = document.getElementById("pset-container");
  if (!container) return;
  const tmp = document.createElement("div");
  tmp.innerHTML = _renderPset(name.trim(), {});  // {} → startet mit einer leeren Zeile
  const block = tmp.firstElementChild;
  container.appendChild(block);
  // Sofort in das erste Eigenschaftsfeld fokussieren
  const firstKey = block.querySelector("[data-prop-key]");
  if (firstKey) firstKey.focus();
  markDirty();
}

function addPsetProp(btn) {
  const block = btn.closest(".pset-edit-block");
  if (!block) return;
  const propsDiv = block.querySelector(".pset-props");
  if (!propsDiv) return;

  // PSet-Block aufklappen falls zugeklappt
  if (propsDiv.style.display === "none") {
    propsDiv.style.display = "";
    const arrow = block.querySelector(".pset-header-arrow");
    if (arrow) arrow.textContent = "▼";
  }

  const tmp = document.createElement("div");
  tmp.innerHTML = _propRowHtml("", "");
  const row = tmp.firstElementChild;
  if (!row) return;
  propsDiv.appendChild(row);
  const keyInp = row.querySelector("[data-prop-key]");
  if (keyInp) keyInp.focus();
  markDirty();
}

function removePropRow(btn) {
  btn.closest(".prop-row")?.remove();
  markDirty();
}

function discardElementChanges() {
  // Re-render from original mesh userData
  if (!selectedMesh) return;
  _renderInfoPanel(selectedMesh.userData);
}

async function saveElementChanges() {
  if (!selectedMesh) return;
  const btn    = document.getElementById("btn-save-elem");
  const status = document.getElementById("save-status");
  if (btn)    btn.disabled = true;
  if (status) status.innerHTML = '<span style="color:#7ab">Speichern …</span>';

  // ── Collect edits from form ──────────────────────────────────────────────
  const changes = {};

  // Core attributes
  for (const inp of infoBody.querySelectorAll("[data-edit-field]")) {
    changes[inp.dataset.editField] = inp.value;
  }

  // PSet blocks
  const psets = {};
  for (const block of infoBody.querySelectorAll(".pset-edit-block")) {
    const psetName = block.dataset.pset;
    if (!psetName) continue;
    const props = {};
    for (const row of block.querySelectorAll(".prop-row")) {
      const keyInp = row.querySelector("[data-prop-key]");
      const valInp = row.querySelector("[data-prop-val]");
      const k = keyInp?.value?.trim();
      const v = valInp?.value ?? "";
      if (k) props[k] = v;
    }
    psets[psetName] = props;
  }
  changes.psets = psets;

  // ── POST to server ───────────────────────────────────────────────────────
  try {
    const resp = await fetch("/viewer/element/update/", {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        session_id: SESSION_ID,
        slot:       _editSlot,
        express_id: _editExpressId,
        changes,
      }),
    });
    const data = await resp.json();

    if (data.error) {
      if (status) status.innerHTML = `<span style="color:#f88">⚠ ${esc(data.error)}</span>`;
    } else {
      // ── Update in-memory mesh userData ──────────────────────────────────
      const newIfcType = changes.ifcType || selectedMesh.userData.ifcType;
      const oldIfcType = selectedMesh.userData.ifcType;

      for (const m of allMeshes()) {
        if (m.userData.expressId !== _editExpressId || m.userData.slot !== _editSlot) continue;
        m.userData.name        = data.name        ?? changes.Name ?? m.userData.name;
        m.userData.objectType  = data.object_type ?? changes.ObjectType ?? m.userData.objectType;
        m.userData.description = data.description ?? changes.Description ?? m.userData.description;
        m.userData.tag         = data.tag         ?? changes.Tag ?? m.userData.tag;
        m.userData.psets       = data.psets       ?? psets;

        if (data.type_changed && newIfcType !== oldIfcType) {
          // Update type in userData
          m.userData.ifcType = newIfcType;
          // Update color
          const newColor = getColor(newIfcType);
          m.userData._origColor = newColor.clone();
          if (m !== selectedMesh) m.material.color.copy(newColor);
        }
      }

      // ── Update category tree if type changed ─────────────────────────────
      if (data.type_changed && newIfcType !== oldIfcType) {
        const slot = _editSlot;
        const oldKey = catKey(slot, oldIfcType);
        const newKey = catKey(slot, newIfcType);

        // Move element from old category to new
        const elemEntry = (catElements[oldKey] || []).find(e => e.expressId === _editExpressId);
        if (elemEntry) {
          catElements[oldKey] = (catElements[oldKey] || []).filter(e => e.expressId !== _editExpressId);
          catCounts[oldKey]   = Math.max(0, (catCounts[oldKey] || 1) - 1);
          if (catCounts[oldKey] === 0) {
            delete catCounts[oldKey];
            delete catElements[oldKey];
            delete catVisible[oldKey];
          }
          elemEntry.name    = data.name || elemEntry.name;
          elemEntry.ifcType = newIfcType;
          if (!catElements[newKey]) catElements[newKey] = [];
          catElements[newKey].push(elemEntry);
          catCounts[newKey]  = (catCounts[newKey] || 0) + 1;
          if (catVisible[newKey] === undefined) catVisible[newKey] = true;
        }
        buildCategoryUI();
        buildSearchIndex();
      }

      // Update selectedMesh userData after type change
      if (data.type_changed) {
        selectedMesh.userData.ifcType = newIfcType;
      }

      if (status) status.innerHTML = '<span style="color:#4caf50">✓ Gespeichert</span>';
      setTimeout(() => { if (status) status.innerHTML = ""; }, 2500);

      // Re-render info panel with updated data
      _renderInfoPanel(selectedMesh.userData);
    }
  } catch (err) {
    if (status) status.innerHTML = `<span style="color:#f88">⚠ Verbindungsfehler: ${esc(err.message)}</span>`;
  } finally {
    const b2 = document.getElementById("btn-save-elem");
    if (b2) b2.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Selektion via Raycasting
// ═══════════════════════════════════════════════════════════════════════════
const raycaster  = new THREE.Raycaster();
const mouse      = new THREE.Vector2();
const HIGHLIGHT  = new THREE.Color(0xffff00);
let selectedMesh = null;
let mouseMoved   = false;

function selectMesh(m) {
  if (selectedMesh && selectedMesh !== m)
    selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  if (!m.userData._origColor)
    m.userData._origColor = m.material.color.clone();
  m.material.color.copy(HIGHLIGHT);
  selectedMesh = m;
  showInfo(m);
}

function deselectAll() {
  if (selectedMesh) {
    selectedMesh.material.color.copy(selectedMesh.userData._origColor);
    selectedMesh = null;
  }
  infoBody.innerHTML =
    '<div style="color:var(--muted);font-style:italic">Klick auf ein Element für Details.</div>';
}

canvas.addEventListener("mousedown", () => { mouseMoved = false; });
canvas.addEventListener("mousemove", () => { mouseMoved = true; });
canvas.addEventListener("mouseup", e => {
  if (e.button !== 0 || mouseMoved) return;
  const rect = canvas.getBoundingClientRect();
  mouse.x =  ((e.clientX - rect.left) / rect.width)  * 2 - 1;
  mouse.y = -((e.clientY - rect.top)  / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(allMeshes().filter(m => m.visible), false);
  if (hits.length > 0) selectMesh(hits[0].object);
  else deselectAll();
});

// Leertaste → ausblenden
window.addEventListener("keydown", e => {
  if (e.code !== "Space" || !selectedMesh) return;
  e.preventDefault();
  const id = selectedMesh.userData.expressId;
  hiddenIds.add(id);
  for (const m of allMeshes()) if (m.userData.expressId === id) m.visible = false;
  selectedMesh.material.color.copy(selectedMesh.userData._origColor);
  selectedMesh = null;
  infoBody.innerHTML =
    '<div style="color:var(--muted);font-style:italic">Element ausgeblendet.</div>';
  updateHiddenCount();
});

// ── Buttons ─────────────────────────────────────────────────────────────────
const btnFit    = document.getElementById("btn-fit");
const btnReset  = document.getElementById("btn-reset");
const btnShowAll= document.getElementById("btn-show-all");
const infoClose = document.getElementById("info-close");

if (btnFit)    btnFit.addEventListener("click", fitAll);
if (btnReset)  btnReset.addEventListener("click", () => {
  orb.tgt.set(0, 0, 0);
  orb.sph.set(80, Math.PI / 4, Math.PI / 4);
  applyOrb();
});
if (btnShowAll) btnShowAll.addEventListener("click", () => {
  hiddenIds.clear();
  applyVisibility();
});
if (infoClose) infoClose.addEventListener("click", () => {
  infoPanel.style.width    = "0";
  infoPanel.style.minWidth = "0";
});

// ── Render-Loop ──────────────────────────────────────────────────────────────
(function animate() {
  requestAnimationFrame(animate);
  renderer.render(scene, camera);
})();

""" + f"\nconst MODEL_URLS = [{model_urls_js}];\n" + highlight_block + r"""

// ═══════════════════════════════════════════════════════════════════════════
// GlobalId-Suche
// ═══════════════════════════════════════════════════════════════════════════
const searchInput   = document.getElementById("gid-search");
const searchResults = document.getElementById("search-results");
const searchClear   = document.getElementById("search-clear");

// Alle bekannten Elemente (dedupliziert nach globalId) für die Suche
// Wird nach dem Laden aller Modelle befüllt
const searchIndex = [];  // [{globalId, expressId, name, ifcType, slot, modelLabel, slotColor}]

let searchHighlightActive = false;

function buildSearchIndex() {
  searchIndex.length = 0;
  const seen = new Set();
  for (const m of allMeshes()) {
    const gid = m.userData.globalId;
    if (!gid || seen.has(gid)) continue;
    seen.add(gid);
    searchIndex.push({
      globalId:   gid,
      expressId:  m.userData.expressId,
      name:       m.userData.name,
      ifcType:    m.userData.ifcType,
      slot:       m.userData.slot,
      modelLabel: m.userData.modelLabel,
      slotColor:  m.userData.slotColor,
    });
  }
}

function clearSearchHighlight() {
  if (!searchHighlightActive) return;
  for (const m of allMeshes()) {
    m.material.opacity = m.userData.isFlat ? 1.0 : 0.90;
    m.material.emissive && m.material.emissive.set(0x000000);
  }
  applyVisibility();
  searchHighlightActive = false;
}

function applySearchHighlight(matchGids) {
  const gidSet = new Set(matchGids);
  searchHighlightActive = true;
  for (const m of allMeshes()) {
    if (gidSet.has(m.userData.globalId)) {
      m.material.opacity = 0.97;
      m.visible = true;
    } else {
      m.material.opacity = 0.05;
    }
  }
}

function flyToGids(matchGids) {
  const gidSet = new Set(matchGids);
  const box = new THREE.Box3();
  for (const m of allMeshes()) {
    if (gidSet.has(m.userData.globalId)) box.expandByObject(m);
  }
  if (!box.isEmpty()) {
    const center = box.getCenter(new THREE.Vector3());
    const size   = box.getSize(new THREE.Vector3());
    orb.tgt.copy(center);
    orb.sph.radius = Math.max(size.x, size.y, size.z) * 3.5;
    applyOrb();
  }
}

function renderSearchResults(query) {
  const q = query.trim().toLowerCase();
  if (q.length === 0) {
    searchResults.style.display = "none";
    searchClear.style.display   = "none";
    clearSearchHighlight();
    return;
  }
  searchClear.style.display = "inline";

  const hits = searchIndex.filter(el =>
    el.globalId.toLowerCase().includes(q)
  );

  searchResults.innerHTML = "";

  if (hits.length === 0) {
    searchResults.style.display = "block";
    searchResults.innerHTML =
      '<div style="padding:8px 12px;font-size:11px;color:var(--muted);font-style:italic">Keine Treffer</div>';
    clearSearchHighlight();
    return;
  }

  // Header
  const header = document.createElement("div");
  header.style.cssText = "padding:5px 12px;font-size:10px;color:#6af;border-bottom:1px solid #1a2e50;" +
    "display:flex;align-items:center;justify-content:space-between";
  header.innerHTML =
    `<span>${hits.length} Element${hits.length !== 1 ? "e" : ""} gefunden</span>` +
    `<span style="color:var(--muted)">${hits.length > 50 ? "ersten 50 angezeigt" : ""}</span>`;
  searchResults.appendChild(header);

  const visible50 = hits.slice(0, 50);
  const matchGids = hits.map(h => h.globalId);

  // Highlight immediately
  applySearchHighlight(matchGids);
  if (hits.length <= 5) flyToGids(matchGids);

  for (const el of visible50) {
    const col  = el.slotColor || "#4fc3f7";
    const tCol = "#" + getColor(el.ifcType).getHexString();
    const row  = document.createElement("div");
    row.style.cssText = "padding:5px 10px;cursor:pointer;border-bottom:1px solid #0e1a2e;" +
      "display:flex;flex-direction:column;gap:1px";
    row.innerHTML =
      `<div style="display:flex;align-items:center;gap:5px">` +
        `<span style="width:7px;height:7px;border-radius:50%;background:${tCol};flex-shrink:0"></span>` +
        `<span style="font-size:11px;color:#cce;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">` +
          `${esc(el.name || el.ifcType)}</span>` +
        `<span style="font-size:10px;color:${col};flex-shrink:0">${esc(el.ifcType)}</span>` +
      `</div>` +
      `<div style="font-size:10px;color:#4a7090;font-family:monospace;padding-left:12px">` +
        highlightMatch(el.globalId, q) +
      `</div>`;
    row.addEventListener("mouseenter", () => row.style.background = "#162a48");
    row.addEventListener("mouseleave", () => row.style.background = "");
    row.addEventListener("click", () => {
      // Einzelnes Element fokussieren und selektieren
      const mesh = allMeshes().find(m => m.userData.globalId === el.globalId);
      if (mesh) {
        selectMesh(mesh);
        flyToGids([el.globalId]);
      }
    });
    searchResults.appendChild(row);
  }

  searchResults.style.display = "block";
}

function highlightMatch(text, q) {
  const idx = text.toLowerCase().indexOf(q);
  if (idx < 0) return esc(text);
  return (
    esc(text.slice(0, idx)) +
    `<span style="background:#1a4a6e;color:#7df;border-radius:2px;padding:0 1px">` +
    esc(text.slice(idx, idx + q.length)) +
    `</span>` +
    esc(text.slice(idx + q.length))
  );
}

if (searchInput) {
  searchInput.addEventListener("input", e => renderSearchResults(e.target.value));
  searchInput.addEventListener("keydown", e => {
    if (e.key === "Escape") {
      searchInput.value = "";
      renderSearchResults("");
    }
  });
  // Klick außerhalb schließt Dropdown
  document.addEventListener("mousedown", e => {
    const bar = document.getElementById("search-bar");
    if (bar && !bar.contains(e.target)) {
      searchResults.style.display = "none";
    }
  });
  searchInput.addEventListener("focus", () => {
    if (searchInput.value.trim()) searchResults.style.display = "block";
  });
}
if (searchClear) {
  searchClear.addEventListener("click", () => {
    searchInput.value = "";
    renderSearchResults("");
    searchInput.focus();
  });
}

// ═══════════════════════════════════════════════════════════════════════════
// Bootstrap
// ═══════════════════════════════════════════════════════════════════════════
(async () => {
  if (MODEL_URLS.length === 0) {
    loadingEl.style.display = "none";
    return;
  }
  try {
    await initWebIfc();
    for (const cfg of MODEL_URLS) {
      try {
        await loadModel(cfg);
      } catch (err) {
        console.error("Ladefehler:", cfg.label, err);
        if (loadTxtEl) loadTxtEl.textContent = `⚠ Fehler: ${err.message}`;
      }
    }
    buildCategoryUI();
    buildSearchIndex();
    fitAll();
    applyClashHighlight();
  } catch (err) {
    if (loadTxtEl) loadTxtEl.textContent = "Initialisierungsfehler: " + err.message;
    console.error(err);
  } finally {
    loadingEl.style.display = "none";
  }
})();
"""


# ─────────────────────────────────────────────────────────────────────────────
# Clash-Analyse-Seite
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/", response_class=HTMLResponse)
def viewer_clash(
    session_id: str  = Query(...),
    tolerance: float = Query(default=0.0),
    project_id: str  = Query(default=""),
):
    """
    Clash-Analyse mit frei definierbaren Vergleichsgruppen.

    Jede Gruppe besteht aus:
      - Auswahl beliebiger Slots (Checkboxen)
      - Beliebig viele Filterregeln (field / operator / value, AND-Logik)

    Die Berechnung erfolgt asynchron via POST /viewer/clash/run/ (JSON-API).
    """
    if not session_exists(session_id):
        return _page("Fehler",
            '<div style="padding:40px;color:var(--accent2)">'
            '<h2>Session nicht gefunden</h2><a href="/">← Start</a></div>')

    sid   = _e(session_id)
    slots = get_session_slots(session_id)
    labels = {s: get_ifc_label(session_id, s) for s in slots}

    if len(slots) < 1:
        body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">
  {_topbar(session_id, "clash", clash_params="", project_id=project_id)}
  <div style="flex:1;overflow-y:auto;padding:16px">
    <h2 style="font-size:17px;margin-bottom:14px">⚡ Clash-Analyse</h2>
    <div class="flash-err">⚠ Bitte zuerst mindestens 1 IFC-Modell im
      <a href="{_viewer_url(session_id, "viewer", project_id)}">Model</a> hochladen.</div>
  </div>
</div>"""
        return _page("Clash-Analyse – BIMPruef", body)

    # Slot-Checkboxen für Gruppe A und B vorbelegen
    default_a = slots[0] if slots else 0
    default_b = slots[1] if len(slots) > 1 else (slots[0] if slots else 0)

    def _slot_checkboxes(group: str, default_slot: int) -> str:
        boxes = []
        for s in slots:
            checked = "checked" if s == default_slot else ""
            lbl = _e(labels[s])
            boxes.append(
                f'<label style="display:flex;align-items:center;gap:6px;cursor:pointer;'
                f'font-size:12px;padding:3px 0">'
                f'<input type="checkbox" class="slot-chk-{group}" value="{s}" {checked} '
                f'style="accent-color:var(--accent);width:13px;height:13px">'
                f'<span style="color:var(--text)">{lbl}</span></label>'
            )
        return "\n".join(boxes)

    slots_a_html = _slot_checkboxes("a", default_a)
    slots_b_html = _slot_checkboxes("b", default_b)

    # Felder für Filter-Dropdowns
    filter_fields = [
        ("type",            "IFC-Typ"),
        ("name",            "Name"),
        ("file_label",      "Dateiname"),
        ("global_id",       "GlobalId"),
        ("object_type",     "ObjectType"),
        ("predefined_type", "PredefinedType"),
    ]
    field_options = "".join(
        f'<option value="{v}">{_e(lbl)}</option>'
        for v, lbl in filter_fields
    )
    operator_options = (
        '<option value="contains">enthält</option>'
        '<option value="not_contains">enthält nicht</option>'
        '<option value="equals">ist gleich</option>'
        '<option value="not_equals">ist ungleich</option>'
        '<option value="starts_with">beginnt mit</option>'
        '<option value="ends_with">endet mit</option>'
    )

    body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">
  {_topbar(session_id, "clash", clash_params="", project_id=project_id)}
  <div style="flex:1;overflow-y:auto;padding:16px">
    <h2 style="font-size:17px;margin-bottom:14px">⚡ Clash-Analyse – Gruppenvergleich</h2>

    <!-- ── Gruppen-Konfiguration ────────────────────────────────────────── -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">

      <!-- Gruppe A -->
      <div class="card" id="card-group-a">
        <div style="font-size:13px;font-weight:700;color:var(--accent);margin-bottom:10px">
          Gruppe A
        </div>

        <div style="font-size:11px;color:var(--muted);margin-bottom:4px;
          text-transform:uppercase;letter-spacing:.4px">Modelle</div>
        <div id="slots-a" style="margin-bottom:10px">
          {slots_a_html}
        </div>

        <div style="font-size:11px;color:var(--muted);margin-bottom:6px;
          text-transform:uppercase;letter-spacing:.4px">Filterregeln</div>
        <div id="filters-a" style="display:flex;flex-direction:column;gap:6px"></div>
        <div id="hint-a" class="flash-ok"
          style="font-size:11px;margin-top:6px;padding:5px 8px">
          Kein Filter aktiv – alle Elemente aus den ausgewählten Modellen werden verwendet.
        </div>
        <button onclick="addFilter('a')" class="btn"
          style="margin-top:8px;font-size:11px;padding:4px 10px">+ Filterregel</button>
      </div>

      <!-- Gruppe B -->
      <div class="card" id="card-group-b">
        <div style="font-size:13px;font-weight:700;color:var(--accent2);margin-bottom:10px">
          Gruppe B
        </div>

        <div style="font-size:11px;color:var(--muted);margin-bottom:4px;
          text-transform:uppercase;letter-spacing:.4px">Modelle</div>
        <div id="slots-b" style="margin-bottom:10px">
          {slots_b_html}
        </div>

        <div style="font-size:11px;color:var(--muted);margin-bottom:6px;
          text-transform:uppercase;letter-spacing:.4px">Filterregeln</div>
        <div id="filters-b" style="display:flex;flex-direction:column;gap:6px"></div>
        <div id="hint-b" class="flash-ok"
          style="font-size:11px;margin-top:6px;padding:5px 8px">
          Kein Filter aktiv – alle Elemente aus den ausgewählten Modellen werden verwendet.
        </div>
        <button onclick="addFilter('b')" class="btn"
          style="margin-top:8px;font-size:11px;padding:4px 10px">+ Filterregel</button>
      </div>
    </div>

    <!-- ── Toleranz + Start ──────────────────────────────────────────────── -->
    <div class="card" style="display:flex;align-items:flex-end;gap:14px;flex-wrap:wrap;
      margin-bottom:14px">
      <div>
        <label style="display:block;font-size:11px;color:var(--muted);margin-bottom:4px">
          Toleranz (m)
        </label>
        <input id="inp-tolerance" type="number" value="{tolerance}" step="0.01" min="0"
          style="background:var(--surface2);border:1px solid var(--border);
          color:var(--text);padding:6px 10px;border-radius:6px;font-size:13px;width:100px">
      </div>
      <button id="btn-run" class="btn btn-primary" style="font-size:13px"
        onclick="runClash()">⚡ Analyse starten</button>
      <span id="run-status" style="font-size:12px;color:var(--muted)"></span>
    </div>

    <!-- ── Ergebnis ──────────────────────────────────────────────────────── -->
    <div id="clash-result"></div>
  </div>
</div>

<script>
(function() {{
  const SESSION_ID       = {json.dumps(session_id)};
  const PROJECT_ID       = {json.dumps(project_id)};
  const BASE_FIELD_OPTIONS = {json.dumps(field_options)};
  const OP_OPTIONS       = {json.dumps(operator_options)};
  const filterRows       = {{a: [], b: []}};
  // Pset-Schlüssel pro Gruppe (werden asynchron geladen)
  const psetKeys         = {{a: [], b: []}};

  // ── Zustand-Persistenz (sessionStorage) ─────────────────────────────────
  const STATE_KEY = "clash_state_" + SESSION_ID;

  function saveState(clashData, tolerance, slots_a, slots_b, filters_a, filters_b) {{
    try {{
      sessionStorage.setItem(STATE_KEY, JSON.stringify({{
        clashData, tolerance, slots_a, slots_b, filters_a, filters_b,
      }}));
    }} catch(e) {{}}
  }}

  function loadSavedState() {{
    try {{
      const raw = sessionStorage.getItem(STATE_KEY);
      return raw ? JSON.parse(raw) : null;
    }} catch(e) {{ return null; }}
  }}

  function esc(s) {{
    return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
  }}

  // ── Pset-Schlüssel laden ─────────────────────────────────────────────────
  async function loadPsetKeys(group) {{
    const slots = collectSlots(group);
    if (!slots.length) return;
    try {{
      const resp = await fetch(
        `/viewer/clash/pset-keys/?session_id=${{encodeURIComponent(SESSION_ID)}}&slots=${{slots.join(",")}}`
      );
      const data = await resp.json();
      if (data.pset_keys) {{
        psetKeys[group] = data.pset_keys;
        // Bestehende Filterzeilen dieser Gruppe aktualisieren
        filterRows[group].forEach(rowId => {{
          const row = document.getElementById(rowId);
          if (!row) return;
          const sel = row.querySelector(".filter-field");
          const cur = sel ? sel.value : "";
          sel.innerHTML = buildFieldOptions(group);
          sel.value = cur;  // bisherige Auswahl beibehalten
        }});
      }}
    }} catch(e) {{
      // Pset-Laden fehlgeschlagen – kein fataler Fehler
    }}
  }}

  function buildFieldOptions(group) {{
    let html = BASE_FIELD_OPTIONS;
    if (psetKeys[group] && psetKeys[group].length) {{
      html += '<option disabled style="color:var(--muted)">── Eigenschaften (Psets) ──</option>';
      psetKeys[group].forEach(k => {{
        const lbl = k.startsWith("pset:") ? k.slice(5) : k;
        html += `<option value="${{esc(k)}}">${{esc(lbl)}}</option>`;
      }});
    }}
    return html;
  }}

  // Slot-Änderungen → Pset-Schlüssel neu laden
  ["a","b"].forEach(group => {{
    document.querySelectorAll(".slot-chk-" + group).forEach(chk => {{
      chk.addEventListener("change", () => loadPsetKeys(group));
    }});
  }});

  // Initial laden
  loadPsetKeys("a");
  loadPsetKeys("b");

  // ── Filter-Zeilen verwalten ──────────────────────────────────────────────
  window.addFilter = function(group) {{
    const container = document.getElementById("filters-" + group);
    const rowId     = "fr-" + group + "-" + Date.now();
    const div       = document.createElement("div");
    div.id          = rowId;
    div.style.cssText = "display:flex;gap:4px;align-items:center";
    div.innerHTML = `
      <select class="filter-field" style="background:var(--surface2);border:1px solid var(--border);
        color:var(--text);padding:4px 6px;border-radius:5px;font-size:11px;flex:1.2">
        ${{buildFieldOptions(group)}}
      </select>
      <select class="filter-op" style="background:var(--surface2);border:1px solid var(--border);
        color:var(--text);padding:4px 6px;border-radius:5px;font-size:11px;flex:1.4">
        ${{OP_OPTIONS}}
      </select>
      <input type="text" class="filter-val" placeholder="Wert …"
        style="background:var(--surface2);border:1px solid var(--border);
        color:var(--text);padding:4px 7px;border-radius:5px;font-size:11px;flex:2;min-width:0"
        onkeydown="if(event.key==='Enter') runClash()">
      <button onclick="removeFilter('${{rowId}}','${{group}}')"
        style="background:none;border:none;color:var(--accent2);cursor:pointer;font-size:14px;
        padding:0 4px" title="Entfernen">✕</button>
    `;
    container.appendChild(div);
    filterRows[group].push(rowId);
    updateHint(group);
  }};

  window.removeFilter = function(rowId, group) {{
    const el = document.getElementById(rowId);
    if (el) el.remove();
    filterRows[group] = filterRows[group].filter(id => id !== rowId);
    updateHint(group);
  }};

  function updateHint(group) {{
    const hint    = document.getElementById("hint-" + group);
    const hasRows = filterRows[group].length > 0;
    hint.style.display = hasRows ? "none" : "block";
  }}

  function collectFilters(group) {{
    const filters = [];
    filterRows[group].forEach(rowId => {{
      const row = document.getElementById(rowId);
      if (!row) return;
      const field = row.querySelector(".filter-field").value;
      const op    = row.querySelector(".filter-op").value;
      const val   = row.querySelector(".filter-val").value.trim();
      if (field && val) filters.push({{field, operator: op, value: val}});
    }});
    return filters;
  }}

  function collectSlots(group) {{
    return [...document.querySelectorAll(".slot-chk-" + group + ":checked")]
      .map(c => parseInt(c.value, 10));
  }}

  // ── Clash-Analyse starten ────────────────────────────────────────────────
  window.runClash = async function() {{
    const btnRun  = document.getElementById("btn-run");
    const status  = document.getElementById("run-status");
    const result  = document.getElementById("clash-result");

    const slots_a   = collectSlots("a");
    const slots_b   = collectSlots("b");
    const filters_a = collectFilters("a");
    const filters_b = collectFilters("b");
    const tolerance = parseFloat(document.getElementById("inp-tolerance").value) || 0;

    if (!slots_a.length) {{
      status.innerHTML = '<span style="color:var(--accent2)">⚠ Bitte mindestens ein Modell für Gruppe A wählen.</span>';
      return;
    }}
    if (!slots_b.length) {{
      status.innerHTML = '<span style="color:var(--accent2)">⚠ Bitte mindestens ein Modell für Gruppe B wählen.</span>';
      return;
    }}

    btnRun.disabled       = true;
    btnRun.textContent    = "⏳ Berechne …";
    status.textContent    = "";
    result.innerHTML      = "";

    try {{
      const resp = await fetch("/viewer/clash/run/", {{
        method:  "POST",
        headers: {{"Content-Type": "application/json"}},
        body:    JSON.stringify({{
          session_id: SESSION_ID, tolerance,
          group_a: {{selected_slots: slots_a, filters: filters_a}},
          group_b: {{selected_slots: slots_b, filters: filters_b}},
        }}),
      }});
      const data = await resp.json();
      if (data.error) throw new Error(data.error);
      renderResult(data, tolerance, slots_a, slots_b);
      saveState(data, tolerance, slots_a, slots_b, filters_a, filters_b);
    }} catch(e) {{
      result.innerHTML = `<div class="flash-err">⚠ Fehler: ${{esc(e.message)}}</div>`;
    }} finally {{
      btnRun.disabled    = false;
      btnRun.textContent = "⚡ Analyse starten";
    }}
  }};

  function renderResult(data, tolerance, slots_a, slots_b) {{
    const result  = document.getElementById("clash-result");
    const clashes = data.clashes || [];

    if (!clashes.length) {{
      result.innerHTML = '<div class="flash-ok">✓ Keine Clashes zwischen Gruppe A und Gruppe B gefunden.</div>';
      return;
    }}

    let rows = "";
    clashes.forEach((c, idx) => {{
      const gid1  = esc(c.global_id_1 || "");
      const gid2  = esc(c.global_id_2 || "");
      const sa    = c.slot_1 || slots_a[0];
      const sb    = c.slot_2 || slots_b[0];
      const detailUrl = `/viewer/clash/detail/?session_id=${{esc(SESSION_ID)}}&slot_a=${{sa}}&slot_b=${{sb}}&gid1=${{encodeURIComponent(gid1)}}&gid2=${{encodeURIComponent(gid2)}}${{PROJECT_ID ? "&project_id=" + encodeURIComponent(PROJECT_ID) : ""}}`;
      const bcfUrl    = `/viewer/clash/bcf-single/?session_id=${{esc(SESSION_ID)}}&slot_a=${{sa}}&slot_b=${{sb}}&clash_index=${{idx}}&tolerance=${{tolerance}}`;
      const lbl1  = c.file_label_1 ? ` <span style="color:var(--muted);font-size:10px">(${{esc(c.file_label_1)}})</span>` : "";
      const lbl2  = c.file_label_2 ? ` <span style="color:var(--muted);font-size:10px">(${{esc(c.file_label_2)}})</span>` : "";
      rows += `<tr>
<td>${{idx+1}}</td>
<td><span class="tag tag-1">${{esc(c.type_1||"")}}</span> ${{esc(c.name_1||"")}}${{lbl1}}</td>
<td style="font-family:monospace;font-size:11px">${{gid1}}</td>
<td><span class="tag tag-2">${{esc(c.type_2||"")}}</span> ${{esc(c.name_2||"")}}${{lbl2}}</td>
<td style="font-family:monospace;font-size:11px">${{gid2}}</td>
<td>
  <a href="${{detailUrl}}" class="btn"
    style="font-size:11px;padding:3px 8px;margin-right:4px;text-decoration:none"
    title="Im Viewer anzeigen">👁 Viewer</a>
  <a href="${{bcfUrl}}" class="btn"
    style="font-size:11px;padding:3px 8px;text-decoration:none"
    title="BCF für diesen Clash">BCF ↓</a>
</td>
</tr>`;
    }});

    // Build BCF-all URL from first clash for slot info
    const _bcfSa = clashes[0].slot_1 || slots_a[0];
    const _bcfSb = clashes[0].slot_2 || slots_b[0];
    const bcfAllUrl = `/viewer/clash/bcf/?session_id=${{esc(SESSION_ID)}}&slot_a=${{_bcfSa}}&slot_b=${{_bcfSb}}&tolerance=${{tolerance}}`;

    result.innerHTML = `
<div class="card">
  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;flex-wrap:wrap;gap:8px">
    <h3 style="font-size:15px">${{clashes.length}} Clash${{clashes.length !== 1 ? "es" : ""}} gefunden</h3>
    <a href="${{bcfAllUrl}}" class="btn btn-primary"
      style="font-size:12px;padding:5px 12px;text-decoration:none"
      title="Alle Clashes als eine BCF-Datei herunterladen">⬇ Alle als BCF</a>
  </div>
  <div style="overflow-x:auto;max-height:55vh;overflow-y:auto">
    <table>
      <tr>
        <th style="width:40px">#</th>
        <th>Element A</th><th>GlobalId A</th>
        <th>Element B</th><th>GlobalId B</th>
        <th style="width:160px">Aktion</th>
      </tr>
      ${{rows}}
    </table>
  </div>
</div>`;
  }}

  // ── Zustand wiederherstellen (nach Rückkehr von Clash-Detail) ────────
  (function restoreState() {{
    const state = loadSavedState();
    if (!state || !state.clashData) return;

    // Slots in Checkboxen wiederherstellen
    function restoreSlots(group, saved_slots) {{
      document.querySelectorAll(".slot-chk-" + group).forEach(chk => {{
        chk.checked = saved_slots.includes(parseInt(chk.value, 10));
      }});
    }}
    if (state.slots_a) restoreSlots("a", state.slots_a);
    if (state.slots_b) restoreSlots("b", state.slots_b);

    // Toleranz wiederherstellen
    const tolInp = document.getElementById("inp-tolerance");
    if (tolInp && state.tolerance !== undefined) tolInp.value = state.tolerance;

    // Filter wiederherstellen
    function restoreFilters(group, saved_filters) {{
      if (!saved_filters || !saved_filters.length) return;
      saved_filters.forEach(f => {{
        window.addFilter(group);
        const rows = filterRows[group];
        const rowId = rows[rows.length - 1];
        const row = document.getElementById(rowId);
        if (!row) return;
        const fieldSel = row.querySelector(".filter-field");
        const opSel    = row.querySelector(".filter-op");
        const valInp   = row.querySelector(".filter-val");
        if (fieldSel) fieldSel.value = f.field || "";
        if (opSel)    opSel.value    = f.operator || "contains";
        if (valInp)   valInp.value   = f.value || "";
      }});
    }}
    if (state.filters_a) restoreFilters("a", state.filters_a);
    if (state.filters_b) restoreFilters("b", state.filters_b);

    // Ergebnis sofort rendern
    renderResult(state.clashData, state.tolerance, state.slots_a || [], state.slots_b || []);
  }})();
}})();
</script>"""

    return _page("Clash-Analyse – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# Pset-Schlüssel für Clash-Filter (GET) – liefert alle Pset-Keys aus Slots
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/pset-keys/")
def viewer_clash_pset_keys(
    session_id: str = Query(...),
    slots: str      = Query(default=""),   # kommagetrennte Slot-Nummern
):
    """
    Gibt alle Pset-Schlüssel ('pset:PsetName.PropName') zurück,
    die in den angegebenen Slots vorkommen.
    """
    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

    try:
        selected_slots = [int(s) for s in slots.split(",") if s.strip()]
    except ValueError:
        selected_slots = get_session_slots(session_id)

    if not selected_slots:
        selected_slots = get_session_slots(session_id)

    # Pset-Schlüssel aus allen gewählten Slots sammeln
    pset_keys: set = set()
    import ifcopenshell
    from app.storage import get_ifc_path
    from app.extractors import get_candidate_products, get_psets_safe

    for slot in selected_slots:
        path = get_ifc_path(session_id, slot)
        if not os.path.exists(path):
            continue
        try:
            model = ifcopenshell.open(path)
            for elem in get_candidate_products(model):
                psets = get_psets_safe(elem)
                for pset_name, props in (psets or {}).items():
                    if isinstance(props, dict):
                        for prop_name in props:
                            pset_keys.add(f"pset:{pset_name}.{prop_name}")
        except Exception:
            continue

    return JSONResponse({"pset_keys": sorted(pset_keys)})


# ─────────────────────────────────────────────────────────────────────────────
# Clash-Analyse JSON-API (POST) – Gruppen-basiert
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/viewer/clash/run/")
async def viewer_clash_run(request: Request):
    """
    Führt eine gruppen-basierte Clash-Analyse durch.

    Erwartet JSON-Body:
    {
      "session_id": "...",
      "tolerance": 0.0,
      "group_a": {
        "selected_slots": [1, 2],
        "filters": [{"field": "type", "operator": "equals", "value": "IfcWall"}]
      },
      "group_b": {
        "selected_slots": [2, 3],
        "filters": []
      }
    }

    Gibt JSON zurück:
    {
      "count_a": <int>,
      "count_b": <int>,
      "clashes": [...]
    }
    """
    try:
        body       = await request.json()
        session_id = body.get("session_id", "")
        tolerance  = float(body.get("tolerance", 0.0))
        group_a    = body.get("group_a", {})
        group_b    = body.get("group_b", {})

        if not session_exists(session_id):
            return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)

        slots_a   = [int(s) for s in group_a.get("selected_slots", [])]
        filters_a = group_a.get("filters", [])
        slots_b   = [int(s) for s in group_b.get("selected_slots", [])]
        filters_b = group_b.get("filters", [])

        if not slots_a:
            return JSONResponse({"error": "Gruppe A: kein Slot ausgewählt."}, status_code=400)
        if not slots_b:
            return JSONResponse({"error": "Gruppe B: kein Slot ausgewählt."}, status_code=400)

        from app.clash import get_group_elements, compare_element_groups_for_clashes

        elements_a = get_group_elements(session_id, slots_a, filters_a)
        elements_b = get_group_elements(session_id, slots_b, filters_b)

        clashes = compare_element_groups_for_clashes(elements_a, elements_b, tolerance=tolerance)

        return JSONResponse({
            "count_a": len(elements_a),
            "count_b": len(elements_b),
            "clashes": clashes,
        })

    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# Clash-Detail-Viewer (nur 2 Elemente hervorgehoben)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/detail/", response_class=HTMLResponse)
def viewer_clash_detail(
    session_id: str = Query(...),
    slot_a: int     = Query(...),
    slot_b: int     = Query(...),
    gid1: str       = Query(...),
    gid2: str       = Query(...),
    project_id: str = Query(default=""),
):
    if not session_exists(session_id):
        return _page("Fehler",
            '<div style="padding:40px;color:var(--accent2)"><h2>Session nicht gefunden</h2></div>')

    sid     = _e(session_id)
    label_a = _e(get_ifc_label(session_id, slot_a))
    label_b = _e(get_ifc_label(session_id, slot_b))
    col_a   = _slot_color(slot_a)
    col_b   = _slot_color(slot_b)

    if slot_a == slot_b:
        # Same model for both groups: load only once to avoid double-load / slot reset bug
        model_urls_js = (
            f'{{url:"/viewer/file/?session_id={sid}&slot={slot_a}",'
            f'label:{repr(label_a)},slot:{slot_a},color:{repr(col_a)}}}'
        )
    else:
        model_urls_js = (
            f'{{url:"/viewer/file/?session_id={sid}&slot={slot_a}",'
            f'label:{repr(label_a)},slot:{slot_a},color:{repr(col_a)}}},'
            f'{{url:"/viewer/file/?session_id={sid}&slot={slot_b}",'
            f'label:{repr(label_b)},slot:{slot_b},color:{repr(col_b)}}}'
        )

    back_url = _viewer_url(session_id, "clash", project_id, f"slot_a={slot_a}&slot_b={slot_b}&run=1")

    body = f"""
<div style="display:flex;flex-direction:column;height:100vh;overflow:hidden">

  <div style="display:flex;align-items:center;gap:6px;padding:6px 14px;
    background:var(--surface);border-bottom:1px solid var(--border);flex-shrink:0">
    {_brand_logo(24)}
    <a href="{back_url}" class="btn"
      style="font-size:12px;text-decoration:none;margin-left:6px">← Clash-Liste</a>
    <span style="margin-left:10px;font-size:12px;color:var(--muted)">
      Nur die kollidierenden Elemente sind hervorgehoben
    </span>
    <div style="margin-left:auto;display:flex;gap:4px">
      <button id="btn-fit"   class="btn" style="font-size:11px;padding:4px 9px">⊡ Einpassen</button>
      <button id="btn-reset" class="btn" style="font-size:11px;padding:4px 9px">⟳ Kamera</button>
    </div>
  </div>

  <div style="display:flex;flex:1;overflow:hidden">

    <!-- Sidebar -->
    <div style="width:220px;min-width:220px;background:var(--surface);
      border-right:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px">Modelle</div>

      <div class="model-card">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">
          <input type="checkbox" class="chk-model" data-slot="{slot_a}" checked
            style="accent-color:{col_a};width:13px;height:13px">
          <span style="width:10px;height:10px;border-radius:50%;
            border:2px solid {col_a};flex-shrink:0;display:inline-block"></span>
          {label_a}
        </label>
      </div>
      {"" if slot_a == slot_b else f'''<div class="model-card">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:12px">
          <input type="checkbox" class="chk-model" data-slot="{slot_b}" checked
            style="accent-color:{col_b};width:13px;height:13px">
          <span style="width:10px;height:10px;border-radius:50%;
            border:2px solid {col_b};flex-shrink:0;display:inline-block"></span>
          {label_b}
        </label>
      </div>'''}

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;margin-top:4px">
        Clash-Elemente
      </div>
      <div style="padding:8px 10px;font-size:11px">
        <div style="margin-bottom:8px">
          <span class="tag tag-1">A</span>
          <span style="font-size:10px;color:var(--muted);margin-left:4px;
            font-family:monospace;word-break:break-all">{_e(gid1)}</span>
        </div>
        <div>
          <span class="tag tag-2">B</span>
          <span style="font-size:10px;color:var(--muted);margin-left:4px;
            font-family:monospace;word-break:break-all">{_e(gid2)}</span>
        </div>
        <div style="margin-top:10px;font-size:10px;color:var(--muted)">
          Alle anderen Elemente werden transparent dargestellt.
        </div>
      </div>

      <div style="padding:5px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;margin-top:4px">Kategorien</div>
      <div id="cat-scroll" style="flex:1;overflow-y:auto;padding:2px 0">
        <div style="padding:8px 10px;font-size:11px;color:var(--muted);font-style:italic">
          Wird geladen …
        </div>
      </div>
    </div>

    <!-- Canvas -->
    <div id="canvas-wrap" style="flex:1;position:relative;overflow:hidden">
      <canvas id="three-canvas"
        style="width:100%!important;height:100%!important;display:block"></canvas>
      <div id="loading" style="position:absolute;inset:0;display:flex;flex-direction:column;
        align-items:center;justify-content:center;background:rgba(14,14,26,.93);z-index:20">
        <div style="width:40px;height:40px;border:4px solid #0f3460;
          border-top-color:var(--accent2);border-radius:50%;
          animation:spin .7s linear infinite;margin-bottom:12px"></div>
        <p id="load-txt" style="color:#889;font-size:13px">Clash-Elemente werden geladen …</p>
      </div>
    </div>

    <!-- Info-Panel -->
    <div id="info-panel" style="width:300px;min-width:300px;background:var(--surface);
      border-left:1px solid var(--border);display:flex;flex-direction:column;
      overflow:hidden;flex-shrink:0">
      <div style="padding:6px 10px;font-size:10px;font-weight:700;background:#0f2040;
        color:#8ab;text-transform:uppercase;letter-spacing:.7px;
        display:flex;align-items:center;justify-content:space-between;flex-shrink:0">
        <span>Element-Info</span>
        <span id="info-close" style="cursor:pointer;color:var(--muted);font-size:14px">✕</span>
      </div>
      <div id="info-body" style="flex:1;overflow-y:auto;padding:10px;font-size:12px">
        <div style="color:var(--muted);font-style:italic">Klick auf ein Element für Details.</div>
      </div>
    </div>
  </div>
</div>

<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<script>
{_viewer_js(model_urls_js, highlight_gids=[gid1, gid2], session_id=sid)}
</script>"""

    return _page("Clash-Detail – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# BCF – alle Clashes
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/bcf/")
def viewer_clash_bcf(
    session_id: str  = Query(...),
    slot_a: int      = Query(...),
    slot_b: int      = Query(...),
    tolerance: float = Query(default=0.0),
):
    try:
        clashes = load_clash_cache(session_id, tolerance, slot_a=slot_a, slot_b=slot_b)
        if clashes is None:
            from app.ifc_loader import load_ifc_models_by_slots
            from app.clash import compare_models_for_clashes
            loaded  = load_ifc_models_by_slots(session_id, slot_a, slot_b)
            clashes = compare_models_for_clashes(
                loaded["model_1"], loaded["model_2"], tolerance=tolerance
            )
            save_clash_cache(session_id, tolerance, clashes, slot_a=slot_a, slot_b=slot_b)

        from app.bcf_export import create_bcf_zip_from_clashes
        data = create_bcf_zip_from_clashes(clashes[:BCF_CLASH_LIMIT])
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": 'attachment; filename="ifc_clashes.bcfzip"'},
        )
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)


# ─────────────────────────────────────────────────────────────────────────────
# BCF – einzelner Clash
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/viewer/clash/bcf-single/")
def viewer_clash_bcf_single(
    session_id: str  = Query(...),
    slot_a: int      = Query(...),
    slot_b: int      = Query(...),
    clash_index: int = Query(...),
    tolerance: float = Query(default=0.0),
):
    try:
        clashes = load_clash_cache(session_id, tolerance, slot_a=slot_a, slot_b=slot_b)
        if clashes is None:
            from app.ifc_loader import load_ifc_models_by_slots
            from app.clash import compare_models_for_clashes
            loaded  = load_ifc_models_by_slots(session_id, slot_a, slot_b)
            clashes = compare_models_for_clashes(
                loaded["model_1"], loaded["model_2"], tolerance=tolerance
            )
            save_clash_cache(session_id, tolerance, clashes, slot_a=slot_a, slot_b=slot_b)

        if clash_index < 0 or clash_index >= len(clashes):
            return Response(content="Clash-Index außerhalb des Bereichs.", status_code=404)

        from app.bcf_export import create_bcf_zip_from_clashes
        data  = create_bcf_zip_from_clashes([clashes[clash_index]])
        fname = f"clash_{clash_index + 1:04d}.bcfzip"
        return Response(
            content=data, media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )
    except Exception as exc:
        return Response(content=f"Fehler: {exc}", status_code=500)
