#!/usr/bin/env python3
"""Synchronize Discogs release metadata and artwork to mapped Spotify playlists."""

import argparse
import base64
import hashlib
import html
import io
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


SPOTIFY_API = "https://api.spotify.com/v1"
DISCOGS_API = "https://api.discogs.com"
SPOTIFY_IMAGE_LIMIT = 256 * 1024
DESCRIPTION_LIMIT = 300
DESCRIPTION_DEFAULTS = {
    "PLAYLIST_DESCRIPTION": True,
    "DESCRIPTION_INCLUDE_CATALOG_NUMBER": True,
    "DESCRIPTION_INCLUDE_FORMAT": True,
    "DESCRIPTION_INCLUDE_GENRE": True,
    "DESCRIPTION_INCLUDE_STYLE": True,
    "DESCRIPTION_INCLUDE_NOTES": True,
    "DESCRIPTION_INCLUDE_MATRIX": False,
    "DESCRIPTION_INCLUDE_DISCOGS_URL": True,
}


class SyncError(Exception):
    """An expected API, configuration, or data-processing failure."""


class UnusableArtwork(SyncError):
    """Discogs image data cannot be decoded as an image."""


def http_request(url, method="GET", headers=None, data=None):
    request = urllib.request.Request(
        url, data=data, headers=headers or {}, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise SyncError(
            "{} {} failed (HTTP {}): {}".format(method, url, error.code, body)
        )
    except urllib.error.URLError as error:
        raise SyncError("{} {} failed: {}".format(method, url, error.reason))


class SpotifyClient:
    def __init__(self, token):
        self.headers = {
            "Authorization": "Bearer " + token,
            "User-Agent": "discogs2spotify/1.0",
        }

    def get_playlist(self, playlist_id):
        url = "{}/playlists/{}?fields=id,name,description,images(url)".format(
            SPOTIFY_API, urllib.parse.quote(playlist_id, safe="")
        )
        try:
            return json.loads(http_request(url, headers=self.headers).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SyncError("Spotify returned invalid playlist JSON: {}".format(error))

    def update_description(self, playlist_id, description):
        url = "{}/playlists/{}".format(
            SPOTIFY_API, urllib.parse.quote(playlist_id, safe="")
        )
        body = json.dumps({"description": description}).encode("utf-8")
        headers = dict(self.headers)
        headers["Content-Type"] = "application/json"
        http_request(url, method="PUT", headers=headers, data=body)

    def update_cover(self, playlist_id, jpeg_base64):
        url = "{}/playlists/{}/images".format(
            SPOTIFY_API, urllib.parse.quote(playlist_id, safe="")
        )
        headers = dict(self.headers)
        headers["Content-Type"] = "image/jpeg"
        http_request(
            url, method="PUT", headers=headers, data=jpeg_base64.encode("ascii")
        )


class DiscogsClient:
    def __init__(self, user_agent):
        self.headers = {"User-Agent": user_agent}

    def get_release(self, release_id):
        url = "{}/releases/{}".format(
            DISCOGS_API, urllib.parse.quote(str(release_id), safe="")
        )
        try:
            return json.loads(http_request(url, headers=self.headers).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SyncError("Discogs returned invalid release JSON: {}".format(error))

    def get_image(self, image_url):
        return http_request(image_url, headers=self.headers)


class StateStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(str(path))
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS playlist_state (
                playlist_id TEXT NOT NULL,
                discogs_release_id TEXT NOT NULL,
                description_hash TEXT,
                artwork_hash TEXT,
                PRIMARY KEY (playlist_id, discogs_release_id)
            )
            """
        )
        self.connection.commit()

    def get(self, playlist_id, release_id):
        row = self.connection.execute(
            """
            SELECT description_hash, artwork_hash
            FROM playlist_state
            WHERE playlist_id = ? AND discogs_release_id = ?
            """,
            (playlist_id, str(release_id)),
        ).fetchone()
        if row is None:
            return {"description_hash": None, "artwork_hash": None}
        return {"description_hash": row[0], "artwork_hash": row[1]}

    def save(self, playlist_id, release_id, description_hash=None, artwork_hash=None):
        self.connection.execute(
            """
            INSERT INTO playlist_state (
                playlist_id, discogs_release_id, description_hash, artwork_hash
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT (playlist_id, discogs_release_id) DO UPDATE SET
                description_hash = COALESCE(excluded.description_hash,
                                             playlist_state.description_hash),
                artwork_hash = COALESCE(excluded.artwork_hash,
                                        playlist_state.artwork_hash)
            """,
            (playlist_id, str(release_id), description_hash, artwork_hash),
        )
        self.connection.commit()

    def close(self):
        self.connection.close()


def _clean(value):
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]*>", " ", text)
    return " ".join(text.split())


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _metadata_lines(release, release_id, settings):
    lines = []
    labels = _as_list(release.get("labels"))
    if labels:
        label = _clean(labels[0].get("name"))
        catalog = _clean(labels[0].get("catno"))
        if not settings["DESCRIPTION_INCLUDE_CATALOG_NUMBER"]:
            catalog = ""
        if label or catalog:
            lines.append(" · ".join(part for part in (label, catalog) if part))

    country = _clean(release.get("country"))
    year = _clean(release.get("year"))
    if country or year:
        lines.append(" · ".join(part for part in (country, year) if part))

    if settings["DESCRIPTION_INCLUDE_FORMAT"]:
        for item in _as_list(release.get("formats")):
            name = _clean(item.get("name"))
            descriptions = [_clean(value) for value in _as_list(item.get("descriptions"))]
            descriptions = [value for value in descriptions if value]
            if name or descriptions:
                lines.append(" · ".join(part for part in (name, ", ".join(descriptions)) if part))

    genres = _as_list(release.get("genres")) if settings["DESCRIPTION_INCLUDE_GENRE"] else []
    styles = _as_list(release.get("styles")) if settings["DESCRIPTION_INCLUDE_STYLE"] else []
    genres = [_clean(value) for value in genres if _clean(value)]
    styles = [_clean(value) for value in styles if _clean(value)]
    if genres or styles:
        lines.append(" · ".join(part for part in (", ".join(genres), ", ".join(styles)) if part))

    if settings["DESCRIPTION_INCLUDE_NOTES"]:
        notes = _clean(release.get("notes"))
        if notes:
            if len(notes) > 90:
                notes = notes[:87].rstrip() + "..."
            lines.append(notes)

    if settings["DESCRIPTION_INCLUDE_MATRIX"]:
        identifiers = _as_list(release.get("identifiers"))
        matrix_values = [
            _clean(item.get("value"))
            for item in identifiers
            if "matrix" in _clean(item.get("type")).lower()
            and _clean(item.get("value"))
        ]
        if matrix_values:
            lines.append("Matrix: " + ", ".join(matrix_values))

    if settings["DESCRIPTION_INCLUDE_DISCOGS_URL"]:
        lines.append("Discogs release: {}".format(release_id))
        lines.append("https://www.discogs.com/release/{}".format(release_id))
    return lines


def generate_description(release, release_id, settings):
    lines = _metadata_lines(release, release_id, settings)
    if not lines:
        return ""

    # Keep the release reference intact when long source fields approach Spotify's limit.
    footer_count = 2 if settings["DESCRIPTION_INCLUDE_DISCOGS_URL"] else 0
    footer = lines[-footer_count:] if footer_count else []
    body = lines[:-footer_count] if footer_count else lines
    footer_text = "\n".join(footer)
    remaining = DESCRIPTION_LIMIT - len(footer_text) - (1 if body and footer else 0)
    kept = []
    for line in body:
        separator = 1 if kept else 0
        available = remaining - separator
        if available <= 0:
            break
        if len(line) > available:
            line = line[: max(0, available - 3)].rstrip() + ("..." if available >= 3 else "")
        if line:
            kept.append(line)
            remaining -= len(line) + separator
    result = "\n".join(kept + footer)
    return result[:DESCRIPTION_LIMIT]


def _select_image(images, image_index):
    if not images:
        return None
    if image_index == "primary":
        for image in images:
            if _clean(image.get("type")).lower() == "primary":
                return image
        return images[0]
    try:
        index = int(image_index)
    except (TypeError, ValueError):
        raise SyncError("ARTWORK_IMAGE_INDEX must be 'primary' or an image index")
    if index < 0 or index >= len(images):
        raise SyncError(
            "ARTWORK_IMAGE_INDEX {} is out of range for {} release images".format(
                index, len(images)
            )
        )
    return images[index]


def make_spotify_jpeg(image_bytes):
    try:
        with Image.open(io.BytesIO(image_bytes)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise UnusableArtwork("Discogs artwork is not a usable image: {}".format(error))

    image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
    while True:
        for quality in (90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40):
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            jpeg = output.getvalue()
            if len(base64.b64encode(jpeg)) <= SPOTIFY_IMAGE_LIMIT:
                return jpeg
        if image.size == (1, 1):
            break
        next_size = (max(1, int(image.width * 0.8)), max(1, int(image.height * 0.8)))
        image = image.resize(next_size, Image.Resampling.LANCZOS)
    raise SyncError("Discogs artwork could not be compressed under Spotify's 256 KB limit")


def _bool_setting(value, key):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off"):
        return False
    raise SyncError("{} must be true or false".format(key))


def load_settings(config):
    settings = {}
    for key, default in DESCRIPTION_DEFAULTS.items():
        value = os.environ.get(key, config.get(key, default))
        settings[key] = _bool_setting(value, key)
    settings["ARTWORK_IMAGE_INDEX"] = os.environ.get(
        "ARTWORK_IMAGE_INDEX", config.get("ARTWORK_IMAGE_INDEX", "primary")
    )
    return settings


def synchronize_playlist(
    spotify, discogs, state, playlist_id, release_id, settings
):
    release = discogs.get_release(release_id)
    playlist = spotify.get_playlist(playlist_id)
    stored = state.get(playlist_id, release_id)
    statuses = {}

    if settings["PLAYLIST_DESCRIPTION"]:
        description = generate_description(release, release_id, settings)
        description_hash = hashlib.sha256(description.encode("utf-8")).hexdigest()
        current_description = playlist.get("description") or ""
        if stored["description_hash"] == description_hash:
            statuses["DESCRIPTION"] = "UNCHANGED"
        elif current_description == description:
            statuses["DESCRIPTION"] = "UNCHANGED"
            state.save(playlist_id, release_id, description_hash=description_hash)
        else:
            statuses["DESCRIPTION"] = "UPDATED" if current_description else "CREATED"
            spotify.update_description(playlist_id, description)
            state.save(playlist_id, release_id, description_hash=description_hash)
    else:
        statuses["DESCRIPTION"] = "DISABLED"

    image = _select_image(release.get("images"), settings["ARTWORK_IMAGE_INDEX"])
    image_url = _clean(image.get("uri")) if image else ""
    if not image_url:
        statuses["COVER"] = "MISSING"
        return statuses

    artwork = discogs.get_image(image_url)
    artwork_hash = hashlib.sha256(artwork).hexdigest()
    has_spotify_image = bool(playlist.get("images"))
    if stored["artwork_hash"] == artwork_hash and has_spotify_image:
        statuses["COVER"] = "UNCHANGED"
        return statuses

    try:
        jpeg = make_spotify_jpeg(artwork)
    except UnusableArtwork:
        statuses["COVER"] = "MISSING"
        return statuses
    encoded = base64.b64encode(jpeg).decode("ascii")
    if len(encoded.encode("ascii")) > SPOTIFY_IMAGE_LIMIT:
        raise SyncError("Prepared artwork exceeds Spotify's 256 KB payload limit")
    statuses["COVER"] = (
        "UPDATED"
        if stored["artwork_hash"] is not None and has_spotify_image
        else "CREATED"
    )
    spotify.update_cover(playlist_id, encoded)
    state.save(playlist_id, release_id, artwork_hash=artwork_hash)
    return statuses


def load_config(path):
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SyncError(
            "Configuration file not found: {} (copy config.example.json to config.json)".format(
                path
            )
        )
    except json.JSONDecodeError as error:
        raise SyncError("Invalid JSON in {}: {}".format(path, error))
    if not isinstance(config, dict):
        raise SyncError("Configuration must be a JSON object")
    playlists = config.get("playlists")
    if not isinstance(playlists, list) or not playlists:
        raise SyncError("Configuration must contain a non-empty 'playlists' array")
    for item in playlists:
        if not isinstance(item, dict) or not item.get("playlist_id") or not item.get(
            "discogs_release_id"
        ):
            raise SyncError(
                "Each playlist entry needs playlist_id and discogs_release_id"
            )
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Synchronize mapped Discogs release artwork and metadata to Spotify."
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).with_name("config.json")),
        help="JSON configuration file (default: config.json beside this script)",
    )
    parser.add_argument(
        "--state-db",
        help="SQLite state database (default: .discogs2spotify.sqlite3 beside config)",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    settings = load_settings(config)
    token = os.environ.get("SPOTIFY_ACCESS_TOKEN", "").strip()
    if not token:
        raise SyncError("Set SPOTIFY_ACCESS_TOKEN to a Spotify user token with ugc-image-upload scope")
    state_path = (
        Path(args.state_db).expanduser()
        if args.state_db
        else config_path.parent / ".discogs2spotify.sqlite3"
    )

    spotify = SpotifyClient(token)
    discogs = DiscogsClient(
        os.environ.get("DISCOGS_USER_AGENT", "discogs2spotify/1.0 +https://github.com/bradsorph/discogs2spotify")
    )
    state = StateStore(state_path)
    try:
        for item in config["playlists"]:
            playlist_id = str(item["playlist_id"])
            release_id = str(item["discogs_release_id"])
            statuses = synchronize_playlist(
                spotify, discogs, state, playlist_id, release_id, settings
            )
            name = item.get("name") or playlist_id
            print(
                "{} [playlist {} · Discogs {}]".format(name, playlist_id, release_id)
            )
            print("  COVER {}".format(statuses["COVER"]))
            print("  DESCRIPTION {}".format(statuses["DESCRIPTION"]))
    finally:
        state.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SyncError as error:
        print("Error: {}".format(error), file=sys.stderr)
        sys.exit(1)
