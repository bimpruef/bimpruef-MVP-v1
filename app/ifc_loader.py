"""
ifc_loader.py – IFC model loading from upload sessions

Public functions:
  load_ifc_models_from_session(session_id)            – Legacy: slots 1 + 2
  load_ifc_models_by_slots(session_id, slot_a, slot_b) – any two slots
"""

import os

import ifcopenshell

from app.exceptions import NotFoundError
from app.storage import (
    get_ifc_label,
    get_ifc_path,
    session_exists,
)


def _open_slot(session_id: str, slot: int):
    """
    Open and return the ifcopenshell model for *slot*.

    Raises:
        NotFoundError: when the session or the slot file does not exist.
    """
    if not session_exists(session_id):
        raise NotFoundError(f"Upload-Session nicht gefunden: {session_id}")

    path = get_ifc_path(session_id, slot)
    if not os.path.exists(path):
        raise NotFoundError(
            f"Modell für Slot {slot} nicht gefunden. "
            "Bitte Datei erneut hochladen."
        )

    return ifcopenshell.open(path), path, get_ifc_label(session_id, slot)


def load_ifc_models_from_session(session_id: str) -> dict:
    """
    Legacy helper: load slots 1 and 2 of *session_id*.

    Still used by older routes in main.py.  New code should call
    ``load_ifc_models_by_slots`` directly.

    Returns a dict with keys:
      model_1, model_2, path_1, path_2, file_label_1, file_label_2
    """
    model_1, path_1, label_1 = _open_slot(session_id, 1)
    model_2, path_2, label_2 = _open_slot(session_id, 2)

    return {
        "model_1": model_1,
        "model_2": model_2,
        "path_1": path_1,
        "path_2": path_2,
        "file_label_1": label_1,
        "file_label_2": label_2,
    }


def load_ifc_models_by_slots(
    session_id: str, slot_a: int, slot_b: int
) -> dict:
    """
    Load two arbitrary slots for clash detection and comparison operations.

    Stored files are always .ifc (IFCZIP is extracted at upload time), so
    no format branching is needed here.

    Returns a dict with keys:
      model_1, model_2, path_1, path_2, file_label_1, file_label_2
    """
    model_a, path_a, label_a = _open_slot(session_id, slot_a)
    model_b, path_b, label_b = _open_slot(session_id, slot_b)

    return {
        "model_1": model_a,
        "model_2": model_b,
        "path_1": path_a,
        "path_2": path_b,
        "file_label_1": label_a,
        "file_label_2": label_b,
    }
