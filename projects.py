"""
ifc_loader.py – IFC-Modelle aus Sessions laden

Funktionen:
  load_ifc_models_from_session(session_id)         → Legacy: Slot 1 + 2
  load_ifc_models_by_slots(session_id, slot_a, slot_b) → beliebige Slots
"""

import os
import ifcopenshell

from app.storage import (
    get_session_file_paths,
    get_ifc_path,
    get_ifc_label,
    read_original_filename,
    session_exists,
)


def load_ifc_models_from_session(session_id: str):
    """
    Legacy-Funktion: lädt Slot 1 und Slot 2.
    Wird von den alten Routen in main.py verwendet.
    """
    if not session_exists(session_id):
        raise FileNotFoundError(f"Upload-Session nicht gefunden: {session_id}")

    paths  = get_session_file_paths(session_id)
    path_1 = paths["model_1"]
    path_2 = paths["model_2"]

    if not os.path.exists(path_1):
        raise FileNotFoundError(f"Modell-1-Datei nicht gefunden: {path_1}")
    if not os.path.exists(path_2):
        raise FileNotFoundError(f"Modell-2-Datei nicht gefunden: {path_2}")

    model_1 = ifcopenshell.open(path_1)
    model_2 = ifcopenshell.open(path_2)

    file_label_1 = read_original_filename(paths["meta_1"], "model_1.ifc")
    file_label_2 = read_original_filename(paths["meta_2"], "model_2.ifc")

    return {
        "model_1":      model_1,
        "model_2":      model_2,
        "path_1":       path_1,
        "path_2":       path_2,
        "file_label_1": file_label_1,
        "file_label_2": file_label_2,
    }


def load_ifc_models_by_slots(session_id: str, slot_a: int, slot_b: int):
    """
    Lädt zwei beliebige Slots für Clash- und Vergleichs-Operationen.
    Die gespeicherten Dateien sind immer .ifc (IFCZIP wurde bereits beim
    Upload entpackt), daher ist kein Sonderfall nötig.
    """
    if not session_exists(session_id):
        raise FileNotFoundError(f"Upload-Session nicht gefunden: {session_id}")

    path_a = get_ifc_path(session_id, slot_a)
    path_b = get_ifc_path(session_id, slot_b)

    if not os.path.exists(path_a):
        raise FileNotFoundError(
            f"Modell für Slot {slot_a} nicht gefunden. "
            "Bitte Datei erneut hochladen."
        )
    if not os.path.exists(path_b):
        raise FileNotFoundError(
            f"Modell für Slot {slot_b} nicht gefunden. "
            "Bitte Datei erneut hochladen."
        )

    model_a = ifcopenshell.open(path_a)
    model_b = ifcopenshell.open(path_b)

    label_a = get_ifc_label(session_id, slot_a)
    label_b = get_ifc_label(session_id, slot_b)

    return {
        "model_1":      model_a,
        "model_2":      model_b,
        "path_1":       path_a,
        "path_2":       path_b,
        "file_label_1": label_a,
        "file_label_2": label_b,
    }
