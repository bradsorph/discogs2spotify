import json
import logging
import urllib.parse
from pathlib import Path

from .api import HTTPTransport
from .artwork import UnusableArtwork
from .errors import APIError, SyncError
from .models import DiscogsRelease, DiscogsTrack


BASE_URL = "https://api.discogs.com"
LOGGER = logging.getLogger("discogs2spotify")


def read_discogs_token(path=None):
    token_path = Path(path or "~/.config/discogs/token").expanduser()
    try:
        token = token_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise SyncError("Cannot read Discogs token at {}: {}".format(token_path, error))
    if not token:
        raise SyncError("Discogs token file is empty: {}".format(token_path))
    return token


class DiscogsClient:
    def __init__(self, token, user_agent="discogs2spotify/1.0", transport=None):
        self.transport = transport or HTTPTransport()
        self.user_agent = user_agent
        self.headers = {
            "User-Agent": user_agent,
            "Authorization": "Discogs token=" + token,
        }

    def _json(self, url):
        data = self.transport.request("Discogs", url, headers=self.headers)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as error:
            raise APIError("Discogs", "invalid response", urllib.parse.urlsplit(url).path, str(error))

    def _paginated(self, url):
        page = 1
        while True:
            separator = "&" if "?" in url else "?"
            data = self._json(
                "{}{}{}".format(
                    url,
                    separator,
                    urllib.parse.urlencode({"page": page, "per_page": 100}),
                )
            )
            yield from data.get("releases", data.get("folders", []))
            pagination = data.get("pagination") or {}
            pages = int(pagination.get("pages") or 1)
            LOGGER.debug("Discogs pagination: received page %d of %d", page, pages)
            if page >= pages:
                break
            page += 1

    def list_folders(self, username):
        endpoint = "{}/users/{}/collection/folders".format(
            BASE_URL, urllib.parse.quote(username, safe="")
        )
        return list(self._paginated(endpoint))

    def list_collection(self, username):
        folders = self.list_folders(username)
        result = {}
        duplicates = []
        folder_by_id = {
            str(folder.get("id")): str(folder.get("name") or "Uncategorized")
            for folder in folders
            if str(folder.get("id")) != "0"
        }
        for folder_id in sorted(folder_by_id, key=lambda value: int(value)):
            endpoint = "{}/users/{}/collection/folders/{}/releases".format(
                BASE_URL,
                urllib.parse.quote(username, safe=""),
                urllib.parse.quote(folder_id, safe=""),
            )
            for item in self._paginated(endpoint):
                basic = item.get("basic_information") or {}
                release_id = str(basic.get("id") or "")
                if not release_id:
                    continue
                record = {
                    "release_id": release_id,
                    "folder_id": folder_id,
                    "folder_name": folder_by_id[folder_id],
                    "basic_information": basic,
                }
                if release_id in result:
                    duplicates.append(release_id)
                    continue
                result[release_id] = record
        return list(result.values()), duplicates, folders

    def get_release(self, release_id):
        endpoint = "{}/releases/{}".format(
            BASE_URL, urllib.parse.quote(str(release_id), safe="")
        )
        return self._json(endpoint)

    def get_image(self, image_url):
        parsed = urllib.parse.urlparse(image_url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme != "https"
            or not (host == "discogs.com" or host.endswith(".discogs.com"))
        ):
            raise UnusableArtwork(
                "Refusing Discogs artwork URL outside HTTPS Discogs hosts"
            )
        # Discogs API tokens are not sent to image hosts.
        return self.transport.request(
            "Discogs artwork",
            image_url,
            headers={"User-Agent": self.user_agent},
        )


def release_from_api(record, details):
    basic = record.get("basic_information") or {}
    artist_names = [
        artist.get("name", "") for artist in (details.get("artists") or basic.get("artists") or [])
    ]
    artist = ", ".join(name for name in artist_names if name)
    title = str(details.get("title") or basic.get("title") or "")
    year_value = details.get("year") or basic.get("year")
    try:
        year = int(year_value) if year_value else None
    except (TypeError, ValueError):
        year = None
    country = str(details.get("country") or "")
    tracks = []
    for ordinal, track in enumerate(details.get("tracklist") or []):
        if track.get("type_") in ("heading", "index"):
            continue
        track_artist = ", ".join(
            item.get("name", "") for item in (track.get("artists") or []) if item.get("name")
        )
        track_title = str(track.get("title") or "").strip()
        if not track_title:
            continue
        tracks.append(
            DiscogsTrack(
                release_id=str(record["release_id"]),
                position=str(track.get("position") or ordinal + 1),
                ordinal=ordinal,
                title=track_title,
                artist=track_artist or artist,
                album=title,
                year=year,
            )
        )
    return DiscogsRelease(
        release_id=str(record["release_id"]),
        folder_id=str(record["folder_id"]),
        folder_name=str(record["folder_name"]),
        artist=artist,
        title=title,
        country=country,
        year=year,
        tracks=tracks,
        raw=details,
    )
