"""
rulecheck.py – BIMPruef Rule-Check-Modul

Routen:
  GET  /viewer/rulecheck/        → Rule-Check-Seite (Slot-Auswahl + Regelkonfiguration)
  POST /viewer/rulecheck/run/    → Regelprüfung ausführen, gibt JSON zurück
  GET  /viewer/rulecheck/export/ → Export der Ergebnisse als JSON (für späteren Excel-Export vorbereitet)

Unterstützte Regeln (Version 1):
  missing_names          – Elemente ohne Name
  missing_global_id      – Elemente ohne GlobalId (direkt auf IfcProduct)
  missing_spaces         – Kein IfcSpace im Modell
  door_without_name      – Türen ohne Name
  window_without_name    – Fenster ohne Name
  wall_without_fire_rating – Wände ohne FireRating-Eigenschaft in PSet
  external_wall_check    – Wände ohne IsExternal-Information (nur Hinweis)
"""

import html
import ifcopenshell
import json
import os

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.storage import (
    get_ifc_path,
    get_ifc_label,
    get_session_slots,
    session_exists,
)
from app.extractors import get_candidate_products, extract_element_data

rulecheck_router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Regel-Definitionen (Metadaten für die UI)
# ─────────────────────────────────────────────────────────────────────────────

ALL_RULES = [
    {
        "id":          "missing_names",
        "label":       "Fehlende Namen",
        "description": "Prüft alle relevanten IfcProduct-Elemente auf fehlende Name-Attribute.",
        "severity":    "warning",
    },
    {
        "id":          "missing_global_id",
        "label":       "Fehlende GlobalId",
        "description": "Prüft alle IfcProduct-Elemente direkt auf fehlende GlobalId (auch solche, "
                       "die von get_candidate_products normalerweise ausgeschlossen werden).",
        "severity":    "error",
    },
    {
        "id":          "missing_spaces",
        "label":       "Fehlende Räume (IfcSpace)",
        "description": "Warnt, wenn im Modell keine IfcSpace-Elemente vorhanden sind.",
        "severity":    "warning",
    },
    {
        "id":          "door_without_name",
        "label":       "Türen ohne Name",
        "description": "Prüft alle IfcDoor-Elemente auf fehlende Namen.",
        "severity":    "warning",
    },
    {
        "id":          "window_without_name",
        "label":       "Fenster ohne Name",
        "description": "Prüft alle IfcWindow-Elemente auf fehlende Namen.",
        "severity":    "warning",
    },
    {
        "id":          "wall_without_fire_rating",
        "label":       "Wände ohne Brandschutzklasse",
        "description": "Prüft IfcWall/IfcWallStandardCase auf fehlende FireRating-Eigenschaft "
                       "(sucht nach: FireRating, Feuerwiderstand, Brandschutz – nicht Groß-/Kleinschreibung-sensitiv).",
        "severity":    "error",
    },
    {
        "id":          "external_wall_check",
        "label":       "Außenwand-Kennzeichnung",
        "description": "Prüft Wände auf IsExternal-Eigenschaft. Fehlende Kennzeichnung wird als Hinweis ausgegeben.",
        "severity":    "info",
    },
]

# Lookup: rule_id → Regeldefinition
_RULE_META = {r["id"]: r for r in ALL_RULES}


# ─────────────────────────────────────────────────────────────────────────────
# Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _e(s) -> str:
    """HTML-Escape-Kurzform."""
    return html.escape(str(s or ""))


def _flatten_psets(psets: dict) -> dict:
    """
    Wandelt verschachtelte Psets in ein flaches Dict um.
    Schlüsselformat: 'PsetName.PropName' → Wert (als String, kleingeschrieben)
    """
    flat = {}
    for pset_name, props in (psets or {}).items():
        if isinstance(props, dict):
            for prop_name, value in props.items():
                flat_key = f"{pset_name}.{prop_name}".lower()
                flat[flat_key] = str(value) if value is not None else ""
    return flat


def _psets_contain_key(psets: dict, search_terms: list) -> bool:
    """
    Gibt True zurück, wenn mindestens ein Schlüssel im flachen Pset-Dict
    einen der gesuchten Begriffe enthält (case-insensitive).
    """
    flat = _flatten_psets(psets)
    for key in flat:
        prop_name = key.split(".", 1)[-1] if "." in key else key
        for term in search_terms:
            if term.lower() in prop_name:
                return True
    return False


