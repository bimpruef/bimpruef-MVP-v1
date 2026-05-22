"""
rulecheck.py – BIMPruef Rule-Check-Modul

Dieses Modul stellt nur noch die Regellogik bereit (Funktionen, Regel-Definitionen).
Die Routen /viewer/rulecheck/* leiten auf das eigenständige Projektmodul um.

Legacy-Redirects (für Abwärtskompatibilität):
  GET  /viewer/rulecheck/        → /projects/{project_id}/checking  (302)
  POST /viewer/rulecheck/run/    → 410 Gone
  GET  /viewer/rulecheck/export/ → 410 Gone

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
# Legacy-Routen: /viewer/rulecheck/* → Projectmodul-Redirects
#
# Die UI lebt jetzt in project_rulecheck.py unter:
#   GET  /projects/{project_id}/checking
#   POST /projects/{project_id}/checking/run
#   GET  /projects/{project_id}/checking/export
#
# Diese Stubs leiten alte Bookmarks / Links um, damit keine 404-Fehler entstehen.
# ─────────────────────────────────────────────────────────────────────────────

@rulecheck_router.get("/viewer/rulecheck/")
def viewer_rulecheck_legacy(
    project_id: str = Query(default=""),
    session_id: str = Query(default=""),
):
    """Legacy-Redirect: Rule-Check ist jetzt ein eigenständiges Projektmodul."""
    if project_id:
        from fastapi.responses import RedirectResponse as _RR
        return _RR(f"/projects/{project_id}/checking", status_code=302)
    # Ohne project_id kein sinnvoller Redirect möglich → Hinweisseite
    from fastapi.responses import HTMLResponse as _HR
    return _HR(
        "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
        "<title>Rule-Check verschoben – BIMPruef</title></head><body style='"
        "font-family:sans-serif;padding:40px;background:#0e0e1a;color:#d0dce8'>"
        "<h2>Rule-Check ist jetzt ein eigenständiges Projektmodul</h2>"
        "<p style='color:#4a6080;margin-top:8px'>Öffne ein Projekt und wechsle zum Tab "
        "<strong>Checking</strong>. IFC-Dateien werden dort aus Documents geladen.</p>"
        "<p style='margin-top:20px'><a href='/' style='color:#4fc3f7'>Zur Projektübersicht</a></p>"
        "</body></html>"
    )


@rulecheck_router.post("/viewer/rulecheck/run/")
async def viewer_rulecheck_run_legacy():
    """Legacy: Diese API ist nach /projects/{project_id}/checking/run verschoben."""
    from fastapi.responses import JSONResponse as _JR
    return _JR(
        {"error": "Diese API wurde nach /projects/{project_id}/checking/run verschoben."},
        status_code=410,
    )


@rulecheck_router.get("/viewer/rulecheck/export/")
def viewer_rulecheck_export_legacy():
    """Legacy: Diese API ist nach /projects/{project_id}/checking/export verschoben."""
    from fastapi.responses import Response as _R
    return _R(
        content="Dieser Endpunkt wurde nach /projects/{project_id}/checking/export verschoben.",
        status_code=410,
    )
