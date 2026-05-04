"""
clash.py – BIMPruef Clash-Erkennung

Enthält:
  - AABB-basierte Clash-Erkennung für zwei beliebige Element-Gruppen
  - Hilfsfunktionen zum Laden und Filtern von Elementen aus mehreren Slots
  - Legacy-Wrapper compare_models_for_clashes (für Abwärtskompatibilität)
"""

import os
import ifcopenshell
import ifcopenshell.geom

from app.extractors import get_candidate_products, extract_element_data
from app.storage import get_ifc_path, get_ifc_label


# ─────────────────────────────────────────────────────────────────────────────
# Gemeinsame Filterlogik (identisch mit list_module._apply_filters)
# ─────────────────────────────────────────────────────────────────────────────

def _flatten_psets(psets: dict) -> dict:
    """Wandelt verschachtelte Psets in ein flaches Dict um: 'PsetName.PropName' → Wert"""
    flat = {}
    for pset_name, props in (psets or {}).items():
        if isinstance(props, dict):
            for prop_name, value in props.items():
                flat[f"{pset_name}.{prop_name}"] = value
    return flat


def apply_filters(elements: list, filters: list) -> list:
    """
    Wendet eine Liste von Filterregeln auf Element-Dicts an (AND-Logik).

    Jeder Filter ist ein Dict mit:
      - field:    'file_label' | 'type' | 'name' | 'global_id' | 'object_type' |
                  'predefined_type' | 'pset:<PsetName>.<PropName>'
      - operator: 'contains' | 'not_contains' | 'equals' | 'not_equals' |
                  'starts_with' | 'ends_with'
      - value:    Suchzeichenkette (case-insensitive)

    Filter ohne Wert werden übersprungen => kein Filter = alle Elemente.
    """
    result = elements
    for f in filters:
        field    = f.get("field", "")
        operator = f.get("operator", "contains")
        value    = str(f.get("value", "")).strip().lower()
        if not field or not value:
            continue

        def get_val(elem, fld=field):
            if fld.startswith("pset:"):
                key = fld[5:]
                flat = _flatten_psets(elem.get("psets", {}))
                return str(flat.get(key, "")).lower()
            return str(elem.get(fld, "")).lower()

        def matches(elem, op=operator, v=value):
            ev = get_val(elem)
            if op == "contains":     return v in ev
            if op == "not_contains": return v not in ev
            if op == "equals":       return ev == v
            if op == "not_equals":   return ev != v
            if op == "starts_with":  return ev.startswith(v)
            if op == "ends_with":    return ev.endswith(v)
            return True

        result = [e for e in result if matches(e)]
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Element-Laden aus mehreren Slots
# ─────────────────────────────────────────────────────────────────────────────

def load_elements_from_slots(session_id: str, slots: list) -> list:
    """
    Lädt alle Kandidaten-Elemente aus den angegebenen Slots.

    Gibt eine flache Liste von Element-Dicts zurück. Jedes Dict enthält
    zusätzlich die Felder 'slot' und '_ifc_element' (das originale
    ifcopenshell-Element für die spätere Geometrie-Berechnung).
    """
    all_elements = []
    for slot in slots:
        path = get_ifc_path(session_id, slot)
        if not os.path.exists(path):
            continue
        label = get_ifc_label(session_id, slot)
        try:
            model = ifcopenshell.open(path)
            for elem in get_candidate_products(model):
                data = extract_element_data(elem, file_label=label)
                data["slot"] = slot
                data["_ifc_element"] = elem   # fuer Bounding-Box-Berechnung
                all_elements.append(data)
        except Exception:
            continue
    return all_elements


def get_group_elements(session_id: str, selected_slots: list, filters: list) -> list:
    """
    Laedt alle Elemente aus den angegebenen Slots und filtert sie.

    Wenn keine Filterregel aktiv ist (oder alle Werte leer), werden alle
    Kandidaten-Elemente aus den Slots zurueckgegeben.
    """
    elements = load_elements_from_slots(session_id, selected_slots)
    return apply_filters(elements, filters)


# ─────────────────────────────────────────────────────────────────────────────
# Bounding-Box-Berechnung
# ─────────────────────────────────────────────────────────────────────────────

def get_bbox_for_element(element, settings):
    """Berechnet die Axis-Aligned Bounding Box (AABB) eines IFC-Elements.

    Gibt None zurueck, wenn die Geometrie nicht erzeugt werden konnte
    oder zu wenig Vertices vorhanden sind.
    """
    try:
        shape = ifcopenshell.geom.create_shape(settings, element)
        verts = shape.geometry.verts

        if not verts or len(verts) < 3:
            return None

        xs = verts[0::3]
        ys = verts[1::3]
        zs = verts[2::3]

        return {
            "min_x": min(xs),
            "min_y": min(ys),
            "min_z": min(zs),
            "max_x": max(xs),
            "max_y": max(ys),
            "max_z": max(zs),
        }
    except Exception:
        return None


