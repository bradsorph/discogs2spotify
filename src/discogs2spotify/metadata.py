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


def playlist_name(release, root=None):
    parts = [_safe_component(release.artist), _safe_component(release.title)]
    name = "_".join(part for part in parts if part)
    if not name:
        name = "Discogs release {}".format(release.release_id)
    safe_root = _safe_component(root)
    if safe_root:
        name = safe_root + "-" + name
    if len(name) > 100:
        name = name[:97].rstrip(" _.-") + "..."
    return name


def playlist_names(releases, root=None):
    names = {}
    grouped = {}
    used_names = set()
    for release in releases:
        grouped.setdefault(playlist_name(release, root).casefold(), []).append(release)
    for group_key in sorted(grouped):
        group = grouped[group_key]
        group.sort(
            key=lambda release: (
                0 if str(release.release_id).isdigit() else 1,
                int(release.release_id)
                if str(release.release_id).isdigit()
                else str(release.release_id).casefold(),
            )
        )
        base_name = playlist_name(group[0], root)
        for index, release in enumerate(group, 1):
            suffix_index = max(index, 2)
            name = base_name if index == 1 else ""
            while not name or name.casefold() in used_names:
                suffix = " ({})".format(suffix_index)
                name = base_name[:100 - len(suffix)].rstrip(" _.-") + suffix
                suffix_index += 1
            names[release.release_id] = name
            used_names.add(name.casefold())
    return names


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
    folder_name = clean(release.folder_name)
    if folder_name:
        lines.append("Discogs folder: " + folder_name)
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
