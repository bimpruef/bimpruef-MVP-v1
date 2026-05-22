"""
storage.py – Dateispeicherung für BIMPruef

Unterstützt:
  - Multi-Slot-Sessions (bis zu MAX_FILES_PER_SESSION Dateien)
  - .ifc und .ifczip Dateien (IFCZIP wird automatisch entpackt)
  - Lokaler Cache für IFC-Verarbeitung
  - Persistente IFC-Speicherung in Cloudflare R2
  - Automatische Wiederherstellung lokaler Dateien aus R2 nach Server-Restart
  - Clash-Cache pro Session und Slot-Paar
  - Automatisches Cleanup alter temporärer Sessions (> 24 h)

Wichtig:
  Render-Dateisysteme sind nicht dauerhaft. Deshalb ist der lokale uploads/
  Ordner nur ein Cache. Die dauerhafte Quelle für IFC-Dateien ist Cloudflare R2.
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

from app.exceptions import StorageError, ValidationError

try:
    from app.r2_storage import (
        delete_file_from_r2,
        delete_prefix_from_r2,
        download_file_from_r2,
        object_exists_in_r2,
        r2_enabled,
        upload_file_to_r2,
    )
except Exception:
    # Die App soll auch lokal ohne R2-Konfiguration starten können.
    delete_file_from_r2 = None
    delete_prefix_from_r2 = None
    download_file_from_r2 = None
    object_exists_in_r2 = None
    r2_enabled = lambda: False
    upload_file_to_r2 = None


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads")

MAX_FILES_PER_SESSION = 10
SESSION_MAX_AGE_HOURS = 24
ALLOWED_EXTENSIONS = (".ifc", ".ifczip")

# UUID v4 produced by uuid.uuid4() is always lowercase hex with hyphens.
SESSION_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


# ─────────────────────────────────────────────────────────────────────────────
# Basis-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def ensure_uploads_dir() -> None:
    os.makedirs(UPLOADS_DIR, exist_ok=True)


def sanitize_filename(filename: str) -> str:
    filename = filename or "uploaded.ifc"
    filename = os.path.basename(filename)
    filename = filename.replace(" ", "_")
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", filename)
    return filename[:180] or "uploaded.ifc"


def validate_session_id(session_id: str) -> str:
    session_id = str(session_id or "").strip().lower()
    if not SESSION_ID_RE.fullmatch(session_id):
        raise ValidationError("Ungültige Session-ID.")
    return session_id


def validate_slot(slot: int) -> int:
    try:
        slot = int(slot)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Ungültiger Datei-Slot.") from exc

    if slot < 1 or slot > MAX_FILES_PER_SESSION:
        raise ValidationError(
            f"Slot muss zwischen 1 und {MAX_FILES_PER_SESSION} liegen."
        )

    return slot


def _safe_join(base_dir: str, *parts: str) -> str:
    base_abs = os.path.abspath(base_dir)
    path_abs = os.path.abspath(os.path.join(base_abs, *parts))

    if path_abs != base_abs and not path_abs.startswith(base_abs + os.sep):
        raise ValidationError("Unsicherer Dateipfad erkannt.")

    return path_abs


# ─────────────────────────────────────────────────────────────────────────────
# R2-Hilfsfunktionen
# ─────────────────────────────────────────────────────────────────────────────

def _r2_available() -> bool:
    try:
        return bool(r2_enabled())
    except Exception:
        return False


def _r2_model_key(session_id: str, slot: int) -> str:
    session_id = validate_session_id(session_id)
    slot = validate_slot(slot)
    return f"sessions/{session_id}/slots/{slot}/model.ifc"


def _r2_meta_key(session_id: str, slot: int) -> str:
    session_id = validate_session_id(session_id)
    slot = validate_slot(slot)
    return f"sessions/{session_id}/slots/{slot}/original_name.txt"


def _r2_session_prefix(session_id: str) -> str:
    """
    R2 prefix for all files belonging to one upload/project session.

    Everything below this prefix belongs to the session and must be removed
    during hard project deletion.
    """
    session_id = validate_session_id(session_id)
    return f"sessions/{session_id}/"


def _r2_object_exists(storage_key: str) -> bool:
    if not _r2_available() or object_exists_in_r2 is None:
        return False

    try:
        return object_exists_in_r2(storage_key)
    except Exception:
        return False


def _upload_to_r2_safely(
    local_path: str,
    storage_key: str,
    content_type: str = "application/octet-stream",
) -> None:
    """
    Upload to R2 when configured.

    Upload failure is treated as StorageError because persistence is now part
    of the expected storage contract.
    """
    if not _r2_available() or upload_file_to_r2 is None:
        return

    try:
        upload_file_to_r2(
            local_path=local_path,
            storage_key=storage_key,
            content_type=content_type,
        )
    except Exception as exc:
        raise StorageError(f"Upload nach Cloudflare R2 fehlgeschlagen: {exc}") from exc


def _download_from_r2_safely(storage_key: str, local_path: str) -> bool:
    """
    Download an object from R2 into local cache.

    Returns True when the file was restored, False when R2 is unavailable
    or the object does not exist.
    """
    if not _r2_available() or download_file_from_r2 is None:
        return False

    if not _r2_object_exists(storage_key):
        return False

    try:
        download_file_from_r2(
            storage_key=storage_key,
            local_path=local_path,
        )
        return True
    except Exception:
        return False


def _delete_from_r2_safely(storage_key: str, strict: bool = False) -> None:
    if not _r2_available() or delete_file_from_r2 is None:
        if strict:
            raise StorageError("Cloudflare R2 ist nicht konfiguriert; Dateien können nicht sicher gelöscht werden.")
        return

    try:
        delete_file_from_r2(storage_key)
    except Exception as exc:
        if strict:
            raise StorageError(f"R2-Objekt konnte nicht gelöscht werden: {storage_key} ({exc})") from exc


def _delete_r2_prefix_safely(prefix: str, strict: bool = False) -> None:
    """
    Delete every object below an R2 prefix.

    In strict mode, failure is fatal. This is important for project/account
    deletion because SQL deletion must not continue when R2 cleanup cannot be
    guaranteed.
    """
    if not _r2_available() or delete_prefix_from_r2 is None:
        if strict:
            raise StorageError(
                "Cloudflare R2 ist nicht konfiguriert; "
                "Projektdateien können nicht sicher vollständig gelöscht werden."
            )
        return

    try:
        delete_prefix_from_r2(prefix)
    except Exception as exc:
        if strict:
            raise StorageError(
                f"R2-Prefix konnte nicht vollständig gelöscht werden: {prefix} ({exc})"
            ) from exc


def _restore_slot_ifc_from_r2_if_missing(session_id: str, slot: int) -> bool:
    """
    Restore model_<slot>.ifc from R2 when the local cache file is missing.
    """
    ifc_path = _slot_ifc_path(session_id, slot)

    if os.path.exists(ifc_path):
        return True

    os.makedirs(get_session_dir(session_id), exist_ok=True)

    restored = _download_from_r2_safely(
        storage_key=_r2_model_key(session_id, slot),
        local_path=ifc_path,
    )

    if restored:
        _touch_session(session_id)

    return restored


def _restore_slot_meta_from_r2_if_missing(session_id: str, slot: int) -> bool:
    """
    Restore model_<slot>_name.txt from R2 when the local metadata file is missing.
    """
    meta_path = _slot_meta_path(session_id, slot)

    if os.path.exists(meta_path):
        return True

    os.makedirs(get_session_dir(session_id), exist_ok=True)

    restored = _download_from_r2_safely(
        storage_key=_r2_meta_key(session_id, slot),
        local_path=meta_path,
    )

    if restored:
        _touch_session(session_id)

    return restored


def _r2_session_has_any_model(session_id: str) -> bool:
    if not _r2_available():
        return False

    for slot in range(1, MAX_FILES_PER_SESSION + 1):
        if _r2_object_exists(_r2_model_key(session_id, slot)):
            return True

    return False


# ─────────────────────────────────────────────────────────────────────────────
# Session-Verwaltung
# ─────────────────────────────────────────────────────────────────────────────

def create_upload_session() -> str:
    ensure_uploads_dir()

    session_id = str(uuid.uuid4())  # always lowercase
    session_dir = os.path.join(UPLOADS_DIR, session_id)

    os.makedirs(session_dir, exist_ok=True)

    # Write a creation-timestamp marker so cleanup can use a reliable mtime.
    _touch_session(session_id)

    return session_id


def _touch_session(session_id: str) -> None:
    """
    Update the session's timestamp marker file to now.
    """
    os.makedirs(get_session_dir(session_id), exist_ok=True)

    marker = _safe_join(get_session_dir(session_id), ".created")

    with open(marker, "w", encoding="utf-8") as f:
        f.write(str(time.time()))


def _session_created_at(session_id: str) -> float:
    """
    Return the session creation timestamp.

    Falls back to the directory mtime when the marker file is absent
    (e.g. for sessions created before this version was deployed).
    """
    try:
        marker = _safe_join(get_session_dir(session_id), ".created")
        with open(marker, "r", encoding="utf-8") as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return os.path.getmtime(get_session_dir(session_id))


def get_session_dir(session_id: str) -> str:
    session_id = validate_session_id(session_id)
    return _safe_join(UPLOADS_DIR, session_id)


def session_exists(session_id: str) -> bool:
    """
    A session exists when either:
      - its local cache directory exists, or
      - at least one IFC model for the session exists in R2.
    """
    try:
        if os.path.isdir(get_session_dir(session_id)):
            return True

        return _r2_session_has_any_model(session_id)

    except ValidationError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Slot – interne Pfad-Helfer
# ─────────────────────────────────────────────────────────────────────────────

def _slot_ifc_path(session_id: str, slot: int) -> str:
    """
    Pfad zur extrahierten .ifc-Datei.
    Immer .ifc, auch wenn das Original .ifczip war.
    """
    slot = validate_slot(slot)
    return _safe_join(get_session_dir(session_id), f"model_{slot}.ifc")


def _slot_meta_path(session_id: str, slot: int) -> str:
    """
    Pfad zur Metadatei mit dem Originaldateinamen.
    """
    slot = validate_slot(slot)
    return _safe_join(get_session_dir(session_id), f"model_{slot}_name.txt")


# ─────────────────────────────────────────────────────────────────────────────
# Multi-Slot – öffentliche API
# ─────────────────────────────────────────────────────────────────────────────

def get_ifc_path(session_id: str, slot: int) -> str:
    """
    Return the local path for a slot IFC.

    If the local file is missing after a Render restart/redeploy, it is restored
    from Cloudflare R2 automatically.
    """
    ifc_path = _slot_ifc_path(session_id, slot)

    if os.path.exists(ifc_path):
        return ifc_path

    _restore_slot_ifc_from_r2_if_missing(session_id, slot)

    return ifc_path


def get_ifc_label(
    session_id: str,
    slot: int,
    fallback: Optional[str] = None,
) -> str:
    """
    Return the original uploaded filename for a slot.

    If the local metadata file is missing, it is restored from R2.
    """
    _restore_slot_meta_from_r2_if_missing(session_id, slot)

    meta_path = _slot_meta_path(session_id, slot)
    fb = fallback or f"model_{slot}.ifc"

    return read_original_filename(meta_path, fb)


def get_session_slots(session_id: str) -> List[int]:
    """
    Sortierte Liste der belegten Slot-Indizes (1-basiert).

    Slots are detected from local cache and from R2, so project files remain
    visible after the local Render filesystem has been reset.
    """
    slots = set()

    session_dir = get_session_dir(session_id)

    if os.path.isdir(session_dir):
        for i in range(1, MAX_FILES_PER_SESSION + 1):
            if os.path.exists(_safe_join(session_dir, f"model_{i}.ifc")):
                slots.add(i)

    if _r2_available():
        for i in range(1, MAX_FILES_PER_SESSION + 1):
            if _r2_object_exists(_r2_model_key(session_id, i)):
                slots.add(i)

    return sorted(slots)


def save_ifc_file(
    session_id: str,
    slot: int,
    file_bytes: bytes,
    original_name: str,
) -> None:
    """
    Speichert eine hochgeladene Datei in den Slot.

    - .ifc    → direkt gespeichert
    - .ifczip → erste .ifc-Datei wird aus dem ZIP extrahiert und gespeichert

    Zusätzlich:
    - Die extrahierte .ifc-Datei wird als lokaler Cache gespeichert.
    - Die extrahierte .ifc-Datei wird dauerhaft nach Cloudflare R2 hochgeladen.
    - Der Originaldateiname wird lokal und in R2 als Metadatei gespeichert.

    Raises:
        ValidationError: unbekanntes Format oder leeres ZIP.
        StorageError:    Schreibfehler oder R2-Uploadfehler.
    """
    slot = validate_slot(slot)

    session_dir = get_session_dir(session_id)
    os.makedirs(session_dir, exist_ok=True)

    ifc_path = _slot_ifc_path(session_id, slot)
    meta_path = _slot_meta_path(session_id, slot)

    clean_original_name = sanitize_filename(original_name)
    lower = clean_original_name.lower()

    if lower.endswith(".ifczip"):
        ifc_bytes = _extract_ifc_from_zip(file_bytes, clean_original_name)
    elif lower.endswith(".ifc"):
        ifc_bytes = file_bytes
    else:
        raise ValidationError(
            f"Nicht unterstütztes Format: '{original_name}'. "
            "Erlaubt: .ifc, .ifczip"
        )

    try:
        with open(ifc_path, "wb") as f:
            f.write(ifc_bytes)

        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(clean_original_name)

        _touch_session(session_id)

    except OSError as exc:
        raise StorageError(f"Datei konnte lokal nicht gespeichert werden: {exc}") from exc

    # Persist local IFC cache + metadata to Cloudflare R2.
    _upload_to_r2_safely(
        local_path=ifc_path,
        storage_key=_r2_model_key(session_id, slot),
        content_type="application/octet-stream",
    )

    _upload_to_r2_safely(
        local_path=meta_path,
        storage_key=_r2_meta_key(session_id, slot),
        content_type="text/plain; charset=utf-8",
    )


def _extract_ifc_from_zip(zip_bytes: bytes, original_name: str) -> bytes:
    """
    Extrahiert die erste .ifc-Datei aus einem IFCZIP-Archiv.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
            ifc_names = [
                name for name in zf.namelist()
                if name.lower().endswith(".ifc")
            ]

            if not ifc_names:
                all_files = ", ".join(zf.namelist()) or "(leer)"
                raise ValidationError(
                    f"Keine .ifc-Datei in '{original_name}' gefunden. "
                    f"Enthaltene Dateien: {all_files}"
                )

            return zf.read(ifc_names[0])

    except zipfile.BadZipFile as exc:
        raise ValidationError(
            f"'{original_name}' ist kein gültiges ZIP/IFCZIP-Archiv: {exc}"
        ) from exc


