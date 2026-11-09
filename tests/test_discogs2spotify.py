import base64
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import urlparse, parse_qs

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discogs2spotify.api import HTTPTransport
from discogs2spotify.artwork import IMAGE_LIMIT, UnusableArtwork, make_spotify_jpeg
from discogs2spotify.config import apply_overrides, filter_releases, load_config
from discogs2spotify.database import StateStore
from discogs2spotify.discogs import DiscogsClient, release_from_api
from discogs2spotify.errors import APIError, SyncError
from discogs2spotify.matching import match_track, normalize_text
from discogs2spotify import main as main_module
from discogs2spotify.metadata import generate_description, playlist_name, stable_hash
from discogs2spotify.models import DiscogsRelease, DiscogsTrack, TrackCandidate
from discogs2spotify.oauth import BASE_SCOPES, SpotifyTokenManager
from discogs2spotify.reporting import print_report
from discogs2spotify.sync import synchronize


def png_bytes():
    image = Image.new("RGB", (1800, 1800), (12, 72, 140))
    output = __import__("io").BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def sample_release(release_id="3812903", tracks=None):
    source_tracks = tracks if tracks is not None else [
        DiscogsTrack(release_id, "A1", 0, "The Waterfront", "John Lee Hooker", "The Real Folk Blues", 1980)
    ]
    return DiscogsRelease(
        release_id=release_id,
        folder_id="3",
        folder_name="Blues",
        artist="John Lee Hooker",
        title="The Real Folk Blues",
        country="France",
        year=1980,
        tracks=source_tracks,
        raw={
            "images": [
                {"type": "secondary", "uri": "https://img.example/other.png"},
                {"type": "primary", "uri": "https://img.example/release.png"},
            ],
            "labels": [{"name": "Chess", "catno": "515009"}],
            "formats": [{"name": "LP", "descriptions": ["Album", "Reissue"]}],
            "genres": ["Blues"],
            "styles": ["Chicago Blues"],
            "notes": "Made in France",
        },
    )


def candidate(
    spotify_id="spotify-1",
    title="The Waterfront",
    artist="John Lee Hooker",
    album="The Real Folk Blues",
    year=1980,
):
    return TrackCandidate(
        spotify_id=spotify_id,
        uri="spotify:track:" + spotify_id,
        title=title,
        artist=artist,
        album=album,
        year=year,
    )


def app_config():
    config = load_config_from_mapping()
    return config


def load_config_from_mapping():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "discogs2spotify.conf"
        path.write_text("DISCOGS_USER=bradz667\n", encoding="utf-8")
        return load_config(path)


class FakeDiscogs:
    def __init__(self, artwork=None):
        self.artwork = artwork if artwork is not None else png_bytes()
        self.requested_images = []

    def get_image(self, image_url):
        self.requested_images.append(image_url)
        return self.artwork

    def list_collection(self, username):
        return (
            [{
                "release_id": "3812903",
                "folder_id": "3",
                "folder_name": "Blues",
                "basic_information": {"id": 3812903},
            }],
            [],
            [{"id": 3, "name": "Blues"}],
        )

    def get_release(self, release_id):
        return {
            "title": "The Real Folk Blues",
            "artists": [{"name": "John Lee Hooker"}],
            "country": "France",
            "year": 1980,
            "labels": [{"name": "Chess", "catno": "515009"}],
            "formats": [{"name": "LP", "descriptions": ["Album", "Reissue"]}],
            "tracklist": [{
                "position": "A1",
                "title": "The Waterfront",
                "artists": [{"name": "John Lee Hooker"}],
            }],
            "images": [{"type": "primary", "uri": "https://img.example/release.png"}],
        }


