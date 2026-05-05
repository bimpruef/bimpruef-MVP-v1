"""
extractors.py – BIMPruef element extraction and filtering utilities

This module is the single source of truth for:
  - which IFC types are considered candidates for comparison / clash detection
  - how raw ifcopenshell elements are serialised to plain dicts
  - the shared filter / flatten logic used by both list_module and clash.py

Keeping these helpers here avoids the duplication that previously existed
between clash.py and list_module.py.
"""

import html
import ifcopenshell.util.element


# ---------------------------------------------------------------------------
# Types excluded from all views (object list, compare, clash detection).
# A single definition ensures every view operates on the same element set.
# ---------------------------------------------------------------------------

EXCLUDED_COMPARE_TYPES = frozenset(
    {
        "IfcOpeningElement",
        "IfcAnnotation",
        "IfcGrid",
        "IfcGridAxis",
        "IfcProject",
        "IfcSite",
    }
)


# ---------------------------------------------------------------------------
# Low-level element data extraction
# ---------------------------------------------------------------------------


def get_psets_safe(element) -> dict:
    try:
        psets = ifcopenshell.util.element.get_psets(element)
        return psets or {}
    except Exception:
        return {}


def get_predefined_type_safe(element) -> str:
    try:
        if hasattr(element, "PredefinedType"):
            value = getattr(element, "PredefinedType", None)
            return str(value) if value is not None else ""
    except Exception:
        return ""
    return ""


def extract_element_data(element, file_label: str = "") -> dict:
    """Serialise a single ifcopenshell element to a plain dict."""
    return {
        "file_label": file_label,
        "express_id": element.id() if hasattr(element, "id") else "",
        "type": element.is_a() if hasattr(element, "is_a") else "",
        "name": getattr(element, "Name", None) or "",
        "global_id": getattr(element, "GlobalId", None) or "",
        "object_type": getattr(element, "ObjectType", None) or "",
        "predefined_type": get_predefined_type_safe(element),
        "psets": get_psets_safe(element),
    }


def get_candidate_products(model) -> list:
    """
    Return all relevant IfcProduct instances from *model*.

    Used by the object list view, model comparison, and clash detection so
    that every part of the application operates on the same element set.
    """
    candidates = []
    for obj in model.by_type("IfcProduct"):
        try:
            if obj.is_a() in EXCLUDED_COMPARE_TYPES:
                continue
            if not getattr(obj, "GlobalId", None):
                continue
            candidates.append(obj)
        except Exception:
            continue
    return candidates


def get_objects_from_model(model, file_label: str) -> list:
    """
    Extract all candidate elements from *model* as plain dicts.

    Uses ``get_candidate_products`` so the object list and the comparison
    view always show the same elements.
    """
    result = []
    for element in get_candidate_products(model):
        try:
            result.append(extract_element_data(element, file_label=file_label))
        except Exception as exc:
            result.append(
                {
                    "file_label": file_label,
                    "express_id": "ERROR",
                    "type": "ERROR",
                    "name": "",
                    "global_id": "",
                    "object_type": "",
                    "predefined_type": "",
                    "psets": {"Error": {"message": str(exc)}},
                }
            )
    return result


# ---------------------------------------------------------------------------
# Shared filter helpers
# (previously duplicated between clash.py and list_module.py)
# ---------------------------------------------------------------------------


def flatten_psets(psets: dict) -> dict:
    """
    Flatten nested pset dicts to ``'PsetName.PropName' → value`` pairs.

    Example::

        {'Pset_WallCommon': {'IsExternal': True}}
        → {'Pset_WallCommon.IsExternal': True}
    """
    flat: dict = {}
    for pset_name, props in (psets or {}).items():
        if isinstance(props, dict):
            for prop_name, value in props.items():
                flat[f"{pset_name}.{prop_name}"] = value
    return flat


def apply_filters(elements: list, filters: list) -> list:
    """
    Filter a list of element dicts using AND-logic.

    Each filter is a dict with:
      field    – ``'file_label'`` | ``'type'`` | ``'name'`` | ``'global_id'``
                 | ``'object_type'`` | ``'predefined_type'``
                 | ``'pset:<PsetName>.<PropName>'``
      operator – ``'contains'`` | ``'not_contains'`` | ``'equals'``
                 | ``'not_equals'`` | ``'starts_with'`` | ``'ends_with'``
      value    – search string (case-insensitive)

    Filters with an empty *value* are skipped, so an empty filter list
    returns all elements unchanged.
    """
    result = elements
    for f in filters:
        field = f.get("field", "")
        operator = f.get("operator", "contains")
        value = str(f.get("value", "")).strip().lower()
        if not field or not value:
            continue

        def _get_val(elem, fld=field) -> str:
            if fld.startswith("pset:"):
                key = fld[5:]
                return str(flatten_psets(elem.get("psets", {})).get(key, "")).lower()
            return str(elem.get(fld, "")).lower()

        def _matches(elem, op=operator, v=value) -> bool:
            ev = _get_val(elem)
            if op == "contains":
                return v in ev
            if op == "not_contains":
                return v not in ev
            if op == "equals":
                return ev == v
            if op == "not_equals":
                return ev != v
            if op == "starts_with":
                return ev.startswith(v)
            if op == "ends_with":
                return ev.endswith(v)
            return True

        result = [e for e in result if _matches(e)]
    return result


# ---------------------------------------------------------------------------
# HTML rendering helpers  (used by the object-list view)
# ---------------------------------------------------------------------------


def format_psets_to_html(psets: dict) -> str:
    if not psets:
        return '<span class="muted">Keine Eigenschaften</span>'

    parts = []
    for pset_name, props in psets.items():
        parts.append('<div class="pset-block">')
        parts.append(f'<div class="pset-title">{html.escape(str(pset_name))}</div>')

        if isinstance(props, dict) and props:
            parts.append('<table class="prop-table">')
            parts.append("<tr><th>Eigenschaft</th><th>Wert</th></tr>")
            for prop_name, value in props.items():
                parts.append(
                    "<tr>"
                    f"<td>{html.escape(str(prop_name))}</td>"
                    f"<td>{html.escape(str(value))}</td>"
                    "</tr>"
                )
            parts.append("</table>")
        else:
            parts.append('<div class="muted">Keine Werte</div>')
        parts.append("</div>")

    return "".join(parts)


def filter_objects(objects: list, ifc_type: str = None, global_id: str = None) -> list:
    filtered = objects
    if ifc_type:
        filtered = [obj for obj in filtered if obj["type"] == ifc_type]
    if global_id:
        search_value = global_id.strip().lower()
        filtered = [
            obj
            for obj in filtered
            if search_value in str(obj["global_id"]).lower()
        ]
    return filtered


def build_object_rows(objects: list) -> str:
    rows_html = []
    for index, obj in enumerate(objects, start=1):
        properties_html = format_psets_to_html(obj["psets"])
        row = (
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{html.escape(str(obj['file_label']))}</td>"
            f"<td>{html.escape(str(obj['express_id']))}</td>"
            f"<td>{html.escape(str(obj['type']))}</td>"
            f"<td>{html.escape(str(obj['name']))}</td>"
            f"<td>{html.escape(str(obj['global_id']))}</td>"
            f"<td>{properties_html}</td>"
            "</tr>"
        )
        rows_html.append(row)
    return "".join(rows_html)
