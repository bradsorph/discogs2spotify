import base64
import json
import urllib.parse

from .api import HTTPTransport, json_request
from .errors import APIError, SyncError
from .matching import candidate_from_spotify


def _as_bool(value, key):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("true", "1", "yes", "on"):
        return True
    if text in ("false", "0", "no", "off", ""):
        return False
    raise SyncError("{} must be true or false".format(key))


BASE_URL = "https://api.spotify.com/v1"
IMAGE_LIMIT = 256 * 1024


class SpotifyClient:
    def __init__(self, token_manager, transport=None):
        self.token_manager = token_manager
        self.transport = transport or HTTPTransport()
        self._items_path = {}

    def _headers(self, content_type=None):
        headers = {"Authorization": "Bearer " + self.token_manager.access_token()}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _json(self, endpoint, method="GET", payload=None):
        return json_request(
            self.transport,
            "Spotify",
            BASE_URL + endpoint,
            method,
            self._headers("application/json" if payload is not None else None),
            payload,
        )

    def current_user(self):
        return self._json("/me")

    def list_playlists(self):
        playlists = []
        offset = 0
        while True:
            page = self._json(
                "/me/playlists?" + urllib.parse.urlencode({"limit": 50, "offset": offset})
            )
            items = page.get("items") or []
            playlists.extend(items)
            if not page.get("next") or not items:
                break
            offset += len(items)
        return playlists

    def get_playlist(self, playlist_id):
        return self._json(
            "/playlists/{}?fields=id,name,description,images(url),owner(id),public".format(
                urllib.parse.quote(str(playlist_id), safe="")
            )
        )

    def _playlist_item_path(self, playlist_id):
        return self._items_path.get(playlist_id, "items")

    def _page_with_legacy_fallback(self, playlist_id, query):
        path = self._playlist_item_path(playlist_id)
        endpoint = "/playlists/{}/{}?{}".format(
            urllib.parse.quote(str(playlist_id), safe=""), path, query
        )
        try:
            return self._json(endpoint)
        except APIError as error:
            if error.status != 404 or path != "items":
                raise
            self._items_path[playlist_id] = "tracks"
            endpoint = endpoint.replace("/items?", "/tracks?")
            return self._json(endpoint)

    def playlist_tracks(self, playlist_id):
        tracks = []
        offset = 0
        while True:
            page = self._page_with_legacy_fallback(
                playlist_id,
                urllib.parse.urlencode(
                    {"limit": 100, "offset": offset, "additional_types": "track"}
                ),
            )
            items = page.get("items") or []
            for entry in items:
                track = entry.get("item") or entry.get("track")
                if track and track.get("uri") and not track.get("is_local"):
                    tracks.append(track)
            if not page.get("next") or not items:
                break
            offset += len(items)
        return tracks

    def search_tracks(self, track, limit=10):
        # Spotify's search grammar rejects some punctuation/quoted field filters
        # as HTTP 400. Send plain terms and do precise ranking locally.
        query = " ".join(
            value.strip() for value in (track.title, track.artist) if value.strip()
        )
        endpoint = "/search?" + urllib.parse.urlencode(
            {"q": query, "type": "track", "limit": limit}
        )
        data = self._json(endpoint)
        results = ((data.get("tracks") or {}).get("items") or [])
        return [candidate for item in results if (candidate := candidate_from_spotify(item))]

    def create_playlist(self, name, description, public=False):
        public = _as_bool(public, "public")
        return self._json(
            "/me/playlists",
            method="POST",
            payload={"name": name, "description": description, "public": public},
        )

    def update_description(self, playlist_id, description):
        return self._json(
            "/playlists/{}".format(urllib.parse.quote(str(playlist_id), safe="")),
            method="PUT",
            payload={"description": description},
        )

    def replace_tracks(self, playlist_id, uris):
        path = self._playlist_item_path(playlist_id)
        endpoint = "/playlists/{}/{}".format(
            urllib.parse.quote(str(playlist_id), safe=""), path
        )
        first = uris[:100]
        try:
            self._json(endpoint, method="PUT", payload={"uris": first})
        except APIError as error:
            if error.status != 404 or path != "items":
                raise
            self._items_path[playlist_id] = "tracks"
            endpoint = endpoint.replace("/items", "/tracks")
            self._json(endpoint, method="PUT", payload={"uris": first})
        for start in range(100, len(uris), 100):
            chunk = uris[start:start + 100]
            self._json(endpoint, method="POST", payload={"uris": chunk})

    def update_cover(self, playlist_id, jpeg_bytes):
        encoded = base64.b64encode(jpeg_bytes)
        if len(encoded) > IMAGE_LIMIT:
            raise ValueError("base64 JPEG exceeds Spotify's 256 KB payload limit")
        self.transport.request(
            "Spotify",
            "{}/playlists/{}/images".format(
                BASE_URL, urllib.parse.quote(str(playlist_id), safe="")
            ),
            method="PUT",
            headers={
                **self._headers("image/jpeg"),
                "Content-Type": "image/jpeg",
            },
            data=encoded,
        )