class FakeSpotify:
    def __init__(self, search_results=None, playlists=None):
        self.search_results = search_results or {}
        self.playlists = playlists or {}
        self.writes = []
        self.created = 0

    def list_playlists(self):
        return list(self.playlists.values())

    def get_playlist(self, playlist_id):
        return self.playlists[str(playlist_id)]

    def playlist_tracks(self, playlist_id):
        return list(self.playlists[str(playlist_id)].get("_tracks", []))

    def search_tracks(self, track):
        return list(self.search_results.get(track.title, []))

    def create_playlist(self, name, description, public=False):
        self.created += 1
        playlist_id = "created-{}".format(self.created)
        self.playlists[playlist_id] = {
            "id": playlist_id,
            "name": name,
            "description": description,
            "images": [],
            "_tracks": [],
            "public": public,
        }
        self.writes.append(("create", playlist_id))
        return self.playlists[playlist_id]

    def update_description(self, playlist_id, description):
        self.playlists[str(playlist_id)]["description"] = description
        self.writes.append(("description", playlist_id))

    def replace_tracks(self, playlist_id, uris):
        playlist = self.playlists[str(playlist_id)]
        playlist["_tracks"] = [{"uri": uri} for uri in uris]
        self.writes.append(("tracks", playlist_id, list(uris)))

    def update_cover(self, playlist_id, jpeg_bytes):
        self.playlists[str(playlist_id)]["images"] = [{"url": "custom-cover"}]
        self.writes.append(("cover", playlist_id, jpeg_bytes))


class ConfigTests(unittest.TestCase):
    def test_conf_file_defaults_and_cli_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discogs2spotify.conf"
            path.write_text(
                'DISCOGS_USER=bradz667\n'
                'INCLUDE_FOLDERS=Blues,"Soul - Funk"\n'
                'EXCLUDE_FOLDERS=For-Sale\n'
                'DRY_RUN=true\n'
                'MATCH_THRESHOLD=92\n',
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config["INCLUDE_FOLDERS"], ["Blues", "Soul - Funk"])
        self.assertEqual(config["STATE_FILE"], str(Path("~/.config/discogs2spotify/state.db").expanduser()))
        overridden = apply_overrides(
            config, folders=["Rock"], excludes=["For-Sale"], sync=True
        )
        self.assertEqual(overridden["INCLUDE_FOLDERS"], ["Rock"])
        self.assertFalse(overridden["DRY_RUN"])
        self.assertEqual(config["MATCH_THRESHOLD"], 92)

    def test_yaml_config_is_still_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "custom.yaml"
            path.write_text(
                "DISCOGS_USER: bradz667\n"
                "INCLUDE_FOLDERS:\n"
                "  - Soul - Funk\n"
                "DRY_RUN: false\n",
                encoding="utf-8",
            )
            config = load_config(path)
        self.assertEqual(config["INCLUDE_FOLDERS"], ["Soul - Funk"])
        self.assertFalse(config["DRY_RUN"])

    def test_folder_filter_include_and_exclude(self):
        releases = [
            {"folder_name": "Blues"},
            {"folder_name": "Rock"},
            {"folder_name": "For-Sale"},
        ]
        self.assertEqual(
            filter_releases(releases, includes=["Blues", "Rock"], excludes=["Rock"]),
            [{"folder_name": "Blues"}],
        )
        self.assertEqual(
            filter_releases(releases, includes=[], excludes=["For-Sale"]),
            releases[:2],
        )


