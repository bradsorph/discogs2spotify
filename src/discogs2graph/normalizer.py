from typing import Dict, Iterable, List

from .taxonomy import slugify


def normalize_values(values: Iterable[str], source_field: str,
                     taxonomy_ids: set) -> Dict[str, object]:
    node_ids: List[str] = []
    unmapped: List[str] = []
    ambiguous: List[Dict[str, object]] = []

    for raw_value in values:
        value = raw_value.strip()
        if not value:
            continue
        node_id = slugify(value)
        if node_id in taxonomy_ids:
            if node_id not in node_ids:
                node_ids.append(node_id)
        else:
            unmapped.append(value)

    return {
        "node_ids": node_ids,
        "unmapped": sorted(set(unmapped)),
        "ambiguous": ambiguous,
    }


def normalize_release(release: Dict[str, object],
                     taxonomy_ids: set) -> Dict[str, object]:
    discogs = release["discogs"]
    genres = normalize_values(discogs["genres"], "discogs_genre", taxonomy_ids)
    styles = normalize_values(discogs["styles"], "discogs_style", taxonomy_ids)

    node_ids = []
    for node_id in genres["node_ids"] + styles["node_ids"]:
        if node_id not in node_ids:
            node_ids.append(node_id)

    return {
        "release_id": release["release_id"],
        "canonical_node_ids": node_ids,
        "genres": genres,
        "styles": styles,
    }