def remove_ifc_slot(session_id: str, slot: int) -> None:
    """
    Löscht eine Datei aus dem Slot:
      - lokale IFC-Datei
      - lokale Metadatei
      - R2-IFC-Datei
      - R2-Metadatei
      - lokale Clash-Caches, die diesen Slot betreffen
    """
    slot = validate_slot(slot)

    for path in (
        _slot_ifc_path(session_id, slot),
        _slot_meta_path(session_id, slot),
    ):
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    _delete_from_r2_safely(_r2_model_key(session_id, slot))
    _delete_from_r2_safely(_r2_meta_key(session_id, slot))

    # Remove all clash caches that involve this slot.
    # Cache filenames follow the exact pattern clash_{a}_{b}_{tol}.json
    # where a < b are slot numbers.
    session_dir = get_session_dir(session_id)

    if not os.path.isdir(session_dir):
        return

    cache_re = re.compile(
        r"^clash_(?P<a>\d+)_(?P<b>\d+)_[\d.]+\.json$"
    )

    for fname in list(os.listdir(session_dir)):
        match = cache_re.fullmatch(fname)

        if match and (
            int(match.group("a")) == slot
            or int(match.group("b")) == slot
        ):
            try:
                os.remove(_safe_join(session_dir, fname))
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Legacy-Kompatibilität (main.py / alte Routen)
# ─────────────────────────────────────────────────────────────────────────────

