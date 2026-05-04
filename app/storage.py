"""
storage.py – Dateispeicherung für BIMPruef

Unterstützt:
  - Multi-Slot-Sessions (bis zu MAX_FILES_PER_SESSION Dateien)
  - .ifc und .ifczip Dateien (IFCZIP wird automatisch entpackt)
  - Clash-Cache pro Session und Slot-Paar
  - Automatisches Cleanup alter Sessions (> 24 h)
"""

import io
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from typing import Dict, List, Optional

BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")

MAX_FILES_PER_SESSION = 10
SESSION_MAX_AGE_HOURS = 24
ALLOWED_EXTENSIONS    = (".ifc", ".ifczip")
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")



# ─────────────────────────────────────────────────────────────────────────────
# Basis-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def ensure_uploads_dir():
    os.makedirs(UPLOADS_DIR, exist_ok=True)


def sanitize_filename(filename: str) -> str:
    filename = filename or "uploaded.ifc"
    filename = os.path.basename(filename)
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return filename[:180] or "uploaded.ifc"


def validate_session_id(session_id: str) -> str:
    session_id = str(session_id or "").strip()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValueError("Ungültige Session-ID.")
    return session_id


def validate_slot(slot: int) -> int:
    try:
        slot = int(slot)
    except (TypeError, ValueError) as exc:
        raise ValueError("Ungültiger Datei-Slot.") from exc
    if slot < 1 or slot > MAX_FILES_PER_SESSION:
        raise ValueError(f"Slot muss zwischen 1 und {MAX_FILES_PER_SESSION} liegen.")
    return slot


def _safe_join(base_dir: str, *parts: str) -> str:
    base_abs = os.path.abspath(base_dir)
    path_abs = os.path.abspath(os.path.join(base_abs, *parts))
    if path_abs != base_abs and not path_abs.startswith(base_abs + os.sep):
        raise ValueError("Unsicherer Dateipfad erkannt.")
    return path_abs


# ─────────────────────────────────────────────────────────────────────────────
# Session-Verwaltung
# ─────────────────────────────────────────────────────────────────────────────

def create_upload_session() -> str:
    ensure_uploads_dir()
    session_id  = str(uuid.uuid4())
    session_dir = os.path.join(UPLOADS_DIR, session_id)
    os.makedirs(session_dir, exist_ok=True)
    return session_id


def get_session_dir(session_id: str) -> str:
    session_id = validate_session_id(session_id)
    return _safe_join(UPLOADS_DIR, session_id)


def session_exists(session_id: str) -> bool:
    try:
        return os.path.isdir(get_session_dir(session_id))
    except ValueError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Slot – interne Pfad-Helfer
# ─────────────────────────────────────────────────────────────────────────────

def _slot_ifc_path(session_id: str, slot: int) -> str:
    """Pfad zur extrahierten .ifc-Datei (immer .ifc, auch wenn Original .ifczip war)."""
    slot = validate_slot(slot)
    return _safe_join(get_session_dir(session_id), f"model_{slot}.ifc")


def _slot_meta_path(session_id: str, slot: int) -> str:
    """Pfad zur Metadatei mit dem Originaldateinamen."""
    slot = validate_slot(slot)
    return _safe_join(get_session_dir(session_id), f"model_{slot}_name.txt")


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Slot – öffentliche API
# ─────────────────────────────────────────────────────────────────────────────

def get_ifc_path(session_id: str, slot: int) -> str:
    return _slot_ifc_path(session_id, slot)


def get_ifc_label(session_id: str, slot: int, fallback: Optional[str] = None) -> str:
    meta_path = _slot_meta_path(session_id, slot)
    fb        = fallback or f"model_{slot}.ifc"
    return read_original_filename(meta_path, fb)


def get_session_slots(session_id: str) -> List[int]:
    """Sortierte Liste der belegten Slot-Indizes (1-basiert)."""
    session_dir = get_session_dir(session_id)
    slots = []
    for i in range(1, MAX_FILES_PER_SESSION + 1):
        if os.path.exists(_safe_join(session_dir, f"model_{i}.ifc")):
            slots.append(i)
    return slots


def save_ifc_file(session_id: str, slot: int, file_bytes: bytes, original_name: str):
    """
    Speichert eine hochgeladene Datei in den Slot.
    - .ifc   → direkt gespeichert
    - .ifczip → erste .ifc-Datei wird aus dem ZIP extrahiert und gespeichert
    Wirft ValueError bei unbekanntem Format oder leerem ZIP.
    """
    ifc_path  = _slot_ifc_path(session_id, slot)
    meta_path = _slot_meta_path(session_id, slot)
    lower     = (original_name or "").lower()

    if lower.endswith(".ifczip"):
        ifc_bytes = _extract_ifc_from_zip(file_bytes, original_name)
    elif lower.endswith(".ifc"):
        ifc_bytes = file_bytes
    else:
        raise ValueError(f"Nicht unterstütztes Format: '{original_name}'. Erlaubt: .ifc, .ifczip")

    with open(ifc_path, "wb") as f:
        f.write(ifc_bytes)

    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(sanitize_filename(original_name))