def _make_result(
    rule_id: str,
    severity: str,
    slot: int,
    file_label: str,
    ifc_type: str,
    name: str,
    global_id: str,
    express_id,
    message: str,
) -> dict:
    """Erstellt ein standardisiertes Ergebnis-Dict."""
    return {
        "rule_id":    rule_id,
        "severity":   severity,
        "slot":       slot,
        "file_label": file_label,
        "ifc_type":   ifc_type,
        "name":       name or "",
        "global_id":  global_id or "",
        "express_id": str(express_id or ""),
        "message":    message,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Einzelne Regelprüfungen
# ─────────────────────────────────────────────────────────────────────────────

def check_missing_names(model, slot: int, file_label: str) -> list:
    """Regel: missing_names – alle Kandidaten-Elemente ohne Name."""
    results = []
    for elem in get_candidate_products(model):
        data = extract_element_data(elem, file_label=file_label)
        if not data.get("name", "").strip():
            results.append(_make_result(
                rule_id    = "missing_names",
                severity   = "warning",
                slot       = slot,
                file_label = file_label,
                ifc_type   = data.get("type", ""),
                name       = "",
                global_id  = data.get("global_id", ""),
                express_id = data.get("express_id", ""),
                message    = f"Element ohne Name gefunden (Typ: {data.get('type', '?')}).",
            ))
    return results


def check_missing_global_id(model, slot: int, file_label: str) -> list:
    """
    Regel: missing_global_id – direkt auf IfcProduct prüfen,
    also auch Elemente, die get_candidate_products ausschließt.
    """
    results = []
    try:
        for obj in model.by_type("IfcProduct"):
            try:
                global_id = getattr(obj, "GlobalId", None)
                if not global_id:
                    ifc_type   = obj.is_a() if hasattr(obj, "is_a") else "IfcProduct"
                    name       = getattr(obj, "Name", None) or ""
                    express_id = obj.id() if hasattr(obj, "id") else ""
                    results.append(_make_result(
                        rule_id    = "missing_global_id",
                        severity   = "error",
                        slot       = slot,
                        file_label = file_label,
                        ifc_type   = ifc_type,
                        name       = name,
                        global_id  = "",
                        express_id = express_id,
                        message    = f"Element ohne GlobalId (Express-ID: {express_id}, Typ: {ifc_type}).",
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return results


def check_missing_spaces(model, slot: int, file_label: str) -> list:
    """Regel: missing_spaces – Warnung wenn kein einziges IfcSpace vorhanden ist."""
    results = []
    try:
        spaces = model.by_type("IfcSpace")
        if not spaces:
            results.append(_make_result(
                rule_id    = "missing_spaces",
                severity   = "warning",
                slot       = slot,
                file_label = file_label,
                ifc_type   = "IfcSpace",
                name       = "",
                global_id  = "",
                express_id = "",
                message    = "Kein IfcSpace im Modell gefunden. Raumstruktur fehlt möglicherweise.",
            ))
    except Exception:
        pass
    return results


def check_door_without_name(model, slot: int, file_label: str) -> list:
    """Regel: door_without_name – IfcDoor ohne Name."""
    results = []
    try:
        for door in model.by_type("IfcDoor"):
            try:
                name       = getattr(door, "Name", None) or ""
                global_id  = getattr(door, "GlobalId", None) or ""
                express_id = door.id() if hasattr(door, "id") else ""
                if not name.strip():
                    results.append(_make_result(
                        rule_id    = "door_without_name",
                        severity   = "warning",
                        slot       = slot,
                        file_label = file_label,
                        ifc_type   = "IfcDoor",
                        name       = "",
                        global_id  = global_id,
                        express_id = express_id,
                        message    = f"Tür ohne Name (GlobalId: {global_id or 'unbekannt'}).",
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return results


def check_window_without_name(model, slot: int, file_label: str) -> list:
    """Regel: window_without_name – IfcWindow ohne Name."""
    results = []
    try:
        for window in model.by_type("IfcWindow"):
            try:
                name       = getattr(window, "Name", None) or ""
                global_id  = getattr(window, "GlobalId", None) or ""
                express_id = window.id() if hasattr(window, "id") else ""
                if not name.strip():
                    results.append(_make_result(
                        rule_id    = "window_without_name",
                        severity   = "warning",
                        slot       = slot,
                        file_label = file_label,
                        ifc_type   = "IfcWindow",
                        name       = "",
                        global_id  = global_id,
                        express_id = express_id,
                        message    = f"Fenster ohne Name (GlobalId: {global_id or 'unbekannt'}).",
                    ))
            except Exception:
                continue
    except Exception:
        pass
    return results


def check_wall_without_fire_rating(model, slot: int, file_label: str) -> list:
    """
    Regel: wall_without_fire_rating – Wände ohne FireRating-Eigenschaft.
    Sucht case-insensitive nach: firerating, feuerwiderstand, brandschutz
    """
    results   = []
    fire_keys = ["firerating", "feuerwiderstand", "brandschutz"]

    wall_types = []
    for wt in ("IfcWall", "IfcWallStandardCase"):
        try:
            wall_types.extend(model.by_type(wt))
        except Exception:
            pass

    for wall in wall_types:
        try:
            import ifcopenshell.util.element
            psets      = ifcopenshell.util.element.get_psets(wall) or {}
            name       = getattr(wall, "Name", None) or ""
            global_id  = getattr(wall, "GlobalId", None) or ""
            express_id = wall.id() if hasattr(wall, "id") else ""
            ifc_type   = wall.is_a() if hasattr(wall, "is_a") else "IfcWall"

            if not _psets_contain_key(psets, fire_keys):
                results.append(_make_result(
                    rule_id    = "wall_without_fire_rating",
                    severity   = "error",
                    slot       = slot,
                    file_label = file_label,
                    ifc_type   = ifc_type,
                    name       = name,
                    global_id  = global_id,
                    express_id = express_id,
                    message    = (
                        f"Wand '{name or global_id}' hat keine FireRating-Eigenschaft "
                        "(FireRating / Feuerwiderstand / Brandschutz)."
                    ),
                ))
        except Exception:
            continue

    return results


def check_external_wall(model, slot: int, file_label: str) -> list:
    """
    Regel: external_wall_check – Wände ohne IsExternal-Information (Hinweis).
    Sucht case-insensitive nach: isexternal
    """
    results    = []
    ext_keys   = ["isexternal"]

    wall_types = []
    for wt in ("IfcWall", "IfcWallStandardCase"):
        try:
            wall_types.extend(model.by_type(wt))
        except Exception:
            pass

    for wall in wall_types:
        try:
            import ifcopenshell.util.element
            psets      = ifcopenshell.util.element.get_psets(wall) or {}
            name       = getattr(wall, "Name", None) or ""
            global_id  = getattr(wall, "GlobalId", None) or ""
            express_id = wall.id() if hasattr(wall, "id") else ""
            ifc_type   = wall.is_a() if hasattr(wall, "is_a") else "IfcWall"

            if not _psets_contain_key(psets, ext_keys):
                results.append(_make_result(
                    rule_id    = "external_wall_check",
                    severity   = "info",
                    slot       = slot,
                    file_label = file_label,
                    ifc_type   = ifc_type,
                    name       = name,
                    global_id  = global_id,
                    express_id = express_id,
                    message    = (
                        f"Wand '{name or global_id}' hat keine IsExternal-Eigenschaft. "
                        "Innen-/Außenwand-Kennzeichnung fehlt möglicherweise."
                    ),
                ))
        except Exception:
            continue

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Regel-Dispatcher
# ─────────────────────────────────────────────────────────────────────────────

_RULE_FUNCTIONS = {
    "missing_names":           check_missing_names,
    "missing_global_id":       check_missing_global_id,
    "missing_spaces":          check_missing_spaces,
    "door_without_name":       check_door_without_name,
    "window_without_name":     check_window_without_name,
    "wall_without_fire_rating":check_wall_without_fire_rating,
    "external_wall_check":     check_external_wall,
}


def run_rules_on_model(model, slot: int, file_label: str, rules: list) -> list:
    """
    Führt alle übergebenen Regelprüfungen auf einem geladenen IFC-Modell aus.

    Args:
        model:      geöffnetes ifcopenshell-Modell
        slot:       Slot-Nummer (für Ergebnisanzeige)
        file_label: Dateiname / Label (für Ergebnisanzeige)
        rules:      Liste von Regel-IDs (Strings)

    Returns:
        Flache Liste von Ergebnis-Dicts.
    """
    results = []
    for rule_id in rules:
        fn = _RULE_FUNCTIONS.get(rule_id)
        if fn is None:
            continue  # unbekannte Regel überspringen
        try:
            results.extend(fn(model, slot, file_label))
        except Exception as exc:
            # Einzelne Regelfehler sollen die anderen nicht blockieren
            results.append(_make_result(
                rule_id    = rule_id,
                severity   = "error",
                slot       = slot,
                file_label = file_label,
                ifc_type   = "",
                name       = "",
                global_id  = "",
                express_id = "",
                message    = f"Interner Fehler beim Ausführen der Regel '{rule_id}': {exc}",
            ))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# UI-Hilfsfunktionen (identischer Stil wie viewer.py)
# ─────────────────────────────────────────────────────────────────────────────

# CSS-Basis (direkt aus viewer.py übernommen, damit der Stil identisch bleibt)
DARK_STYLES = """\
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0e0e1a;--surface:#16213e;--surface2:#1a2a4a;
  --border:#1e3a6e;--accent:#4fc3f7;--accent2:#e94560;
  --text:#d0dce8;--muted:#4a6080;--success:#4caf50;
  --warn:#ffb74d;--info:#64b5f6;
}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);
  color:var(--text);min-height:100vh;line-height:1.5}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
button,.btn{padding:7px 14px;background:var(--surface2);border:1px solid var(--border);
  color:var(--text);border-radius:6px;cursor:pointer;font-size:13px;
  transition:background .15s,border-color .15s}
button:hover,.btn:hover{background:#223a5e;border-color:var(--accent)}
.btn-primary{background:var(--accent);color:#0a1a2e;border-color:var(--accent);font-weight:600}
.btn-primary:hover{background:#81d4fa}
.card{background:var(--surface);border:1px solid var(--border);
  border-radius:10px;padding:18px;margin-bottom:16px}
table{border-collapse:collapse;width:100%}
th,td{border:1px solid var(--border);padding:8px 10px;text-align:left;vertical-align:top;font-size:12px}
th{background:var(--surface2);color:#8ab;font-weight:600;position:sticky;top:0;z-index:1}
tr:hover td{background:rgba(79,195,247,.04)}
footer{text-align:center;padding:24px 0 12px;border-top:1px solid var(--border);
  color:var(--muted);font-size:12px;margin-top:40px}
label{cursor:pointer}
input[type=checkbox]{accent-color:var(--accent);width:14px;height:14px;cursor:pointer}
.flash-err{background:#2a0a10;border:1px solid var(--accent2);border-radius:8px;
  padding:10px 14px;color:#ffaaaa;font-size:13px;margin-bottom:14px}
.sev-error{color:#ef9a9a;font-weight:600}
.sev-warning{color:var(--warn);font-weight:600}
.sev-info{color:var(--info)}
.badge{display:inline-block;padding:2px 9px;border-radius:12px;font-size:11px;font-weight:600;margin-right:4px}
.badge-error{background:#3e0d1a;color:#ef9a9a;border:1px solid #6e1a2e}
.badge-warning{background:#3e2800;color:#ffb74d;border:1px solid #6e4800}
.badge-info{background:#0d2a3e;color:#64b5f6;border:1px solid #1a4a6e}
@keyframes spin{to{transform:rotate(360deg)}}
</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title>
{DARK_STYLES}
<link rel="stylesheet" href="/static/bimpruef.css">
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


def _brand_logo(height_px: int = 28) -> str:
    """Brand-Element – identisch mit viewer.py."""
    icon_size = max(27, int(height_px * 1.28))
    text_size = max(18, int(height_px * 0.92))
    return (
        f'<div style="display:flex;align-items:center;gap:8px;margin-right:12px;line-height:1">'
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 54" role="img" '
        f'aria-label="BIMpruef icon" style="height:{icon_size}px;width:auto;display:block">'
        f'<g fill="none" stroke-linejoin="round" stroke-linecap="round" stroke-width="2.8">'
        f'<g stroke="#1f5f9f">'
        f'<path d="M8 18 24 8l16 10-16 10z"/><path d="M8 18v18l16 10V28z"/><path d="M40 18v18L24 46V28z"/>'
        f'</g><g stroke="#d8192f">'
        f'<path d="M29 18 45 8l16 10-16 10z"/><path d="M29 18v18l16 10V28z"/><path d="M61 18v18L45 46V28z"/>'
        f'</g><g stroke="#8f4399">'
        f'<path d="M50 18 66 8l16 10-16 10z"/><path d="M50 18v18l16 10V28z"/><path d="M82 18v18L66 46V28z"/>'
        f'</g><g stroke="#27a6ad">'
        f'<path d="M71 18 87 8l16 10-16 10z"/><path d="M71 18v18l16 10V28z"/><path d="M103 18v18L87 46V28z"/>'
        f'</g></g></svg>'
        f'<span style="font-family:\'Avenir Next\',\'Montserrat\',\'Segoe UI\',sans-serif;'
        f'font-weight:300;letter-spacing:1.2px;color:var(--text);font-size:{text_size}px;'
        f'white-space:nowrap;text-transform:uppercase">BIMPRUEF</span></div>'
    )


def _topbar(session_id: str, active: str = "", project_id: str = "") -> str:
    """Rendert die gemeinsame projektfähige Viewer-Navigation."""
    from app.viewer import _topbar as viewer_topbar
    return viewer_topbar(session_id, active=active, project_id=project_id)


# ─────────────────────────────────────────────────────────────────────────────
# Route 1: GET /viewer/rulecheck/  – Konfigurationsseite
# ─────────────────────────────────────────────────────────────────────────────

@rulecheck_router.get("/viewer/rulecheck/")
def viewer_rulecheck(session_id: str = Query(...), project_id: str = Query(default="")):
    """Zeigt die Rule-Check-Konfigurationsseite."""

    if not session_exists(session_id):
        return _page("Rule-Check – BIMPruef", f"""
        {_topbar('', 'rulecheck', project_id)}
        <div style="padding:32px 24px">
          <div class="flash-err">Session nicht gefunden. Bitte Datei erneut hochladen.</div>
        </div>""")

    slots = get_session_slots(session_id)
    sid   = _e(session_id)

    # Slot-Auswahl-HTML
    if not slots:
        slots_html = (
            '<div class="flash-err">'
            'Keine IFC-Dateien in dieser Session. Bitte zuerst eine Datei hochladen.'
            '</div>'
        )
    else:
        slots_html = '<div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:8px">'
        for s in slots:
            label = _e(get_ifc_label(session_id, s))
            slots_html += (
                f'<label style="display:flex;align-items:center;gap:6px;'
                f'background:var(--surface2);border:1px solid var(--border);'
                f'border-radius:8px;padding:8px 14px;font-size:13px">'
                f'<input type="checkbox" name="slot" value="{s}" checked> '
                f'Slot&nbsp;{s}&nbsp;–&nbsp;<strong>{label}</strong>'
                f'</label>'
            )
        slots_html += '</div>'

    # Regeln-HTML
    rules_html = '<div style="display:flex;flex-direction:column;gap:8px">'
    for rule in ALL_RULES:
        sev      = rule["severity"]
        badge_cl = f"badge-{sev}"
        badge_lb = {"error": "Fehler", "warning": "Warnung", "info": "Hinweis"}.get(sev, sev)
        rules_html += (
            f'<label style="display:flex;align-items:flex-start;gap:10px;'
            f'background:var(--surface2);border:1px solid var(--border);'
            f'border-radius:8px;padding:10px 14px;font-size:13px;cursor:pointer">'
            f'<input type="checkbox" name="rule" value="{_e(rule["id"])}" checked '
            f'style="margin-top:2px"> '
            f'<div>'
            f'<div style="font-weight:600">{_e(rule["label"])} '
            f'<span class="badge {badge_cl}">{badge_lb}</span></div>'
            f'<div style="color:var(--muted);font-size:11px;margin-top:2px">'
            f'{_e(rule["description"])}</div>'
            f'</div>'
            f'</label>'
        )
    rules_html += '</div>'

    body = f"""
{_topbar(session_id, "rulecheck", project_id)}

<div style="max-width:900px;margin:32px auto;padding:0 24px">

  <h1 style="font-size:22px;margin-bottom:4px">✅ Rule-Check</h1>
  <p style="color:var(--muted);font-size:13px;margin-bottom:24px">
    Wähle die zu prüfenden Modell-Slots und Regeln aus, dann starte die Prüfung.
  </p>

  <!-- Slot-Auswahl -->
  <div class="card">
    <h2 style="font-size:15px;margin-bottom:12px">Modell-Slots</h2>
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
    <button id="btn-run" class="btn btn-primary" style="font-size:14px;padding:10px 28px"
      onclick="runCheck()">▶ Prüfung starten</button>
    <span id="run-status" style="margin-left:14px;font-size:12px;color:var(--muted)"></span>
  </div>

  <!-- Ergebnis-Bereich (wird per JS befüllt) -->
  <div id="result-area" style="display:none">

    <!-- Zusammenfassung -->
    <div class="card" id="result-summary"></div>

    <!-- Filter -->
    <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap" id="sev-filter">
      <button class="btn" data-sev="all"     onclick="filterSev('all')"     style="font-size:12px">Alle</button>
      <button class="btn" data-sev="error"   onclick="filterSev('error')"   style="font-size:12px">Nur Fehler</button>
      <button class="btn" data-sev="warning" onclick="filterSev('warning')" style="font-size:12px">Nur Warnungen</button>
      <button class="btn" data-sev="info"    onclick="filterSev('info')"    style="font-size:12px">Nur Hinweise</button>
      <a id="btn-export" href="#" style="display:none;margin-left:auto" class="btn"
        style="font-size:12px">⬇ JSON exportieren</a>
    </div>

    <!-- Ergebnis-Tabelle -->
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
const SESSION_ID = {json.dumps(session_id)};
const PROJECT_ID = {json.dumps(project_id)};
let _allResults = [];
let _currentFilter = 'all';

function toggleAll(state) {{
  document.querySelectorAll('#rule-selection input[type=checkbox]')
    .forEach(cb => cb.checked = state);
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
  if (sev === 'all') {{
    renderTable(_allResults);
  }} else {{
    renderTable(_allResults.filter(r => r.severity === sev));
  }}
}}

async function runCheck() {{
  const slots = [...document.querySelectorAll('#slot-selection input[name=slot]:checked')]
    .map(cb => parseInt(cb.value));
  const rules = [...document.querySelectorAll('#rule-selection input[name=rule]:checked')]
    .map(cb => cb.value);

  if (!slots.length) {{
    alert('Bitte mindestens einen Slot auswählen.');
    return;
  }}
  if (!rules.length) {{
    alert('Bitte mindestens eine Regel auswählen.');
    return;
  }}

  const btn    = document.getElementById('btn-run');
  const status = document.getElementById('run-status');
  btn.disabled = true;
  status.textContent = 'Prüfung läuft …';
  document.getElementById('result-area').style.display = 'none';

  try {{
    const resp = await fetch('/viewer/rulecheck/run/', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{session_id: SESSION_ID, slots, rules}}),
    }});

    let data;
    try {{
      data = await resp.json();
    }} catch (_) {{
      const txt = await resp.text().catch(() => '(kein Body)');
      alert(`HTTP ${{resp.status}} – Antwort konnte nicht geparst werden:\n${{txt.slice(0, 400)}}`);
      status.textContent = '';
      btn.disabled = false;
      return;
    }}

    if (!resp.ok || data.error) {{
      const msg = data.error || data.detail
        || JSON.stringify(data).slice(0, 400)
        || `HTTP ${{resp.status}}`;
      status.textContent = '';
      alert('Fehler: ' + msg);
      btn.disabled = false;
      return;
    }}

    _allResults = data.results || [];
    const counts = data.summary || {{}};

    // Zusammenfassung
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

    // Export-Link aufbauen
    const exportParams = new URLSearchParams({{
      session_id: SESSION_ID,
      slots: slots.join(','),
      rules: rules.join(','),
    }});
    const exportBtn = document.getElementById('btn-export');
    exportBtn.href  = '/viewer/rulecheck/export/?' + exportParams.toString();
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
</script>"""

    return _page("Rule-Check – BIMPruef", body)


# ─────────────────────────────────────────────────────────────────────────────
# Route 2: POST /viewer/rulecheck/run/  – Prüfung ausführen
# ─────────────────────────────────────────────────────────────────────────────

@rulecheck_router.post("/viewer/rulecheck/run/")
async def viewer_rulecheck_run(request: Request):
    """
    Nimmt JSON entgegen:
      { "session_id": "...", "slots": [1, 2], "rules": ["missing_names", ...] }
    Gibt JSON zurück:
      { "summary": {...}, "results": [...] }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Ungültiges JSON."}, status_code=400)

    session_id = str(body.get("session_id", "")).strip()
    slots      = body.get("slots", [])
    rules      = body.get("rules", [])

    # Eingabe-Validierung
    if not session_id:
        return JSONResponse({"error": "session_id fehlt."}, status_code=400)
    if not session_exists(session_id):
        return JSONResponse({"error": "Session nicht gefunden."}, status_code=404)
    if not slots:
        return JSONResponse({"error": "Keine Slots angegeben."}, status_code=400)
    if not rules:
        return JSONResponse({"error": "Keine Regeln angegeben."}, status_code=400)

    # Nur ganzzahlige, bekannte Slots zulassen
    try:
        slots = [int(s) for s in slots]
    except (TypeError, ValueError):
        return JSONResponse({"error": "Ungültige Slot-Angabe."}, status_code=400)

    all_results      = []
    slots_checked    = 0
    slot_errors      = []

    for slot in slots:
        ifc_path = get_ifc_path(session_id, slot)
        if not os.path.exists(ifc_path):
            slot_errors.append(f"Slot {slot}: Datei nicht gefunden.")
            continue

        file_label = get_ifc_label(session_id, slot, fallback=f"model_{slot}.ifc")
        try:
            model = ifcopenshell.open(ifc_path)
        except Exception as exc:
            slot_errors.append(f"Slot {slot}: IFC-Datei konnte nicht geöffnet werden – {exc}")
            continue

        results = run_rules_on_model(model, slot=slot, file_label=file_label, rules=rules)
        all_results.extend(results)
        slots_checked += 1

    # Zusammenfassung berechnen
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


# ─────────────────────────────────────────────────────────────────────────────
# Route 3: GET /viewer/rulecheck/export/  – JSON-Export (Excel vorbereitet)
# ─────────────────────────────────────────────────────────────────────────────

@rulecheck_router.get("/viewer/rulecheck/export/")
def viewer_rulecheck_export(
    session_id: str = Query(...),
    slots: str      = Query(default=""),   # kommagetrennte Slot-Nummern
    rules: str      = Query(default=""),   # kommagetrennte Regel-IDs
):
    """
    Exportiert die Prüfergebnisse als JSON-Datei.

    TODO (zukünftige Version): Excel-Export über openpyxl ergänzen.
    Das Modul openpyxl ist bereits in requirements.txt enthalten.
    Der JSON-Export dient als Basis und kann direkt in Excel importiert werden.
    """
    if not session_exists(session_id):
        return Response(content="Session nicht gefunden.", status_code=404)

    slot_list = []
    for s in slots.split(","):
        s = s.strip()
        if s.isdigit():
            slot_list.append(int(s))

    rule_list = [r.strip() for r in rules.split(",") if r.strip()]

    if not slot_list:
        slot_list = get_session_slots(session_id)
    if not rule_list:
        rule_list = list(_RULE_FUNCTIONS.keys())

    all_results = []
    for slot in slot_list:
        ifc_path = get_ifc_path(session_id, slot)
        if not os.path.exists(ifc_path):
            continue
        file_label = get_ifc_label(session_id, slot, fallback=f"model_{slot}.ifc")
        try:
            model   = ifcopenshell.open(ifc_path)
            results = run_rules_on_model(model, slot=slot, file_label=file_label, rules=rule_list)
            all_results.extend(results)
        except Exception:
            continue

    export_data = {
        "session_id": session_id,
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
