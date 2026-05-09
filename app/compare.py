"""
compare.py – BIMPruef IFC model comparison

Compares two IFC models element-by-element using GlobalId as the stable key.
Returns a structured result that categorises elements as:
  missing, new, changed, unchanged, or duplicated.
"""

import json
import logging
from collections import defaultdict

from app.extractors import extract_element_data, get_candidate_products

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data normalisation
# ---------------------------------------------------------------------------


def normalize_data(value):
    """
    Normalise *value* for a stable, order-independent comparison.

    Serialises to JSON with sorted keys so that dict field order does not
    affect equality checks.  When serialisation fails (e.g. a value is not
    JSON-serialisable), the original value is returned unchanged and a
    warning is emitted so the issue is visible during development.
    """
    try:
        return json.loads(json.dumps(value, sort_keys=True, default=str))
    except Exception as exc:  # pragma: no cover
        logger.warning("normalize_data: could not normalise %r – %s", value, exc)
        return value


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------


def build_model_index(model, file_label: str) -> tuple[dict, dict]:
    """
    Build a GlobalId → element-dict index for all candidate elements.

    Also returns a dict of GlobalIds that appear more than once in the model
    (duplicates should be rare but possible in malformed IFC files).

    Returns:
        (index, duplicates) where both are dicts keyed by GlobalId.
    """
    index: dict = {}
    duplicates: dict = defaultdict(list)

    for element in get_candidate_products(model):
        data = extract_element_data(element, file_label=file_label)
        gid = data.get("global_id", "")
        if not gid:
            continue

        if gid in index:
            duplicates[gid].append(data)
        else:
            index[gid] = data

    return index, duplicates


# ---------------------------------------------------------------------------
# Element-level comparison
# ---------------------------------------------------------------------------


def compare_element_data(a: dict, b: dict) -> dict:
    """
    Return a dict of field-level differences between two element dicts.

    An empty dict means the elements are identical for the compared fields.
    """
    differences: dict = {}

    for field in ("type", "name", "object_type", "predefined_type"):
        if normalize_data(a.get(field)) != normalize_data(b.get(field)):
            differences[field] = {
                "model_1": a.get(field),
                "model_2": b.get(field),
            }

    psets_a = normalize_data(a.get("psets", {}))
    psets_b = normalize_data(b.get("psets", {}))
    if psets_a != psets_b:
        differences["psets"] = {
            "model_1": psets_a,
            "model_2": psets_b,
        }

    return differences


# ---------------------------------------------------------------------------
# Model-level comparison
# ---------------------------------------------------------------------------


def compare_models(
    model1,
    model2,
    file_label_1: str = "Modell 1",
    file_label_2: str = "Modell 2",
) -> dict:
    """
    Compare two IFC models by GlobalId and return a structured result.

    Returns:
        A dict with the following keys:

        summary         – element counts for each category
        missing_in_model2 – elements present only in model 1
        new_in_model2   – elements present only in model 2
        changed         – elements present in both with at least one difference
        unchanged       – elements present in both that are identical
        duplicates      – duplicate GlobalIds per model
    """
    index1, duplicates1 = build_model_index(model1, file_label=file_label_1)
    index2, duplicates2 = build_model_index(model2, file_label=file_label_2)

    gids1 = set(index1)
    gids2 = set(index2)

    missing_in_model2: list = []
    new_in_model2: list = []
    changed: list = []
    unchanged: list = []

    for gid in sorted(gids1 - gids2):
        missing_in_model2.append(index1[gid])

    for gid in sorted(gids2 - gids1):
        new_in_model2.append(index2[gid])

    for gid in sorted(gids1 & gids2):
        a = index1[gid]
        b = index2[gid]
        diffs = compare_element_data(a, b)

        if diffs:
            changed.append(
                {
                    "global_id": gid,
                    "model_1": a,
                    "model_2": b,
                    "differences": diffs,
                }
            )
        else:
            unchanged.append(a)

    return {
        "summary": {
            "model_1_count": len(index1),
            "model_2_count": len(index2),
            "missing_in_model2": len(missing_in_model2),
            "new_in_model2": len(new_in_model2),
            "changed": len(changed),
            "unchanged": len(unchanged),
            "duplicates_in_model1": len(duplicates1),
            "duplicates_in_model2": len(duplicates2),
        },
        "missing_in_model2": missing_in_model2,
        "new_in_model2": new_in_model2,
        "changed": changed,
        "unchanged": unchanged,
        "duplicates": {
            "model_1": dict(duplicates1),
            "model_2": dict(duplicates2),
        },
    }