def get_session_file_paths(session_id: str) -> Dict[str, str]:
    """
    Legacy helper for routes that expect fixed model_1/model_2 paths.

    This function restores slot 1 and 2 from R2 when local cache files are
    missing.
    """
    session_dir = get_session_dir(session_id)

    get_ifc_path(session_id, 1)
    get_ifc_path(session_id, 2)
    get_ifc_label(session_id, 1)
    get_ifc_label(session_id, 2)

    return {
        "model_1": _safe_join(session_dir, "model_1.ifc"),
        "model_2": _safe_join(session_dir, "model_2.ifc"),
        "meta_1": _safe_join(session_dir, "model_1_name.txt"),
        "meta_2": _safe_join(session_dir, "model_2_name.txt"),
    }


def save_upload_file(file_obj, destination_path: str) -> None:
    with open(destination_path, "wb") as out_file:
        shutil.copyfileobj(file_obj, out_file)


def save_original_filename(meta_path: str, filename: str) -> None:
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

def _clash_cache_path(
    session_id: str,
    slot_a: int,
    slot_b: int,
    tolerance: float,
) -> str:
    # Slot-Paare normalisieren (kleinerer Slot immer zuerst)
    a, b = sorted([slot_a, slot_b])
    key = f"clash_{a}_{b}_{tolerance:.4f}.json"

    return _safe_join(get_session_dir(session_id), key)


