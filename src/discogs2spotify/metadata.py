import hashlib
import html
import re

DESCRIPTION_LIMIT = 300


def clean(value):
    if value is None:
        return ""
    return " ".join(re.sub(r"<[^>]*>", " ", html.unescape(str(value))).split())


def _safe_component(value):
    return re.sub(r"[/\\\x00-\x1f]", " ", clean(value)).strip(" ._")


def playlist_name(release, settings=None):
    release_parts = [
        release.artist,
        release.title,
        release.country,
        str(release.year) if release.year else "",
    ]
    release_parts = [_safe_component(part) for part in release_parts]
    release_suffix = "_".join(part for part in release_parts if part)
    prefix_parts = []
    if settings:
        root = _safe_component(settings.get("SPOTIFY_PLAYLIST_ROOT", ""))
        folder = _safe_component(release.folder_name)
        prefix_parts = [part for part in (root, folder) if part]
    prefix = "/".join(prefix_parts)
    name = "/".join(part for part in (prefix, release_suffix) if part)
    if not name:
        name = "Discogs release {}".format(release.release_id)
    if len(name) > 100:
        if prefix and release_suffix:
            available = 100 - len(prefix) - 1
            if available > 3:
                suffix = release_suffix[:available - 3].rstrip(" _.-") + "..."
                name = prefix + "/" + suffix
            else:
                name = prefix[:97].rstrip(" /._-") + "..."
        else:
            name = name[:97].rstrip(" _.-") + "..."
    return name


def _truncate_utf8(value, limit):
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    if limit <= 3:
        return encoded[:limit].decode("utf-8", errors="ignore")
    return encoded[:limit - 3].decode("utf-8", errors="ignore").rstrip() + "..."


def generate_description(release, settings):
    if not settings.get("PLAYLIST_DESCRIPTION", True):
        return "Discogs release: {}".format(release.release_id)
    lines = []
    labels = release.raw.get("labels") or []
    if labels:
        label = clean(labels[0].get("name"))
        catalog = clean(labels[0].get("catno"))
        if not settings.get("DESCRIPTION_INCLUDE_CATALOG_NUMBER", True):
            catalog = ""
        if label or catalog:
            lines.append(" · ".join(part for part in (label, catalog) if part))
    country = clean(release.country)
    if country or release.year:
        lines.append(
            " · ".join(
                part for part in (country, str(release.year or "")) if part
            )
        )
    if settings.get("DESCRIPTION_INCLUDE_FORMAT", True):
        for item in release.raw.get("formats") or []:
            name = clean(item.get("name"))
            descriptions = [
                clean(value) for value in (item.get("descriptions") or []) if clean(value)
            ]
            if name or descriptions:
                lines.append(
                    " · ".join(part for part in (name, ", ".join(descriptions)) if part)
                )
    genres = (
        release.raw.get("genres") or []
        if settings.get("DESCRIPTION_INCLUDE_GENRE", True)
        else []
    )
    styles = (
        release.raw.get("styles") or []
        if settings.get("DESCRIPTION_INCLUDE_STYLE", True)
        else []
    )
    genres = [clean(value) for value in genres if clean(value)]
    styles = [clean(value) for value in styles if clean(value)]
    if genres or styles:
        lines.append(" · ".join(part for part in (", ".join(genres), ", ".join(styles)) if part))
    if settings.get("DESCRIPTION_INCLUDE_NOTES", True):
        notes = clean(release.raw.get("notes"))
        if notes:
            lines.append(notes[:87].rstrip() + "..." if len(notes) > 90 else notes)
    if settings.get("DESCRIPTION_INCLUDE_MATRIX", False):
        values = [
            clean(item.get("value"))
            for item in release.raw.get("identifiers") or []
            if "matrix" in clean(item.get("type")).lower()
            and clean(item.get("value"))
        ]
        if values:
            lines.append("Matrix: " + ", ".join(values))
    marker = "Discogs release: {}".format(release.release_id)
    url = None
    if settings.get("DESCRIPTION_INCLUDE_DISCOGS_URL", True):
        url = "https://www.discogs.com/release/{}".format(release.release_id)
    separator = " · "
    separator_size = len(separator.encode("utf-8"))
    body_budget = DESCRIPTION_LIMIT - len(marker.encode("utf-8"))
    if url:
        body_budget -= separator_size + len(url.encode("utf-8"))
    result = []
    for line in lines:
        available = body_budget - separator_size
        if available <= 0:
            break
        line = _truncate_utf8(line, available)
        result.append(line)
        body_budget -= separator_size + len(line.encode("utf-8"))
    parts = [marker] + result
    if url:
        parts.append(url)
    return separator.join(parts)


def stable_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def discogs_marker(description):
    match = re.search(r"(?m)^Discogs release:\s*(\d+)(?:\s|$)", description or "")
    return match.group(1) if match else None
