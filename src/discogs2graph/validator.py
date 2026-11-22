import json
from collections import Counter
from typing import Dict, Iterable, List, Optional


VALID_RELATION_TYPES = {
    "influence",
    "evolved_into",
    "derived_from",
    "precursor",
    "parallel",
    "associated_with",
    "scene",
    "regional_development",
    "artist_bridge",
}
VALID_STATUSES = {"verified", "proposed", "uncertain", "ambiguous"}


def validate_schema(data: Dict[str, object], expected: str,
                    report: Dict[str, List[object]]) -> None:
    version = data.get("schema_version")
    if version != expected:
        report["errors"].append({
            "code": "SCHEMA_VERSION",
            "message": "Inconsistent schema version",
            "expected": expected,
            "found": version,
        })


def validate_taxonomy(taxonomy: Dict[str, object],
                      report: Dict[str, List[object]]) -> None:
    validate_schema(taxonomy, "1.0", report)
    nodes = taxonomy.get("nodes")
    if not isinstance(nodes, list):
        report["errors"].append({"code": "MALFORMED_TAXONOMY",
                                 "message": "taxonomy nodes must be an array"})
        return

    counts = Counter(str(node.get("id")) for node in nodes if isinstance(node, dict))
    for node_id, count in counts.items():
        if count > 1:
            report["errors"].append({"code": "DUPLICATE_NODE_ID",
                                     "message": "Duplicate taxonomy node ID",
                                     "node_id": node_id})
    for node in nodes:
        if not isinstance(node, dict):
            report["errors"].append({"code": "MALFORMED_NODE",
                                     "message": "Taxonomy node must be an object"})
            continue
        if not node.get("id") or not isinstance(node["id"], str):
            report["errors"].append({"code": "MISSING_NODE_ID",
                                     "message": "Taxonomy node has no stable ID"})
        if not node.get("name"):
            report["errors"].append({"code": "MISSING_NODE_NAME",
                                     "message": "Taxonomy node has no name",
                                     "node_id": node.get("id")})


def validate_relations(relations_data: Dict[str, object],
                       taxonomy: Dict[str, object],
                       report: Dict[str, List[object]]) -> None:
    validate_schema(relations_data, "1.0", report)
    nodes = taxonomy.get("nodes", [])
    node_ids = {node["id"] for node in nodes if isinstance(node, dict) and node.get("id")}
    relations = relations_data.get("relations")
    if not isinstance(relations, list):
        report["errors"].append({"code": "MALFORMED_RELATIONS",
                                 "message": "relations must be an array"})
        return

    seen = set()
    for index, relation in enumerate(relations):
        if not isinstance(relation, dict):
            report["errors"].append({"code": "MALFORMED_RELATION",
                                     "message": "Relation must be an object",
                                     "index": index})
            continue
        source = relation.get("from")
        target = relation.get("to")
        relation_type = relation.get("type")
        status = relation.get("status")
        confidence = relation.get("confidence")
        strength = relation.get("strength")

        if not source or not target:
            report["errors"].append({"code": "MISSING_RELATION_ENDPOINT",
                                     "message": "Relation endpoint is missing",
                                     "index": index})
        if source not in node_ids:
            report["errors"].append({"code": "UNKNOWN_RELATION_ENDPOINT",
                                     "message": "Relation source node does not exist",
                                     "node_id": source})
        if target not in node_ids:
            report["errors"].append({"code": "UNKNOWN_RELATION_ENDPOINT",
                                     "message": "Relation target node does not exist",
                                     "node_id": target})
        if relation_type not in VALID_RELATION_TYPES:
            report["errors"].append({"code": "INVALID_RELATION_TYPE",
                                     "message": "Invalid historical relation type",
                                     "relation_type": relation_type})
        if status not in VALID_STATUSES:
            report["errors"].append({"code": "INVALID_RELATION_STATUS",
                                     "message": "Invalid historical relation status",
                                     "status": status})
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            report["errors"].append({"code": "INVALID_CONFIDENCE",
                                     "message": "Confidence must be between 0 and 1",
                                     "index": index})
        if not isinstance(strength, (int, float)) or not 0 <= strength <= 1:
            report["errors"].append({"code": "INVALID_STRENGTH",
                                     "message": "Strength must be between 0 and 1",
                                     "index": index})

        key = (source, target, relation_type)
        if key in seen:
            report["errors"].append({"code": "DUPLICATE_RELATION",
                                     "message": "Duplicate historical relation",
                                     "relation": key})
        seen.add(key)


def validate_collection(releases: Iterable[Dict[str, object]],
                        report: Dict[str, List[object]]) -> Dict[str, int]:
    release_counts = Counter()
    unmapped_count = 0
    ambiguous_count = 0

    for release in releases:
        release_id = release.get("release_id")
        artist = release.get("artist")
        title = release.get("title")
        if release_id is None or not str(release_id).strip():
            report["errors"].append({"code": "MISSING_RELEASE_ID",
                                     "message": "Release ID is missing"})
        else:
            release_counts[str(release_id)] += 1
        if not artist:
            report["errors"].append({"code": "MISSING_ARTIST",
                                     "message": "Artist is missing",
                                     "release_id": release_id})
        if not title:
            report["errors"].append({"code": "MISSING_TITLE",
                                     "message": "Title is missing",
                                     "release_id": release_id})

        mapping = release.get("canonical_mapping")
        if mapping:
            for field in ("genres", "styles"):
                unmapped = mapping.get(field, {}).get("unmapped", [])
                ambiguous = mapping.get(field, {}).get("ambiguous", [])
                unmapped_count += len(unmapped)
                ambiguous_count += len(ambiguous)

    for release_id, count in release_counts.items():
        if count > 1:
            report["errors"].append({"code": "DUPLICATE_RELEASE_ID",
                                     "message": "Duplicate release ID",
                                     "release_id": release_id,
                                     "count": count})

    return {"unmapped": unmapped_count, "ambiguous": ambiguous_count}


def validate_malformed_json(path: str, report: Dict[str, List[object]],
                            label: str) -> Optional[Dict[str, object]]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        report["errors"].append({"code": "FILE_NOT_FOUND",
                                 "message": "{} file was not found".format(label),
                                 "path": path})
    except json.JSONDecodeError as exc:
        report["errors"].append({"code": "MALFORMED_JSON",
                                 "message": "Malformed JSON in {}".format(label),
                                 "path": path,
                                 "line": exc.lineno,
                                 "column": exc.colno})
    return None