def bboxes_intersect(b1, b2, tolerance=0.0):
    """Prueft, ob sich zwei Bounding Boxes ueberlappen (AABB-Test mit optionaler Toleranz)."""
    if b1 is None or b2 is None:
        return False

    return (
        b1["min_x"] <= b2["max_x"] + tolerance and
        b1["max_x"] >= b2["min_x"] - tolerance and
        b1["min_y"] <= b2["max_y"] + tolerance and
        b1["max_y"] >= b2["min_y"] - tolerance and
        b1["min_z"] <= b2["max_z"] + tolerance and
        b1["max_z"] >= b2["min_z"] - tolerance
    )


# ─────────────────────────────────────────────────────────────────────────────
# Gruppen-basierte Clash-Erkennung (neu)
# ─────────────────────────────────────────────────────────────────────────────

def compare_element_groups_for_clashes(
    group_a: list,
    group_b: list,
    tolerance: float = 0.0,
) -> list:
    """
    Vergleicht zwei vorbereitete Element-Gruppen auf Kollisionen.

    Beide Gruppen sind Listen von Element-Dicts (wie von get_group_elements
    zurueckgegeben). Jedes Dict muss das Feld '_ifc_element' enthalten.

    Regeln:
      - Elemente aus demselben Slot mit identischer GlobalId werden nicht
        mit sich selbst verglichen.
      - Doppelte Clash-Paare (gleiche GlobalId- und Slot-Kombination) werden
        dedupliziert.

    Returns:
        Liste von Clash-Dicts mit den Feldern:
          type_1, name_1, global_id_1, express_id_1, file_label_1, slot_1,
          type_2, name_2, global_id_2, express_id_2, file_label_2, slot_2
    """
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    def _with_bbox(group):
        result = []
        for elem_data in group:
            ifc_elem = elem_data.get("_ifc_element")
            if ifc_elem is None:
                continue
            bbox = get_bbox_for_element(ifc_elem, settings)
            if bbox is not None:
                result.append((elem_data, bbox))
        return result

    group_a_bboxes = _with_bbox(group_a)
    group_b_bboxes = _with_bbox(group_b)

    clashes    = []
    seen_pairs = set()

    for data_a, bbox_a in group_a_bboxes:
        gid_a  = data_a.get("global_id", "") or ""
        slot_a = data_a.get("slot") or 0

        for data_b, bbox_b in group_b_bboxes:
            gid_b  = data_b.get("global_id", "") or ""
            slot_b = data_b.get("slot") or 0

            # Element darf nicht mit sich selbst kollidieren
            if gid_a == gid_b and slot_a == slot_b:
                continue

            # Deduplizierung: Paare normalisiert speichern
            if slot_a < slot_b or (slot_a == slot_b and gid_a <= gid_b):
                pair_key = (gid_a, slot_a, gid_b, slot_b)
            else:
                pair_key = (gid_b, slot_b, gid_a, slot_a)

            if pair_key in seen_pairs:
                continue

            if bboxes_intersect(bbox_a, bbox_b, tolerance=tolerance):
                seen_pairs.add(pair_key)
                clashes.append({
                    "type_1":       data_a.get("type", ""),
                    "name_1":       data_a.get("name", ""),
                    "global_id_1":  gid_a,
                    "express_id_1": data_a.get("express_id", ""),
                    "file_label_1": data_a.get("file_label", ""),
                    "slot_1":       slot_a,
                    "type_2":       data_b.get("type", ""),
                    "name_2":       data_b.get("name", ""),
                    "global_id_2":  gid_b,
                    "express_id_2": data_b.get("express_id", ""),
                    "file_label_2": data_b.get("file_label", ""),
                    "slot_2":       slot_b,
                })

    return clashes


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Wrapper (Abwaertskompatibilitaet)
# ─────────────────────────────────────────────────────────────────────────────

def compare_models_for_clashes(model1, model2, tolerance=0.0):
    """
    Legacy-Funktion: vergleicht zwei ifcopenshell-Modelle direkt.

    Wird noch von alten Routen / BCF-Exporten verwendet.
    Intern werden die Elemente als synthetische Gruppen behandelt.
    """
    def _build_group(model, slot_id, label):
        group = []
        for elem in get_candidate_products(model):
            data = extract_element_data(elem, file_label=label)
            data["slot"] = slot_id
            data["_ifc_element"] = elem
            group.append(data)
        return group

    group_a = _build_group(model1, slot_id=1, label="model_1")
    group_b = _build_group(model2, slot_id=2, label="model_2")

    return compare_element_groups_for_clashes(group_a, group_b, tolerance=tolerance)
