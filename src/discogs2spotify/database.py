import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS managed_releases (
    discogs_release_id TEXT PRIMARY KEY,
    discogs_folder_id TEXT NOT NULL,
    discogs_folder_name TEXT NOT NULL,
    spotify_playlist_id TEXT NOT NULL UNIQUE,
    playlist_name TEXT NOT NULL,
    description_hash TEXT,
    artwork_hash TEXT,
    last_sync TEXT
);
CREATE TABLE IF NOT EXISTS track_mappings (
    discogs_release_id TEXT NOT NULL,
    track_key TEXT NOT NULL,
    track_position TEXT NOT NULL,
    track_title TEXT NOT NULL,
    track_artist TEXT NOT NULL,
    spotify_track_id TEXT,
    spotify_track_uri TEXT,
    match_method TEXT NOT NULL,
    match_score REAL,
    status TEXT NOT NULL,
    PRIMARY KEY (discogs_release_id, track_key)
);
CREATE INDEX IF NOT EXISTS track_mappings_spotify_track
ON track_mappings(spotify_track_id);
"""


class StateStore:
    def __init__(self, path, readonly=False):
        self.path = Path(path).expanduser()
        self.readonly = readonly
        if readonly and self.path.exists():
            uri = self.path.resolve().as_uri() + "?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True)
        elif readonly:
            self.connection = sqlite3.connect(":memory:")
            self.connection.executescript(SCHEMA)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(str(self.path))
            self.connection.executescript(SCHEMA)
            self.connection.commit()

    def _table_exists(self, name):
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def managed_releases(self):
        if not self._table_exists("managed_releases"):
            return []
        rows = self.connection.execute(
            """
            SELECT discogs_release_id, discogs_folder_id, discogs_folder_name,
                   spotify_playlist_id, playlist_name, description_hash,
                   artwork_hash, last_sync
            FROM managed_releases ORDER BY discogs_release_id
            """
        ).fetchall()
        keys = (
            "release_id", "folder_id", "folder_name", "playlist_id",
            "playlist_name", "description_hash", "artwork_hash", "last_sync",
        )
        return [dict(zip(keys, row)) for row in rows]

    def get_release(self, release_id):
        if not self._table_exists("managed_releases"):
            return None
        row = self.connection.execute(
            """
            SELECT discogs_release_id, discogs_folder_id, discogs_folder_name,
                   spotify_playlist_id, playlist_name, description_hash,
                   artwork_hash, last_sync
            FROM managed_releases WHERE discogs_release_id=?
            """,
            (str(release_id),),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "release_id", "folder_id", "folder_name", "playlist_id",
            "playlist_name", "description_hash", "artwork_hash", "last_sync",
        )
        return dict(zip(keys, row))

    def record_release(
        self,
        release_id,
        folder_id,
        folder_name,
        playlist_id,
        playlist_name,
        description_hash=None,
        artwork_hash=None,
    ):
        self._write_check()
        self.connection.execute(
            """
            INSERT INTO managed_releases (
                discogs_release_id, discogs_folder_id, discogs_folder_name,
                spotify_playlist_id, playlist_name, description_hash,
                artwork_hash, last_sync
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(discogs_release_id) DO UPDATE SET
                discogs_folder_id=excluded.discogs_folder_id,
                discogs_folder_name=excluded.discogs_folder_name,
                spotify_playlist_id=excluded.spotify_playlist_id,
                playlist_name=excluded.playlist_name,
                description_hash=COALESCE(excluded.description_hash,
                                          managed_releases.description_hash),
                artwork_hash=COALESCE(excluded.artwork_hash,
                                      managed_releases.artwork_hash),
                last_sync=datetime('now')
            """,
            (
                str(release_id), str(folder_id), folder_name, str(playlist_id),
                playlist_name, description_hash, artwork_hash,
            ),
        )
        self.connection.commit()

    def update_hashes(self, release_id, description_hash=None, artwork_hash=None):
        self._write_check()
        self.connection.execute(
            """
            UPDATE managed_releases
            SET description_hash=COALESCE(?, description_hash),
                artwork_hash=COALESCE(?, artwork_hash),
                last_sync=datetime('now')
            WHERE discogs_release_id=?
            """,
            (description_hash, artwork_hash, str(release_id)),
        )
        self.connection.commit()

    def tracks(self, release_id):
        if not self._table_exists("track_mappings"):
            return {}
        rows = self.connection.execute(
            """
            SELECT track_key, track_position, track_title, track_artist,
                   spotify_track_id, spotify_track_uri, match_method,
                   match_score, status
            FROM track_mappings WHERE discogs_release_id=?
            """,
            (str(release_id),),
        ).fetchall()
        keys = (
            "track_key", "position", "title", "artist", "spotify_id",
            "spotify_uri", "match_method", "score", "status",
        )
        return {row[0]: dict(zip(keys, row)) for row in rows}

    def record_tracks(self, release_id, tracks, matches):
        self._write_check()
        self.connection.execute(
            "DELETE FROM track_mappings WHERE discogs_release_id=?", (str(release_id),)
        )
        for track, match in zip(tracks, matches):
            self.connection.execute(
                """
                INSERT INTO track_mappings (
                    discogs_release_id, track_key, track_position, track_title,
                    track_artist, spotify_track_id, spotify_track_uri,
                    match_method, match_score, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(release_id), track.key, track.position, track.title,
                    track.artist, match.spotify_id, match.uri, match.reason,
                    match.score, match.status,
                ),
            )
        self.connection.commit()

    def _write_check(self):
        if self.readonly:
            raise RuntimeError("Cannot write to the synchronization database in dry-run mode")

    def close(self):
        self.connection.close()
