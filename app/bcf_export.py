"""
bcf_export.py – BCF 2.1 export for BIMPruef clash results

Produces a BCF 2.1-compliant ZIP archive from a list of clash dicts.
"""

import io
import re
import uuid
import zipfile
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring

# IFC GlobalIds are exactly 22 characters of base64-like characters.
# We validate against this pattern before embedding in XML attributes to
# prevent attribute-injection from malformed IFC data.
_IFC_GUID_RE = re.compile(r"^[0-9A-Za-z_$]{22}$")


def _safe_guid(value: str) -> str:
    """Return *value* when it looks like a valid IFC GlobalId, else ''."""
    value = str(value or "")
    return value if _IFC_GUID_RE.fullmatch(value) else ""


def _safe_text(value: str) -> str:
    """Return *value* as a safe plain-text string (no XML special chars needed)."""
    return str(value or "")


def _xml_bytes(element: Element) -> bytes:
    return tostring(element, encoding="utf-8", xml_declaration=True)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# BCF building blocks
# ---------------------------------------------------------------------------


def build_bcf_version_xml() -> bytes:
    root = Element("Version", {"VersionId": "2.1"})
    return _xml_bytes(root)


def build_bcf_project_xml(
    project_id: str, project_name: str = "IFC Clash Report"
) -> bytes:
    root = Element("Project", {"ProjectId": _safe_text(project_id)})
    SubElement(root, "Name").text = _safe_text(project_name)
    return _xml_bytes(root)


def build_markup_xml(
    topic_guid: str, topic_index: int, clash: dict
) -> bytes:
    """Build the markup.bcf XML for a single clash topic."""
    root = Element("Markup")

    header = SubElement(root, "Header")
    file_node = SubElement(
        header, "File", {"IfcProject": "", "isExternal": "false"}
    )
    SubElement(file_node, "Filename").text = "uploaded_models.ifc"

    topic = SubElement(
        root,
        "Topic",
        {"Guid": _safe_text(topic_guid), "TopicType": "Clash"},
    )
    SubElement(topic, "Title").text = f"Clash #{topic_index:04d}"
    SubElement(topic, "CreationDate").text = _utc_now_iso()
    SubElement(topic, "CreationAuthor").text = "BIMPruef"
    SubElement(topic, "Priority").text = "Normal"
    SubElement(topic, "Stage").text = "New"
    SubElement(topic, "Description").text = (
        f"Clash #{topic_index:04d}\n"
        f"Element 1 GlobalId: {_safe_guid(clash.get('global_id_1', ''))}\n"
        f"Element 1 Klasse: {_safe_text(clash.get('type_1', ''))}\n"
        f"Element 2 GlobalId: {_safe_guid(clash.get('global_id_2', ''))}\n"
        f"Element 2 Klasse: {_safe_text(clash.get('type_2', ''))}"
    )

    labels = SubElement(topic, "Labels")
    SubElement(labels, "Label").text = (
        _safe_text(clash.get("type_1")) or "UnknownType"
    )
    SubElement(labels, "Label").text = (
        _safe_text(clash.get("type_2")) or "UnknownType"
    )

    viewpoints = SubElement(root, "Viewpoints")
    viewpoint = SubElement(viewpoints, "ViewPoint", {"Guid": str(uuid.uuid4())})
    SubElement(viewpoint, "Viewpoint").text = "viewpoint.bcfv"

    return _xml_bytes(root)


def build_viewpoint_xml(clash: dict) -> bytes:
    """Build the viewpoint.bcfv XML for a single clash topic."""
    root = Element("VisualizationInfo", {"Guid": str(uuid.uuid4())})

    components = SubElement(root, "Components")
    selection = SubElement(components, "Selection")

    comp1 = SubElement(
        selection,
        "Component",
        {"IfcGuid": _safe_guid(clash.get("global_id_1", ""))},
    )
    SubElement(comp1, "OriginatingSystem").text = (
        _safe_text(clash.get("type_1")) or "UnknownType"
    )

    comp2 = SubElement(
        selection,
        "Component",
        {"IfcGuid": _safe_guid(clash.get("global_id_2", ""))},
    )
    SubElement(comp2, "OriginatingSystem").text = (
        _safe_text(clash.get("type_2")) or "UnknownType"
    )

    camera = SubElement(root, "OrthogonalCamera")
    view_point = SubElement(camera, "CameraViewPoint")
    SubElement(view_point, "X").text = "0"
    SubElement(view_point, "Y").text = "0"
    SubElement(view_point, "Z").text = "10"

    direction = SubElement(camera, "CameraDirection")
    SubElement(direction, "X").text = "0"
    SubElement(direction, "Y").text = "0"
    SubElement(direction, "Z").text = "-1"

    up = SubElement(camera, "CameraUpVector")
    SubElement(up, "X").text = "0"
    SubElement(up, "Y").text = "1"
    SubElement(up, "Z").text = "0"

    SubElement(camera, "ViewToWorldScale").text = "1"

    return _xml_bytes(root)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_bcf_zip_from_clashes(
    clashes: list, project_name: str = "IFC Clash Report"
) -> bytes:
    """
    Build a BCF 2.1-compliant ZIP archive from *clashes*.

    Args:
        clashes:      List of clash dicts as produced by
                      ``clash.compare_element_groups_for_clashes``.
        project_name: Human-readable name embedded in the BCF project file.

    Returns:
        Raw bytes of the finished BCF ZIP archive.
    """
    project_id = str(uuid.uuid4())
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bcf.version", build_bcf_version_xml())
        zf.writestr("project.bcfp", build_bcf_project_xml(project_id, project_name))

        for index, clash in enumerate(clashes, start=1):
            topic_guid = str(uuid.uuid4())
            zf.writestr(
                f"{topic_guid}/markup.bcf",
                build_markup_xml(topic_guid, index, clash),
            )
            zf.writestr(
                f"{topic_guid}/viewpoint.bcfv",
                build_viewpoint_xml(clash),
            )

    buffer.seek(0)
    return buffer.read()