class DiscogsTests(unittest.TestCase):
    def test_collection_pagination_and_release_deduplication(self):
        class FakeTransport:
            def __init__(self):
                self.urls = []

            def request(self, service, url, **kwargs):
                self.urls.append(url)
                parsed = urlparse(url)
                query = parse_qs(parsed.query)
                page = int(query.get("page", ["1"])[0])
                if parsed.path.endswith("/collection/folders"):
                    response = (
                        {"folders": [{"id": 0, "name": "All"}, {"id": 1, "name": "Blues"}]}
                        if page == 1
                        else {"folders": [{"id": 2, "name": "Rock"}]}
                    )
                    response["pagination"] = {"pages": 2}
                elif parsed.path.endswith("/folders/1/releases"):
                    response = (
                        {"releases": [{"basic_information": {"id": 11}}]}
                        if page == 1
                        else {"releases": [{"basic_information": {"id": 12}}]}
                    )
                    response["pagination"] = {"pages": 2}
                else:
                    response = {
                        "releases": [{"basic_information": {"id": 12}}],
                        "pagination": {"pages": 1},
                    }
                return json.dumps(response).encode()

        transport = FakeTransport()
        client = DiscogsClient("not-a-real-token", transport=transport)
        releases, duplicates, folders = client.list_collection("bradz667")
        self.assertEqual([release["release_id"] for release in releases], ["11", "12"])
        self.assertEqual(duplicates, ["12"])
        self.assertEqual(len(folders), 3)
        self.assertGreaterEqual(len(transport.urls), 5)

    def test_missing_metadata_and_track_artist_fallback(self):
        record = {
            "release_id": "20",
            "folder_id": "1",
            "folder_name": "Uncategorized",
            "basic_information": {},
        }
        release = release_from_api(
            record,
            {
                "artists": [{"name": "Album artist"}],
                "tracklist": [
                    {"position": "A1", "title": "Track", "artists": [{"name": "Guest"}]},
                    {"position": "A2", "title": "Fallback"},
                ],
            },
        )
        self.assertEqual(release.title, "")
        self.assertIsNone(release.year)
        self.assertEqual(release.tracks[0].artist, "Guest")
        self.assertEqual(release.tracks[1].artist, "Album artist")

    def test_artwork_download_does_not_send_api_token(self):
        class FakeTransport:
            headers = None

            def request(self, service, url, **kwargs):
                self.headers = kwargs["headers"]
                return b"image"

        transport = FakeTransport()
        client = DiscogsClient("private-token", transport=transport)
        client.get_image("https://i.discogs.com/cover.jpg")
        self.assertNotIn("Authorization", transport.headers)
        with self.assertRaises(UnusableArtwork):
            client.get_image("https://not-discogs.example/cover.jpg")


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.track = sample_release().tracks[0]

    def test_normalizes_titles_and_artists(self):
        self.assertEqual(normalize_text("Beyoncé — Live & Featuring"), "beyonce live and")
        self.assertEqual(normalize_text("John Lee Hooker"), "john lee hooker")

    def test_exact_and_fuzzy_matches(self):
        exact = match_track(self.track, [candidate()], threshold=90)
        fuzzy = match_track(
            self.track,
            [candidate(title="The Waterfrnt")],
            threshold=90,
        )
        self.assertEqual(exact.status, "MATCHED")
        self.assertEqual(exact.score, 100)
        self.assertEqual(fuzzy.status, "MATCHED")

    def test_ambiguous_match_and_version_rejection(self):
        ambiguous = match_track(
            self.track,
            [candidate("id-a"), candidate("id-b")],
            threshold=90,
        )
        live = match_track(
            self.track,
            [candidate(title="The Waterfront (Live)")],
            threshold=80,
        )
        self.assertEqual(ambiguous.status, "AMBIGUOUS")
        self.assertEqual(live.status, "REJECTED")

    def test_missing_spotify_candidate(self):
        result = match_track(self.track, [], threshold=90)
        self.assertEqual(result.status, "MISSING")

    def test_playlist_name_uses_available_fields_and_is_limited(self):
        release = sample_release()
        self.assertEqual(
            playlist_name(release),
            "John Lee Hooker_The Real Folk Blues_France_1980",
        )
        release.artist = ""
        release.country = ""
        release.year = None
        release.title = "A" * 150
        self.assertLessEqual(len(playlist_name(release)), 100)

    def test_description_includes_available_edition_metadata_and_is_hashed(self):
        release = sample_release()
        config = app_config()
        description = generate_description(release, config)
        self.assertIn("Chess · 515009", description)
        self.assertIn("France · 1980", description)
        self.assertIn("LP · Album, Reissue", description)
        self.assertIn("Discogs release: 3812903", description)
        self.assertIn("https://www.discogs.com/release/3812903", description)
        self.assertLessEqual(len(description), 300)
        self.assertEqual(stable_hash(description), stable_hash(description))
        config["DESCRIPTION_INCLUDE_DISCOGS_URL"] = False
        self.assertNotIn("https://www.discogs.com", generate_description(release, config))


