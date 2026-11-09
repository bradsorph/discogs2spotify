import base64
import hashlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from discogs2spotify import (
    DESCRIPTION_DEFAULTS,
    SPOTIFY_IMAGE_LIMIT,
    StateStore,
    generate_description,
    make_spotify_jpeg,
    synchronize_playlist,
)


class FakeDiscogs:
    def __init__(self, release, artwork):
        self.release = release
        self.artwork = artwork
        self.requested_release = None
        self.requested_image = None

    def get_release(self, release_id):
        self.requested_release = str(release_id)
        return self.release

    def get_image(self, image_url):
        self.requested_image = image_url
        return self.artwork


class FakeSpotify:
    def __init__(self, description="", images=None):
        self.playlist = {
            "name": "Test playlist",
            "description": description,
            "images": images or [],
        }
        self.description_updates = []
        self.cover_updates = []

    def get_playlist(self, playlist_id):
        return self.playlist

    def update_description(self, playlist_id, description):
        self.description_updates.append(description)
        self.playlist["description"] = description

    def update_cover(self, playlist_id, jpeg_base64):
        self.cover_updates.append(jpeg_base64)
        self.playlist["images"] = [{"url": "custom-cover"}]


def image_bytes():
    image = Image.new("RGB", (1800, 1800), (12, 72, 140))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def settings():
    return dict(DESCRIPTION_DEFAULTS, ARTWORK_IMAGE_INDEX="primary")


class DescriptionTests(unittest.TestCase):
    def test_description_includes_available_catalog_metadata_and_release_url(self):
        release = {
            "labels": [{"name": "Chess", "catno": "515009"}],
            "country": "France",
            "year": 1980,
            "formats": [{"name": "LP", "descriptions": ["Album", "Reissue", "Stereo"]}],
            "genres": ["Blues"],
            "styles": ["Country Blues", "Chicago Blues"],
            "notes": "Made in France\n℗ & © 1966",
        }
        description = generate_description(release, 3812903, settings())
        self.assertIn("Chess · 515009", description)
        self.assertIn("France · 1980", description)
        self.assertIn("LP · Album, Reissue, Stereo", description)
        self.assertIn("Blues · Country Blues, Chicago Blues", description)
        self.assertIn("Discogs release: 3812903", description)
        self.assertIn("https://www.discogs.com/release/3812903", description)
        self.assertLessEqual(len(description), 300)

    def test_optional_fields_are_omitted_when_disabled(self):
        configured = settings()
        configured["DESCRIPTION_INCLUDE_CATALOG_NUMBER"] = False
        configured["DESCRIPTION_INCLUDE_NOTES"] = False
        configured["DESCRIPTION_INCLUDE_DISCOGS_URL"] = False
        description = generate_description(
            {"labels": [{"name": "Chess", "catno": "515009"}], "notes": "Private text"},
            7,
            configured,
        )
        self.assertEqual(description, "Chess")
        self.assertNotIn("Private text", description)
        self.assertNotIn("515009", description)


class ArtworkTests(unittest.TestCase):
    def test_processed_artwork_is_jpeg_and_within_base64_limit(self):
        jpeg = make_spotify_jpeg(image_bytes())
        self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertLessEqual(len(base64.b64encode(jpeg)), SPOTIFY_IMAGE_LIMIT)


class SynchronizationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state = StateStore(Path(self.temp_dir.name) / "state.sqlite3")
        self.addCleanup(self.state.close)
        self.release = {
            "images": [
                {"type": "secondary", "uri": "https://img.example/secondary.png"},
                {"type": "primary", "uri": "https://img.example/exact-release.png"},
            ],
            "labels": [{"name": "Chess", "catno": "515009"}],
        }

    def test_first_sync_then_repeat_uses_hashes_and_primary_release_image(self):
        artwork = image_bytes()
        discogs = FakeDiscogs(self.release, artwork)
        spotify = FakeSpotify()
        first = synchronize_playlist(
            spotify, discogs, self.state, "playlist-1", "3812903", settings()
        )
        self.assertEqual(first["COVER"], "CREATED")
        self.assertEqual(first["DESCRIPTION"], "CREATED")
        self.assertEqual(discogs.requested_release, "3812903")
        self.assertEqual(discogs.requested_image, "https://img.example/exact-release.png")
        self.assertEqual(len(spotify.cover_updates), 1)

        second = synchronize_playlist(
            spotify, discogs, self.state, "playlist-1", "3812903", settings()
        )
        self.assertEqual(second["COVER"], "UNCHANGED")
        self.assertEqual(second["DESCRIPTION"], "UNCHANGED")
        self.assertEqual(len(spotify.cover_updates), 1)
        stored = self.state.get("playlist-1", "3812903")
        self.assertEqual(stored["artwork_hash"], hashlib.sha256(artwork).hexdigest())
        self.assertIsNotNone(stored["description_hash"])

    def test_missing_release_image_does_not_replace_spotify_cover(self):
        release = {"images": []}
        discogs = FakeDiscogs(release, b"")
        spotify = FakeSpotify(description="Existing description", images=[{"url": "old"}])
        result = synchronize_playlist(
            spotify, discogs, self.state, "playlist-2", "42", settings()
        )
        self.assertEqual(result["COVER"], "MISSING")
        self.assertEqual(spotify.cover_updates, [])
        self.assertEqual(spotify.playlist["images"], [{"url": "old"}])

    def test_unusable_release_image_is_reported_missing_without_replacement(self):
        discogs = FakeDiscogs(self.release, b"not an image")
        spotify = FakeSpotify(description="Existing description", images=[{"url": "old"}])
        result = synchronize_playlist(
            spotify, discogs, self.state, "playlist-4", "42", settings()
        )
        self.assertEqual(result["COVER"], "MISSING")
        self.assertEqual(spotify.cover_updates, [])
        self.assertEqual(spotify.playlist["images"], [{"url": "old"}])

    def test_changed_release_artwork_and_description_are_updated(self):
        discogs = FakeDiscogs(self.release, image_bytes())
        spotify = FakeSpotify()
        synchronize_playlist(
            spotify, discogs, self.state, "playlist-3", "42", settings()
        )

        updated_release = dict(self.release)
        updated_release["labels"] = [{"name": "Chess", "catno": "515010"}]
        discogs.release = updated_release
        discogs.artwork = image_bytes() + b"different-source"
        result = synchronize_playlist(
            spotify, discogs, self.state, "playlist-3", "42", settings()
        )
        self.assertEqual(result["COVER"], "UPDATED")
        self.assertEqual(result["DESCRIPTION"], "UPDATED")
        self.assertEqual(len(spotify.cover_updates), 2)


if __name__ == "__main__":
    unittest.main()
