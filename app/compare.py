import json
from collections import defaultdict

from app.extractors import extract_element_data, get_candidate_products


def normalize_data(value):
    """Normalisiert Werte für einen stabilen Vergleich (z. B. Dict-Reihenfolge)."""
    try:
        return json.loads(json.dumps(value, sort_keys=True, default=str))
    except Exception:
        return value


def build_model_index(model, file_label: str):
    """Erstellt einen Index aller Kandidaten-Elemente, indexiert nach GlobalId.

    Gibt zusätzlich ein Dict mit duplizierten GlobalIds zurück.
    """
    index = {}
    duplicates = defaultdict(list)

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


def compare_element_data(a: dict, b: dict):
    """Vergleicht zwei Element-Dicts und gibt ein Dict der Unterschiede zurück."""
    differences = {}

    fields_to_compare = [
        "type",
        "name",
        "object_type",
        "predefined_type",
    ]

    for field in fields_to_compare:
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


def compare_models(model1, model2, file_label_1="Modell 1", file_label_2="Modell 2"):
    """Vergleicht zwei IFC-Modelle nach GlobalId und gibt ein strukturiertes Ergebnis zurück.

    Returns:
        Dict mit folgenden Schlüsseln:
        - summary: Zusammenfassung der Zählungen
        - missing_in_model2: Elemente, die nur in Modell 1 vorhanden sind
        - new_in_model2: Elemente, die nur in Modell 2 vorhanden sind
        - changed: Elemente mit Unterschieden
        - unchanged: Identische Elemente
        - duplicates: Doppelte GlobalIds pro Modell
    """
    index1, duplicates1 = build_model_index(model1, file_label=file_label_1)
    index2, duplicates2 = build_model_index(model2, file_label=file_label_2)

    gids1 = set(index1.keys())
    gids2 = set(index2.keys())

    missing_in_model2 = []
    new_in_model2 = []
    changed = []
    unchanged = []

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
