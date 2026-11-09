import os
import shlex
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from .errors import SyncError


DEFAULTS = {
    "DISCOGS_USER": "",
    "SPOTIFY_PLAYLIST_ROOT": "My_record_collection",
    "INCLUDE_FOLDERS": [],
    "EXCLUDE_FOLDERS": ["For-Sale"],
    "SYNC_DELETIONS": False,
    "DRY_RUN": True,
    "MATCH_THRESHOLD": 90,
    "AMBIGUOUS_MATCH_ACTION": "skip",
    "MISSING_TRACK_ACTION": "report",
    "DUPLICATE_TRACK_ACTION": "skip",
    "STATE_FILE": "~/.config/discogs2spotify/state.db",
    "SPOTIFY_PUBLIC_PLAYLISTS": False,
    "PLAYLIST_DESCRIPTION": True,
    "DESCRIPTION_INCLUDE_CATALOG_NUMBER": True,
    "DESCRIPTION_INCLUDE_FORMAT": True,
    "DESCRIPTION_INCLUDE_GENRE": True,
    "DESCRIPTION_INCLUDE_STYLE": True,
    "DESCRIPTION_INCLUDE_NOTES": True,
    "DESCRIPTION_INCLUDE_MATRIX": False,
    "DESCRIPTION_INCLUDE_DISCOGS_URL": True,
    "ARTWORK_IMAGE_INDEX": "primary",
    "API_DELAY": 1.05,
    "SPOTIFY_API_DELAY": 2.0,
    "OAUTH_REDIRECT_URI": "http://127.0.0.1:8888/callback",
    "LOG_LEVEL": "INFO",
}


def _parse_scalar(value):
    value = value.strip()
    if not value:
        return ""
    try:
        parts = shlex.split(value, comments=True, posix=True)
    except ValueError as error:
        raise SyncError("Invalid configuration value {!r}: {}".format(value, error))
    return " ".join(parts) if parts else ""


def _parse_conf(text):
    config = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise SyncError(
                "Invalid configuration line {}: expected KEY=value".format(line_number)
            )
        key, value = line.split("=", 1)
        key = key.strip().upper()
        if not key:
            raise SyncError("Configuration line {} has an empty key".format(line_number))
        config[key] = _parse_scalar(value)
    return config


def _is_yaml_config(path, text):
    if path.suffix.lower() in (".yaml", ".yml"):
        return True
    first_content_line = next(
        (
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ),
        "",
    )
    return bool(first_content_line.startswith("{") or (
        ":" in first_content_line and "=" not in first_content_line
    ))


def _as_bool(value, key):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False
    raise SyncError("{} must be true or false".format(key))


def _as_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    # Commas are the config-file list separator; quoted spaces remain part of a name.
    return [item.strip() for item in str(value).split(",") if item.strip()]


def load_config(path):
    path = Path(path).expanduser()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise SyncError("Configuration file not found: {}".format(path))
    except OSError as error:
        raise SyncError("Cannot read configuration file {}: {}".format(path, error))

    if _is_yaml_config(path, text):
        try:
            parsed = yaml.safe_load(text)
        except yaml.YAMLError as error:
            raise SyncError("Invalid YAML in {}: {}".format(path, error))
        if not isinstance(parsed, dict):
            raise SyncError("Configuration must be a YAML mapping")
        raw = {str(key).upper(): value for key, value in parsed.items()}
    else:
        raw = _parse_conf(text)

    config = dict(DEFAULTS)
    config.update(raw)
    for key in ("INCLUDE_FOLDERS", "EXCLUDE_FOLDERS"):
        config[key] = _as_list(config[key])
    for key in (
        "SYNC_DELETIONS",
        "DRY_RUN",
        "SPOTIFY_PUBLIC_PLAYLISTS",
        "PLAYLIST_DESCRIPTION",
        "DESCRIPTION_INCLUDE_CATALOG_NUMBER",
        "DESCRIPTION_INCLUDE_FORMAT",
        "DESCRIPTION_INCLUDE_GENRE",
        "DESCRIPTION_INCLUDE_STYLE",
        "DESCRIPTION_INCLUDE_NOTES",
        "DESCRIPTION_INCLUDE_MATRIX",
        "DESCRIPTION_INCLUDE_DISCOGS_URL",
    ):
        config[key] = _as_bool(config[key], key)
    try:
        config["MATCH_THRESHOLD"] = float(config["MATCH_THRESHOLD"])
        config["API_DELAY"] = max(0.0, float(config["API_DELAY"]))
        config["SPOTIFY_API_DELAY"] = max(
            0.0, float(config["SPOTIFY_API_DELAY"])
        )
    except (TypeError, ValueError):
        raise SyncError("MATCH_THRESHOLD and API_DELAY must be numbers")
    if not 0 <= config["MATCH_THRESHOLD"] <= 100:
        raise SyncError("MATCH_THRESHOLD must be between 0 and 100")
    if config["AMBIGUOUS_MATCH_ACTION"] not in ("skip", "best"):
        raise SyncError("AMBIGUOUS_MATCH_ACTION must be 'skip' or 'best'")
    if config["MISSING_TRACK_ACTION"] not in ("report", "skip"):
        raise SyncError("MISSING_TRACK_ACTION must be 'report' or 'skip'")
    if config["DUPLICATE_TRACK_ACTION"] not in ("skip", "allow"):
        raise SyncError("DUPLICATE_TRACK_ACTION must be 'skip' or 'allow'")
    config["STATE_FILE"] = str(Path(str(config["STATE_FILE"])).expanduser())
    config["LOG_LEVEL"] = str(config["LOG_LEVEL"]).strip().upper()
    if config["LOG_LEVEL"] not in ("DEBUG", "INFO", "WARNING", "ERROR"):
        raise SyncError("LOG_LEVEL must be DEBUG, INFO, WARNING, or ERROR")
    return config


def apply_overrides(
    config,
    discogs_user=None,
    folders=None,
    excludes=None,
    dry_run=None,
    sync=None,
    state_file=None,
):
    result = dict(config)
    if discogs_user is not None:
        result["DISCOGS_USER"] = discogs_user
    if folders is not None:
        result["INCLUDE_FOLDERS"] = list(folders)
    if excludes is not None:
        result["EXCLUDE_FOLDERS"] = list(excludes)
    if dry_run is not None:
        result["DRY_RUN"] = dry_run
    if sync:
        result["DRY_RUN"] = False
    if state_file is not None:
        result["STATE_FILE"] = str(Path(state_file).expanduser())
    return result


def filter_releases(releases, includes=None, excludes=None):
    include_names = set(_as_list(includes))
    exclude_names = set(_as_list(excludes))
    def folder_name(release):
        if isinstance(release, dict):
            return str(release.get("folder_name") or "")
        return str(getattr(release, "folder_name", "") or "")

    return [
        release
        for release in releases
        if (not include_names or folder_name(release) in include_names)
        and folder_name(release) not in exclude_names
    ]
