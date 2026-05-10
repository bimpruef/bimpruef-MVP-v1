"""
clash.py – BIMPruef Clash-Erkennung

Enthält:
  - AABB-basierte Clash-Erkennung für zwei beliebige Element-Gruppen
  - Hilfsfunktionen zum Laden und Filtern von Elementen aus mehreren Slots
  - Legacy-Wrapper compare_models_for_clashes (für Abwärtskompatibilität)

Filter- und Flatten-Logik lebt ausschliesslich in extractors.py; clash.py
und list_module importieren von dort, damit kein Code dupliziert wird.
"""

import os

import ifcopenshell
import ifcopenshell.geom

from app.extractors import apply_filters, extract_element_data, get_candidate_products
from app.storage import get_ifc_label, get_ifc_path


# ---------------------------------------------------------------------------
# Element loading
# ---------------------------------------------------------------------------


def load_elements_from_slots(session_id: str, slots: list) -> list:
    """
    Load all candidate elements from the given *slots*.

    Returns a flat list of element dicts.  Each dict carries two extra fields:
      - ``slot``         – the slot number the element came from
      - ``_ifc_element`` – the original ifcopenshell object (needed for
                           geometry / bounding-box computation)
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
                data["_ifc_element"] = elem
                all_elements.append(data)
        except Exception:
            continue
    return all_elements


def get_group_elements(
    session_id: str, selected_slots: list, filters: list
) -> list:
    """
    Return all elements from *selected_slots* that pass *filters*.

    Delegates filtering to ``extractors.apply_filters`` so the logic is
    defined in exactly one place.
    """
    elements = load_elements_from_slots(session_id, selected_slots)
    return apply_filters(elements, filters)


# ---------------------------------------------------------------------------
# Bounding-box helpers
# ---------------------------------------------------------------------------


def _make_geometry_settings() -> ifcopenshell.geom.settings:
    """Create and return a geometry settings object for world-coordinate AABBs."""
    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)
    return settings


def get_bbox_for_element(element, settings) -> dict | None:
    """
    Compute the axis-aligned bounding box (AABB) of *element*.

    Returns ``None`` when geometry cannot be computed or has fewer than one
    complete vertex triple.

    The caller is responsible for passing a *settings* object so that a single
    instance can be reused across many elements (construction is not cheap).
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


def bboxes_intersect(b1: dict, b2: dict, tolerance: float = 0.0) -> bool:
    """
    Return True when two AABBs overlap (with optional outward *tolerance*).

    Both arguments must be non-None dicts with the six min/max keys.
    """
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


# ---------------------------------------------------------------------------
# Group-based clash detection
# ---------------------------------------------------------------------------


def compare_element_groups_for_clashes(
    group_a: list,
    group_b: list,
    tolerance: float = 0.0,
) -> list:
    """
    Compare two prepared element groups for geometry collisions.

    Both groups are lists of element dicts as returned by
    ``get_group_elements``.  Each dict must contain ``'_ifc_element'``.

    Rules:
      - Elements from the same slot with the same GlobalId are not compared
        against themselves.
      - Duplicate clash pairs (same GlobalId + slot combination) are
        deduplicated.

    Returns a list of clash dicts with fields:
      type_1, name_1, global_id_1, express_id_1, file_label_1, slot_1,
      type_2, name_2, global_id_2, express_id_2, file_label_2, slot_2
    """
    # Build the settings object once; reuse it for every element.
    settings = _make_geometry_settings()

    def _attach_bboxes(group: list) -> list:
        result = []
        for elem_data in group:
            ifc_elem = elem_data.get("_ifc_element")
            if ifc_elem is None:
                continue
            bbox = get_bbox_for_element(ifc_elem, settings)
            if bbox is not None:
                result.append((elem_data, bbox))
        return result

    group_a_bboxes = _attach_bboxes(group_a)
    group_b_bboxes = _attach_bboxes(group_b)

    clashes: list = []
    seen_pairs: set = set()

    for data_a, bbox_a in group_a_bboxes:
        gid_a = data_a.get("global_id") or ""
        slot_a = data_a.get("slot") or 0

        for data_b, bbox_b in group_b_bboxes:
            gid_b = data_b.get("global_id") or ""
            slot_b = data_b.get("slot") or 0

            # Skip self-comparison
            if gid_a == gid_b and slot_a == slot_b:
                continue

            # Normalise pair key so (a, b) and (b, a) map to the same entry
            if slot_a < slot_b or (slot_a == slot_b and gid_a <= gid_b):
                pair_key = (gid_a, slot_a, gid_b, slot_b)
            else:
                pair_key = (gid_b, slot_b, gid_a, slot_a)

            if pair_key in seen_pairs:
                continue

            if bboxes_intersect(bbox_a, bbox_b, tolerance=tolerance):
                seen_pairs.add(pair_key)
                clashes.append(
                    {
                        "type_1": data_a.get("type", ""),
                        "name_1": data_a.get("name", ""),
                        "global_id_1": gid_a,
                        "express_id_1": data_a.get("express_id", ""),
                        "file_label_1": data_a.get("file_label", ""),
                        "slot_1": slot_a,
                        "document_id_1": data_a.get("document_id", ""),
                        "document_name_1": data_a.get("document_name", data_a.get("file_label", "")),
                        "type_2": data_b.get("type", ""),
                        "name_2": data_b.get("name", ""),
                        "global_id_2": gid_b,
                        "express_id_2": data_b.get("express_id", ""),
                        "file_label_2": data_b.get("file_label", ""),
                        "slot_2": slot_b,
                        "document_id_2": data_b.get("document_id", ""),
                        "document_name_2": data_b.get("document_name", data_b.get("file_label", "")),
                    }
                )

    return clashes


