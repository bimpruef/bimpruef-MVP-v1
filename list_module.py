import html
import ifcopenshell.util.element


# Typen, die beim Vergleich und in der Objektansicht ausgeschlossen werden.
# Beide Ansichten (Objektliste + Vergleich) verwenden dieselbe Liste,
# damit die angezeigten Elemente konsistent sind.
EXCLUDED_COMPARE_TYPES = {
    "IfcOpeningElement",
    "IfcAnnotation",
    "IfcGrid",
    "IfcGridAxis",
    "IfcProject",
    "IfcSite",
}


def get_psets_safe(element):
    try:
        psets = ifcopenshell.util.element.get_psets(element)
        return psets or {}
    except Exception:
        return {}


def get_predefined_type_safe(element):
    try:
        if hasattr(element, "PredefinedType"):
            value = getattr(element, "PredefinedType", None)
            return str(value) if value is not None else ""
    except Exception:
        return ""
    return ""


def extract_element_data(element, file_label: str = ""):
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


def get_candidate_products(model):
    """Gibt alle relevanten IfcProduct-Instanzen zurück.

    Wird sowohl für die Objektansicht als auch für Vergleich und
    Clash-Erkennung genutzt, sodass alle Ansichten dieselben Elemente zeigen.
    """
    candidates = []

    for obj in model.by_type("IfcProduct"):
        try:
            obj_type = obj.is_a()

            if obj_type in EXCLUDED_COMPARE_TYPES:
                continue

            global_id = getattr(obj, "GlobalId", None)
            if not global_id:
                continue

            candidates.append(obj)

        except Exception:
            continue

    return candidates


def get_objects_from_model(model, file_label: str):
    """Liest alle relevanten Objekte aus einem Modell aus.

    Verwendet get_candidate_products, damit Objektansicht und Vergleich
    dieselben Elemente berücksichtigen.
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


def filter_objects(objects, ifc_type=None, global_id=None):
    filtered = objects

    if ifc_type:
        filtered = [obj for obj in filtered if obj["type"] == ifc_type]

    if global_id:
        search_value = global_id.strip().lower()
        filtered = [
            obj for obj in filtered
            if search_value in str(obj["global_id"]).lower()
        ]

    return filtered


def build_object_rows(objects):
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