def save_clash_cache(
    session_id: str,
    tolerance: float,
    clashes: list,
    slot_a: int = 1,
    slot_b: int = 2,
) -> None:
    path = _clash_cache_path(session_id, slot_a, slot_b, tolerance)

    try:
        os.makedirs(get_session_dir(session_id), exist_ok=True)

        with open(path, "w", encoding="utf-8") as f:
            json.dump(clashes, f, ensure_ascii=False, default=str)

    except OSError as exc:
        raise StorageError(
            f"Clash-Cache konnte nicht gespeichert werden: {exc}"
        ) from exc


def load_clash_cache(
    session_id: str,
    tolerance: float,
    slot_a: int = 1,
    slot_b: int = 2,
) -> Optional[list]:
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

def cleanup_old_sessions() -> None:
    """
    Delete old anonymous local upload sessions, but keep project sessions.

    R2 data is not deleted here. The local uploads directory is only a cache.
    Permanent deletion from R2 happens through remove_ifc_slot() or delete_session().
    """
    if not os.path.isdir(UPLOADS_DIR):
        return

    cutoff = time.time() - (SESSION_MAX_AGE_HOURS * 3600)

    protected_project_sessions: set[str] = set()

    try:
        from app.project_storage import get_all_project_session_ids
        protected_project_sessions = get_all_project_session_ids()
    except Exception:
        # Cleanup must never break app startup because the database is temporarily
        # unavailable. In that case we simply skip deleting project-protected IDs.
        protected_project_sessions = set()

    for entry in os.listdir(UPLOADS_DIR):
        # Only remove UUID-named upload sessions. Persistent account/project
        # metadata lives under uploads/accounts and must never be cleaned here.
        entry_id = entry.lower()

        if not SESSION_ID_RE.fullmatch(entry_id):
            continue

        if entry_id in protected_project_sessions or entry in protected_project_sessions:
            continue

        session_dir = _safe_join(UPLOADS_DIR, entry)

        if not os.path.isdir(session_dir):
            continue

        try:
            created_at = _session_created_at(entry)
        except Exception:
            created_at = os.path.getmtime(session_dir)

        if created_at < cutoff:
            shutil.rmtree(session_dir, ignore_errors=True)


