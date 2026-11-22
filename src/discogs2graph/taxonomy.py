import json
import re
from typing import Dict, Iterable, List


SCHEMA_VERSION = "1.0"


def slugify(value: str) -> str:
    normalized = value.lower().strip()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9]+", "_", normalized)
    return normalized.strip("_")


def load_json(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Top-level JSON value must be an object")
    return data


def load_taxonomy(path: str) -> Dict[str, object]:
    data = load_json(path)
    if data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Inconsistent taxonomy schema version")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("taxonomy.json must contain a nodes array")
    return data


def node_index(nodes: Iterable[Dict[str, object]]) -> Dict[str, Dict[str, object]]:
    return {str(node["id"]): node for node in nodes if "id" in node}


def build_observed_taxonomy(source_values: Iterable[str]) -> Dict[str, object]:
    nodes: List[Dict[str, object]] = []
    seen = set()
    for value in source_values:
        node_id = slugify(value)
        if not node_id or node_id in seen:
            continue
        seen.add(node_id)
        nodes.append({
            "id": node_id,
            "name": value,
            "category": "discogs_value",
            "description": "Canonical node for the observed Discogs genre/style value.",
        })
    nodes.sort(key=lambda node: str(node["id"]))
    return {"schema_version": SCHEMA_VERSION, "nodes": nodes}
