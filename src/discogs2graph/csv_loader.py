import csv
from collections import Counter
from typing import Dict, Iterable, List, Optional


RELEASE_ID_FIELDS = ("release_id", "Catalog#", "ReleaseID")
ARTIST_FIELDS = ("Artist", "artist")
TITLE_FIELDS = ("Title", "title")
GENRE_FIELDS = ("discogs_genre", "Genre")
STYLE_FIELDS = ("discogs_style", "Style")


def split_multi_value(raw: object) -> List[str]:
    if raw is None:
        return []
    return [part.strip() for part in str(raw).split(";") if part.strip()]


def first_present(row: Dict[str, str], names: Iterable[str]) -> Optional[str]:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def parse_year(row: Dict[str, str]) -> Optional[int]:
    value = first_present(row, ("discogs_year", "Released", "year"))
    if not value:
        return None
    try:
        year = int(value)
        return year if year > 0 else None
    except (TypeError, ValueError):
        return None


def load_collection(path: str) -> Dict[str, object]:
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    releases: List[Dict[str, object]] = []
    for row in rows:
        raw = dict(row)
        release_id = first_present(row, RELEASE_ID_FIELDS)
        artist = first_present(row, ARTIST_FIELDS)
        title = first_present(row, TITLE_FIELDS)
        folder_name = first_present(row, ("CollectionFolder", "folder_name"))
        genre_raw = first_present(row, GENRE_FIELDS)
        style_raw = first_present(row, STYLE_FIELDS)

        releases.append({
            "release_id": release_id,
            "artist": artist,
            "title": title,
            "folder_name": folder_name,
            "year": parse_year(row),
            "discogs": {
                "genres_raw": genre_raw,
                "genres": split_multi_value(genre_raw),
                "styles_raw": style_raw,
                "styles": split_multi_value(style_raw),
            },
            "raw": raw,
        })

    return {
        "fieldnames": fieldnames,
        "releases": releases,
        "source_row_count": len(rows),
    }


def duplicate_release_ids(releases: Iterable[Dict[str, object]]) -> Dict[str, int]:
    counts = Counter(str(release["release_id"]).strip()
                     for release in releases
                     if release.get("release_id") is not None)
    return {release_id: count for release_id, count in counts.items() if count > 1}