class HTTPTests(unittest.TestCase):
    def test_retries_rate_limit_then_succeeds(self):
        sleeps = []
        responses = [
            HTTPError("https://api.example/test", 429, "slow down", {"Retry-After": "0"}, None),
            mock.MagicMock(read=mock.Mock(return_value=b"ok")),
        ]
        responses[1].__enter__.return_value = responses[1]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            transport = HTTPTransport(delay=0, retries=1, sleeper=sleeps.append)
            self.assertEqual(
                transport.request("Spotify", "https://api.example/test"), b"ok"
            )
        self.assertEqual(sleeps, [0.0])


class StateAndSyncTests(unittest.TestCase):
    def _state(self, directory, readonly=False):
        return StateStore(Path(directory) / "state.db", readonly=readonly)

    def test_duplicate_discogs_tracks_are_preserved_and_reported(self):
        track_one = DiscogsTrack("7", "A1", 0, "Same Song", "Artist", "Album", 1970)
        track_two = DiscogsTrack("7", "A2", 1, "Same Song", "Artist", "Album", 1970)
        release = sample_release("7", [track_one, track_two])
        spotify = FakeSpotify({"Same Song": [candidate(title="Same Song", artist="Artist")]})
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory, readonly=True)
            report = synchronize(
                [release], ["7"], spotify, discogs, state, app_config(), dry_run=True
            )
            state.close()
        plan = report.plans[0]
        self.assertEqual(plan.desired_uris, ["spotify:track:spotify-1"] * 2)
        self.assertEqual(report.duplicate_tracks, 1)

    def test_missing_tracks_do_not_abort_release(self):
        release = sample_release()
        spotify = FakeSpotify({"The Waterfront": []})
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory, readonly=True)
            report = synchronize(
                [release], ["3812903"], spotify, discogs, state, app_config(), dry_run=True
            )
            state.close()
        self.assertEqual(report.releases_processed, 1)
        self.assertEqual(report.tracks_missing, 1)
        self.assertIn("MISSING ON SPOTIFY", report.warnings[0])

    def test_dry_run_has_no_spotify_or_disk_writes(self):
        release = sample_release()
        spotify = FakeSpotify({"The Waterfront": [candidate()]})
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.db"
            state = StateStore(state_path, readonly=True)
            report = synchronize(
                [release], ["3812903"], spotify, discogs, state, app_config(), dry_run=True
            )
            state.close()
            self.assertFalse(state_path.exists())
        self.assertEqual(spotify.writes, [])
        self.assertEqual(report.playlists_created, 1)
        self.assertEqual(report.tracks_matched, 1)

    def test_cli_dry_run_uses_mocked_apis_without_state_or_spotify_writes(self):
        spotify = FakeSpotify({"The Waterfront": [candidate()]})
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "discogs2spotify.conf"
            state_path = Path(directory) / "state.db"
            config_path.write_text(
                "DISCOGS_USER=bradz667\n"
                "STATE_FILE={}\n"
                "DRY_RUN=true\n"
                "API_DELAY=0\n".format(state_path),
                encoding="utf-8",
            )
            output = StringIO()
            with mock.patch.object(main_module, "DiscogsClient", return_value=discogs), \
                    mock.patch.object(main_module, "SpotifyClient", return_value=spotify), \
                    mock.patch.object(main_module, "read_discogs_token", return_value="fake"), \
                    redirect_stdout(output):
                exit_code = main_module.main(["--config", str(config_path)])
            self.assertFalse(state_path.exists())
        self.assertEqual(exit_code, 0)
        self.assertEqual(spotify.writes, [])
        self.assertIn("NO CHANGES MADE", output.getvalue())

    def test_real_sync_then_repeat_does_not_repeat_spotify_writes(self):
        release = sample_release()
        spotify = FakeSpotify({"The Waterfront": [candidate()]})
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            first = synchronize(
                [release], ["3812903"], spotify, discogs, state, app_config(), dry_run=False
            )
            writes_after_first = list(spotify.writes)
            second = synchronize(
                [release], ["3812903"], spotify, discogs, state, app_config(), dry_run=False
            )
            state.close()
        self.assertEqual(first.playlists_created, 1)
        self.assertEqual(second.playlists_unchanged, 1)
        self.assertEqual(spotify.writes, writes_after_first)

    def test_missing_tracks_reuse_previous_mapping_without_clearing_playlist(self):
        release = sample_release()
        spotify = FakeSpotify({"The Waterfront": [candidate()]})
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            synchronize(
                [release], ["3812903"], spotify, discogs, state,
                app_config(), dry_run=False,
            )
            previous_writes = list(spotify.writes)
            spotify.search_results = {"The Waterfront": []}
            report = synchronize(
                [release], ["3812903"], spotify, discogs, state,
                app_config(), dry_run=False,
            )
            state.close()
        self.assertEqual(report.tracks_missing, 1)
        self.assertEqual(spotify.writes, previous_writes)
        self.assertEqual(
            spotify.playlists["created-1"]["_tracks"],
            [{"uri": "spotify:track:spotify-1"}],
        )

    def test_orphan_detection_and_deletion_are_safe_by_default(self):
        spotify = FakeSpotify(
            playlists={
                "managed-1": {
                    "id": "managed-1",
                    "name": "Old record",
                    "description": "Discogs release: 99",
                    "_tracks": [{"uri": "spotify:track:old"}],
                    "images": [],
                }
            }
        )
        discogs = FakeDiscogs()
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            state.record_release("99", "2", "Rock", "managed-1", "Old record")
            state.close()

            state = self._state(directory, readonly=True)
            report = synchronize(
                [], [], spotify, discogs, state, app_config(), dry_run=True
            )
            state.close()
        self.assertEqual(report.playlists_orphaned, 1)
        self.assertEqual(spotify.writes, [])
        self.assertEqual(len(spotify.playlists["managed-1"]["_tracks"]), 1)

    def test_duplicate_managed_playlists_are_not_modified(self):
        spotify = FakeSpotify(
            playlists={
                "one": {"id": "one", "description": "Discogs release: 3812903", "name": "x"},
                "two": {"id": "two", "description": "Discogs release: 3812903", "name": "x"},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory, readonly=True)
            report = synchronize(
                [sample_release()], ["3812903"], spotify, FakeDiscogs(),
                state, app_config(), dry_run=False,
            )
            state.close()
        self.assertEqual(report.other_errors, 1)
        self.assertEqual(spotify.writes, [])

    def test_unmanaged_same_name_playlist_is_not_modified_or_duplicated(self):
        release = sample_release()
        name = "John Lee Hooker_The Real Folk Blues_France_1980"
        spotify = FakeSpotify(
            playlists={
                "unmanaged": {
                    "id": "unmanaged",
                    "name": name,
                    "description": "A personal playlist",
                    "images": [],
                    "_tracks": [],
                }
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory, readonly=True)
            report = synchronize(
                [release], ["3812903"], spotify, FakeDiscogs(),
                state, app_config(), dry_run=False,
            )
            state.close()
        self.assertEqual(report.other_errors, 1)
        self.assertEqual(spotify.writes, [])
        self.assertEqual(set(spotify.playlists), {"unmanaged"})

    def test_orphan_cleanup_requires_config_and_keeps_playlist(self):
        spotify = FakeSpotify(
            playlists={
                "managed-2": {
                    "id": "managed-2",
                    "name": "Old record",
                    "description": "Discogs release: 99",
                    "_tracks": [{"uri": "spotify:track:old"}],
                    "images": [],
                }
            }
        )
        config = app_config()
        config["SYNC_DELETIONS"] = True
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory)
            state.record_release("99", "2", "Rock", "managed-2", "Old record")
            report = synchronize(
                [], [], spotify, FakeDiscogs(), state, config, dry_run=False
            )
            state.close()
        self.assertEqual(report.playlists_orphaned, 1)
        self.assertEqual(spotify.playlists["managed-2"]["_tracks"], [])
        self.assertIn("managed-2", spotify.playlists)

    def test_unusable_artwork_does_not_replace_cover_or_abort_track_planning(self):
        release = sample_release()
        playlist = {
            "id": "managed-3",
            "name": "managed",
            "description": "Discogs release: 3812903",
            "images": [{"url": "existing-cover"}],
            "_tracks": [],
        }
        spotify = FakeSpotify(
            {"The Waterfront": [candidate()]}, {"managed-3": playlist}
        )
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory, readonly=True)
            report = synchronize(
                [release], ["3812903"], spotify, FakeDiscogs(b"not an image"),
                state, app_config(), dry_run=True,
            )
            state.close()
        self.assertEqual(report.releases_processed, 1)
        self.assertEqual(report.plans[0].cover_status, "MISSING")
        self.assertEqual(spotify.writes, [])

    def test_release_without_artwork_does_not_replace_existing_cover(self):
        release = sample_release()
        release.raw["images"] = []
        playlist = {
            "id": "managed-4",
            "name": "managed",
            "description": "Discogs release: 3812903",
            "images": [{"url": "existing-cover"}],
            "_tracks": [],
        }
        spotify = FakeSpotify(
            {"The Waterfront": [candidate()]}, {"managed-4": playlist}
        )
        with tempfile.TemporaryDirectory() as directory:
            state = self._state(directory, readonly=True)
            report = synchronize(
                [release], ["3812903"], spotify, FakeDiscogs(),
                state, app_config(), dry_run=True,
            )
            state.close()
        self.assertEqual(report.plans[0].cover_status, "MISSING")
        self.assertEqual(spotify.writes, [])
        self.assertEqual(playlist["images"], [{"url": "existing-cover"}])


