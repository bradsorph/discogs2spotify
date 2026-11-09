import hashlib
import html
import re

DESCRIPTION_LIMIT = 300


def clean(value):
    if value is None:
        return ""
    return " ".join(re.sub(r"<[^>]*>", " ", html.unescape(str(value))).split())


def playlist_name(release):
    parts = [
        release.artist,
        release.title,
        release.country,
        str(release.year) if release.year else "",
    ]
    parts = [
        re.sub(r"[/\\\x00-\x1f]", " ", clean(part)).strip(" ._")
        for part in parts
        if clean(part)
    ]
    name = "_".join(part for part in parts if part)
    if len(name) > 100:
        name = name[:97].rstrip(" _.-") + "..."
    return name or "Discogs release {}".format(release.release_id)


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
    if release.country or release.year:
        lines.append(
            " · ".join(
                part for part in (release.country, str(release.year or "")) if part
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
    lines.append("Discogs release: {}".format(release.release_id))
    if settings.get("DESCRIPTION_INCLUDE_DISCOGS_URL", True):
        lines.append("https://www.discogs.com/release/{}".format(release.release_id))
    if not lines:
        lines.append("Discogs release: {}".format(release.release_id))

    footer_count = 2 if settings.get("DESCRIPTION_INCLUDE_DISCOGS_URL", True) else 1
    footer = lines[-footer_count:] if footer_count else []
    body = lines[:-footer_count] if footer_count else lines
    room = DESCRIPTION_LIMIT - len("\n".join(footer)) - (1 if footer and body else 0)
    result = []
    for line in body:
        available = room - (1 if result else 0)
        if available <= 0:
            break
        if len(line) > available:
            line = line[:max(0, available - 3)].rstrip() + ("..." if available >= 3 else "")
        result.append(line)
        room -= len(line) + (1 if len(result) > 1 else 0)
    description = "\n".join(result + footer)
    return description[:DESCRIPTION_LIMIT]


def stable_hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def discogs_marker(description):
    match = re.search(r"(?m)^Discogs release:\s*(\d+)\s*$", description or "")
    return match.group(1) if match else None
