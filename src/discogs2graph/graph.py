from typing import Dict, Iterable, List


def build_collection_coverage(releases: Iterable[Dict[str, object]],
                              all_node_ids: Iterable[str]) -> Dict[str, object]:
    represented = set()
    by_node: Dict[str, List[str]] = {}

    for release in releases:
        mapping = release["canonical_mapping"]
        for node_id in mapping["canonical_node_ids"]:
            represented.add(node_id)
            by_node.setdefault(node_id, []).append(str(release["release_id"]))

    all_ids = set(all_node_ids)
    return {
        "represented_node_ids": sorted(represented),
        "not_represented_node_ids": sorted(all_ids - represented),
        "release_ids_by_node": {
            node_id: sorted(set(release_ids))
            for node_id, release_ids in sorted(by_node.items())
        },
        "represented_count": len(represented),
        "not_represented_count": len(all_ids - represented),
    }