def delete_session(session_id: str, strict: bool = False) -> None:
    """
    Löscht eine Session vollständig:

      - alle R2-Objekte unter sessions/{session_id}/
      - alle lokalen IFC-Dateien
      - lokale Metadaten
      - lokale Clash-Caches
      - sonstige lokale Session-Dateien

    strict=True wird für Projekt-/Account-Löschung verwendet. Dann wird bei
    R2-Fehlern abgebrochen, damit keine SQL-Datensätze gelöscht werden, während
    Dateien im R2-Speicher zurückbleiben.
    """
    session_id = validate_session_id(session_id)

    # Future-proof R2 cleanup:
    # Löscht nicht nur model.ifc/original_name.txt, sondern alles unter dem
    # Session-Prefix, z. B. spätere Exporte, Viewer-Daten, BCF-Dateien usw.
    _delete_r2_prefix_safely(
        _r2_session_prefix(session_id),
        strict=strict,
    )

    session_dir = get_session_dir(session_id)

    if os.path.isdir(session_dir):
        try:
            shutil.rmtree(session_dir)
        except OSError as exc:
            if strict:
                raise StorageError(
                    f"Lokale Session-Dateien konnten nicht gelöscht werden: {session_dir} ({exc})"
                ) from exc
            shutil.rmtree(session_dir, ignore_errors=True)
