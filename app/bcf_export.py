import io
import uuid
import zipfile
from datetime import datetime, timezone
from xml.etree.ElementTree import Element, SubElement, tostring


def _xml_bytes(element) -> bytes:
    return tostring(element, encoding="utf-8", xml_declaration=True)


def _utc_now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_bcf_version_xml():
    root = Element("Version", {"VersionId": "2.1"})
    return _xml_bytes(root)


def build_bcf_project_xml(project_id: str, project_name: str = "IFC Clash Report"):
    root = Element("Project", {"ProjectId": project_id})
    name_node = SubElement(root, "Name")
    name_node.text = project_name
    return _xml_bytes(root)


def build_markup_xml(topic_guid: str, topic_index: int, clash: dict):
    """Erstellt die markup.bcf-XML für einen einzelnen Clash-Topic."""
    root = Element("Markup")

    header = SubElement(root, "Header")
    file_node = SubElement(header, "File", {"IfcProject": "", "isExternal": "false"})
    filename = SubElement(file_node, "Filename")
    filename.text = "uploaded_models.ifc"

    topic = SubElement(root, "Topic", {"Guid": topic_guid, "TopicType": "Clash"})
    title = SubElement(topic, "Title")
    title.text = f"Clash #{topic_index:04d}"

    creation_date = SubElement(topic, "CreationDate")
    creation_date.text = _utc_now_iso()

    creation_author = SubElement(topic, "CreationAuthor")
    creation_author.text = "BIMPruef FastAPI"

    priority = SubElement(topic, "Priority")
    priority.text = "Normal"

    stage = SubElement(topic, "Stage")
    stage.text = "New"

    description = SubElement(topic, "Description")
    description.text = (
        f"Clash #{topic_index:04d}\n"
        f"Element 1 GlobalId: {clash.get('global_id_1', '')}\n"
        f"Element 1 Klasse: {clash.get('type_1', '')}\n"
        f"Element 2 GlobalId: {clash.get('global_id_2', '')}\n"
        f"Element 2 Klasse: {clash.get('type_2', '')}"
    )

    labels = SubElement(topic, "Labels")
    label_1 = SubElement(labels, "Label")
    label_1.text = clash.get("type_1", "") or "UnknownType"
    label_2 = SubElement(labels, "Label")
    label_2.text = clash.get("type_2", "") or "UnknownType"

    viewpoints = SubElement(root, "Viewpoints")
    viewpoint = SubElement(viewpoints, "ViewPoint", {"Guid": str(uuid.uuid4())})
    viewpoint_ref = SubElement(viewpoint, "Viewpoint")
    viewpoint_ref.text = "viewpoint.bcfv"

    return _xml_bytes(root)


def build_viewpoint_xml(clash: dict):
    """Erstellt die viewpoint.bcfv-XML für einen einzelnen Clash-Topic."""
    root = Element("VisualizationInfo", {"Guid": str(uuid.uuid4())})

    components = SubElement(root, "Components")
    selection = SubElement(components, "Selection")

    comp1 = SubElement(selection, "Component", {"IfcGuid": clash.get("global_id_1", "")})
    origin1 = SubElement(comp1, "OriginatingSystem")
    origin1.text = clash.get("type_1", "") or "UnknownType"

    comp2 = SubElement(selection, "Component", {"IfcGuid": clash.get("global_id_2", "")})
    origin2 = SubElement(comp2, "OriginatingSystem")
    origin2.text = clash.get("type_2", "") or "UnknownType"

    camera = SubElement(root, "OrthogonalCamera")
    camera_view_point = SubElement(camera, "CameraViewPoint")
    SubElement(camera_view_point, "X").text = "0"
    SubElement(camera_view_point, "Y").text = "0"
    SubElement(camera_view_point, "Z").text = "10"

    camera_direction = SubElement(camera, "CameraDirection")
    SubElement(camera_direction, "X").text = "0"
    SubElement(camera_direction, "Y").text = "0"
    SubElement(camera_direction, "Z").text = "-1"

    camera_up = SubElement(camera, "CameraUpVector")
    SubElement(camera_up, "X").text = "0"
    SubElement(camera_up, "Y").text = "1"
    SubElement(camera_up, "Z").text = "0"

    view_to_world_scale = SubElement(camera, "ViewToWorldScale")
    view_to_world_scale.text = "1"

    return _xml_bytes(root)


def create_bcf_zip_from_clashes(clashes, project_name: str = "IFC Clash Report") -> bytes:
    """Erstellt ein BCF 2.1-konformes ZIP-Archiv aus einer Liste von Clashes.

    Args:
        clashes: Liste von Clash-Dicts (aus compare_models_for_clashes).
        project_name: Name des BCF-Projekts.

    Returns:
        Bytes des fertigen BCF-ZIP-Archivs.
    """
    project_id = str(uuid.uuid4())
    memory_file = io.BytesIO()

    with zipfile.ZipFile(memory_file, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bcf.version", build_bcf_version_xml())
        zf.writestr("project.bcfp", build_bcf_project_xml(project_id, project_name))

        for index, clash in enumerate(clashes, start=1):
            topic_guid = str(uuid.uuid4())
            folder_name = topic_guid

            zf.writestr(
                f"{folder_name}/markup.bcf",
                build_markup_xml(topic_guid, index, clash),
            )
            zf.writestr(
                f"{folder_name}/viewpoint.bcfv",
                build_viewpoint_xml(clash),
            )

    memory_file.seek(0)
    return memory_file.read()