# ---------------------------------------------------------------------------
# Legacy wrapper
# ---------------------------------------------------------------------------


def compare_models_for_clashes(model1, model2, tolerance: float = 0.0) -> list:
    """
    Compare two ifcopenshell model objects directly (legacy interface).

    New code should prefer ``compare_element_groups_for_clashes``.
    This wrapper is retained for backward compatibility with old routes and
    BCF exports.
    """

    def _build_group(model, slot_id: int, label: str) -> list:
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

# ---------------------------------------------------------------------------
# Project-Documents based clash detection
# ---------------------------------------------------------------------------

import tempfile
import zipfile
from pathlib import Path
from contextlib import ExitStack

from app.document_storage import download_document_to_temp, get_document
from app.exceptions import ValidationError, StorageError


def _extract_ifczip_to_temp(ifczip_path: str) -> str:
    """Extract the first .ifc member from an IFCZIP into a temporary .ifc file."""
    try:
        with zipfile.ZipFile(ifczip_path, "r") as zf:
            members = [m for m in zf.namelist() if m.lower().endswith(".ifc") and not m.endswith("/")]
            if not members:
                raise ValidationError("IFCZIP enthält keine .ifc-Datei.")
            member = members[0]
            fd, out_path = tempfile.mkstemp(prefix="bimpruef-ifczip-", suffix=".ifc")
            os.close(fd)
            with zf.open(member, "r") as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            return out_path
    except ValidationError:
        raise
    except Exception as exc:
        raise StorageError(f"IFCZIP konnte nicht entpackt werden: {exc}") from exc


def prepare_document_ifc_temp(account_id: str, project_id: str, document_id: str) -> tuple[dict, str, list[str]]:
    """
    Download one ProjectDocument and return a path that ifcopenshell can open.

    Returns (document_dict, ifc_path, temp_paths_to_delete).  The caller owns
    all returned temp paths and must remove them after processing.
    """
    doc = get_document(account_id, project_id, document_id)
    ext = str(doc.get("file_extension") or "").lower()
    if ext not in {".ifc", ".ifczip"}:
        raise ValidationError(f"'{doc.get('original_filename')}' ist keine IFC/IFCZIP-Datei.")

    downloaded_doc, raw_path = download_document_to_temp(account_id, project_id, document_id)
    temp_paths = [raw_path]
    if ext == ".ifczip":
        ifc_path = _extract_ifczip_to_temp(raw_path)
        temp_paths.append(ifc_path)
    else:
        ifc_path = raw_path
    return downloaded_doc, ifc_path, temp_paths


def load_elements_from_project_documents(account_id: str, project_id: str, document_ids: list[str]) -> tuple[list, list[str]]:
    """
    Load candidate IFC elements directly from Project Documents.

    Returns (elements, temp_paths).  The caller must delete temp_paths after
    clash calculation. Each element contains document metadata in addition to
    the existing clash/filter fields.
    """
    elements: list = []
    temp_paths: list[str] = []
    seen: set[str] = set()
    clean_ids: list[str] = []
    for document_id in document_ids or []:
        did = str(document_id or "").strip()
        if did and did not in seen:
            clean_ids.append(did)
            seen.add(did)
    if not clean_ids:
        raise ValidationError("Bitte mindestens ein IFC/IFCZIP-Dokument auswählen.")

    for index, document_id in enumerate(clean_ids, start=1):
        doc, ifc_path, paths = prepare_document_ifc_temp(account_id, project_id, document_id)
        temp_paths.extend(paths)
        label = doc.get("original_filename") or doc.get("safe_filename") or f"Dokument {index}"
        try:
            model = ifcopenshell.open(ifc_path)
            for elem in get_candidate_products(model):
                data = extract_element_data(elem, file_label=label)
                data["slot"] = index
                data["document_id"] = doc.get("document_id", "")
                data["document_name"] = label
                data["file_extension"] = doc.get("file_extension", "")
                data["folder_path"] = doc.get("folder_path", "")
                data["_ifc_element"] = elem
                elements.append(data)
        except Exception as exc:
            raise StorageError(f"IFC-Dokument '{label}' konnte nicht gelesen werden: {exc}") from exc
    return elements, temp_paths


def cleanup_temp_paths(paths: list[str]) -> None:
    for path in paths or []:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def compare_project_documents_for_clashes(
    account_id: str,
    project_id: str,
    document_ids_a: list[str],
    document_ids_b: list[str],
    filters_a: list | None = None,
    filters_b: list | None = None,
    tolerance: float = 0.0,
) -> dict:
    """Run the group-based clash algorithm using Project Documents as source."""
    temp_paths: list[str] = []
    try:
        elements_a, tmp_a = load_elements_from_project_documents(account_id, project_id, document_ids_a)
        temp_paths.extend(tmp_a)
        elements_b, tmp_b = load_elements_from_project_documents(account_id, project_id, document_ids_b)
        temp_paths.extend(tmp_b)

        group_a = apply_filters(elements_a, filters_a or [])
        group_b = apply_filters(elements_b, filters_b or [])
        clashes = compare_element_groups_for_clashes(group_a, group_b, tolerance=tolerance)

        for c in clashes:
            # Preserve document context for issue creation and later viewer session creation.
            # compare_element_groups_for_clashes copies these keys through below after the
            # small extension in its result builder.
            c.setdefault("source_type", "clash")

        return {
            "ok": True,
            "total_a": len(elements_a),
            "total_b": len(elements_b),
            "filtered_a": len(group_a),
            "filtered_b": len(group_b),
            "tolerance": tolerance,
            "clashes": clashes,
        }
    finally:
        cleanup_temp_paths(temp_paths)