class ArtworkTests(unittest.TestCase):
    def test_jpeg_and_base64_payload_limit(self):
        jpeg = make_spotify_jpeg(png_bytes())
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertLessEqual(len(base64.b64encode(jpeg)), IMAGE_LIMIT)


class OAuthTests(unittest.TestCase):
    def test_refresh_retains_existing_refresh_token_and_scope_is_minimal(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SpotifyTokenManager(config_dir=directory)
            manager._read_token = lambda: {
                "access_token": "expired",
                "refresh_token": "keep-this-refresh-token",
                "scope": " ".join(BASE_SCOPES),
                "expires_at": 1,
            }
            manager._client_id = lambda: "client-id"
            saved = []
            manager._save_token = saved.append
            manager._post_token = lambda values: {
                "access_token": "fresh-access-token",
                "expires_in": 3600,
            }
            self.assertEqual(manager.access_token(), "fresh-access-token")
            self.assertEqual(saved[0]["refresh_token"], "keep-this-refresh-token")
        self.assertEqual(
            set(BASE_SCOPES),
            {"playlist-read-private", "playlist-modify-private", "ugc-image-upload"},
        )

    def test_public_playlist_setting_requires_its_scope(self):
        manager = SpotifyTokenManager(public_playlists=True)
        with self.assertRaisesRegex(SyncError, "playlist-modify-public"):
            manager._check_scopes(
                {"scope": "playlist-read-private playlist-modify-private ugc-image-upload"}
            )

    def test_token_file_is_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = SpotifyTokenManager(config_dir=directory)
            manager._save_token({"access_token": "example"})
            mode = os.stat(manager.token_path).st_mode & 0o777
            self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