def _extract_ifc_from_zip(zip_bytes: bytes, original_name: str) -> bytes:
    """Extrahiert die erste .ifc-Datei aus einem IFCZIP-Archiv."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            ifc_names = [n for n in zf.namelist() if n.lower().endswith(".ifc")]
            if not ifc_names:
                all_files = ", ".join(zf.namelist()) or "(leer)"
                raise ValueError(
                    f"Keine .ifc-Datei in '{original_name}' gefunden. "
                    f"Enthaltene Dateien: {all_files}"
                )
            return zf.read(ifc_names[0])
    except zipfile.BadZipFile as exc:
        raise ValueError(
            f"'{original_name}' ist kein gültiges ZIP/IFCZIP-Archiv: {exc}"
        ) from exc


def remove_ifc_slot(session_id: str, slot: int):
    """Löscht eine Datei aus dem Slot (IFC + Metadatei)."""
    for path in (_slot_ifc_path(session_id, slot), _slot_meta_path(session_id, slot)):
        if os.path.exists(path):
            os.remove(path)
    # zugehörige Clash-Caches löschen
    session_dir = get_session_dir(session_id)
    if not os.path.isdir(session_dir):
        return
    for fname in list(os.listdir(session_dir)):
        if fname.startswith(f"clash_{slot}_") or f"_{slot}_" in fname:
            try:
                os.remove(_safe_join(session_dir, fname))
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Kompatibilität (main.py / alte Routen)
# ─────────────────────────────────────────────────────────────────────────────

def get_session_file_paths(session_id: str) -> Dict[str, str]:
    session_dir = get_session_dir(session_id)
    return {
        "model_1": _safe_join(session_dir, "model_1.ifc"),
        "model_2": _safe_join(session_dir, "model_2.ifc"),
        "meta_1":  _safe_join(session_dir, "model_1_name.txt"),
        "meta_2":  _safe_join(session_dir, "model_2_name.txt"),
    }


def save_upload_file(file_obj, destination_path: str):
    with open(destination_path, "wb") as out_file:
        shutil.copyfileobj(file_obj, out_file)


def save_original_filename(meta_path: str, filename: str):
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(filename or "")


def read_original_filename(meta_path: str, fallback: str) -> str:
    if not os.path.exists(meta_path):
        return fallback
    with open(meta_path, "r", encoding="utf-8") as f:
        value = f.read().strip()
    return value or fallback


# ─────────────────────────────────────────────────────────────────────────────
# Clash-Cache  (pro Session + Slot-Paar + Toleranz)
# ─────────────────────────────────────────────────────────────────────────────

def _clash_cache_path(session_id: str, slot_a: int, slot_b: int, tolerance: float) -> str:
    # Slot-Paare normalisieren (kleinerer Slot immer zuerst)
    a, b = sorted([slot_a, slot_b])
    key  = f"clash_{a}_{b}_{tolerance:.4f}.json"
    return _safe_join(get_session_dir(session_id), key)


def save_clash_cache(session_id: str, tolerance: float, clashes: list,
                     slot_a: int = 1, slot_b: int = 2):
    path = _clash_cache_path(session_id, slot_a, slot_b, tolerance)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(clashes, f, ensure_ascii=False, default=str)


def load_clash_cache(session_id: str, tolerance: float,
                     slot_a: int = 1, slot_b: int = 2) -> Optional[list]:
    path = _clash_cache_path(session_id, slot_a, slot_b, tolerance)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Session-Cleanup
# ─────────────────────────────────────────────────────────────────────────────

def cleanup_old_sessions():
    if not os.path.isdir(UPLOADS_DIR):
        return
    cutoff = time.time() - (SESSION_MAX_AGE_HOURS * 3600)
    for session_id in os.listdir(UPLOADS_DIR):
        # Only remove UUID-named upload sessions. Persistent account/project
        # metadata lives under uploads/accounts and must never be cleaned here.
        if not SESSION_ID_RE.fullmatch(session_id):
            continue
        session_dir = _safe_join(UPLOADS_DIR, session_id)
        if not os.path.isdir(session_dir):
            continue
        if os.path.getmtime(session_dir) < cutoff:
            shutil.rmtree(session_dir, ignore_errors=True)


def delete_session(session_id: str):
    """Löscht eine Session und alle zugehörigen Daten sofort vom Dateisystem."""
    session_dir = get_session_dir(session_id)
    if os.path.isdir(session_dir):
        shutil.rmtree(session_dir, ignore_errors=True)
