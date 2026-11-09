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
    last_sync TEXT,
    sync_complete INTEGER NOT NULL DEFAULT 0,
    sync_signature TEXT
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
    cache_signature TEXT,
    PRIMARY KEY (discogs_release_id, track_key)
);
CREATE INDEX IF NOT EXISTS track_mappings_spotify_track
ON track_mappings(spotify_track_id);
CREATE TABLE IF NOT EXISTS sync_progress (
    run_id INTEGER PRIMARY KEY CHECK (run_id = 1),
    status TEXT NOT NULL,
    reason TEXT,
    current_release_id TEXT,
    retry_after REAL,
    retry_at TEXT,
    completed_releases INTEGER NOT NULL DEFAULT 0,
    pending_releases INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
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
            self._migrate()
            self.connection.commit()

    def _table_exists(self, name):
        return self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _column_exists(self, table, column):
        return any(
            row[1] == column
            for row in self.connection.execute(
                "PRAGMA table_info({})".format(table)
            ).fetchall()
        )

    def _migrate(self):
        for table, definition in (
            ("managed_releases", "sync_complete INTEGER NOT NULL DEFAULT 0"),
            ("managed_releases", "sync_signature TEXT"),
            ("track_mappings", "cache_signature TEXT"),
        ):
            name = definition.split()[0]
            if not self._column_exists(table, name):
                self.connection.execute(
                    "ALTER TABLE {} ADD COLUMN {}".format(table, definition)
                )

    def managed_releases(self):
        if not self._table_exists("managed_releases"):
            return []
        complete = (
            "sync_complete" if self._column_exists("managed_releases", "sync_complete")
            else "0"
        )
        signature = (
            "sync_signature" if self._column_exists("managed_releases", "sync_signature")
            else "NULL"
        )
        rows = self.connection.execute(
            """
            SELECT discogs_release_id, discogs_folder_id, discogs_folder_name,
                   spotify_playlist_id, playlist_name, description_hash,
                   artwork_hash, last_sync, {}, {}
            FROM managed_releases ORDER BY discogs_release_id
            """.format(complete, signature)
        ).fetchall()
        keys = (
            "release_id", "folder_id", "folder_name", "playlist_id",
            "playlist_name", "description_hash", "artwork_hash", "last_sync",
            "sync_complete", "sync_signature",
        )
        return [dict(zip(keys, row)) for row in rows]

    def get_release(self, release_id):
        if not self._table_exists("managed_releases"):
            return None
        complete = (
            "sync_complete" if self._column_exists("managed_releases", "sync_complete")
            else "0"
        )
        signature = (
            "sync_signature" if self._column_exists("managed_releases", "sync_signature")
            else "NULL"
        )
        row = self.connection.execute(
            """
            SELECT discogs_release_id, discogs_folder_id, discogs_folder_name,
                   spotify_playlist_id, playlist_name, description_hash,
                   artwork_hash, last_sync, {}, {}
            FROM managed_releases WHERE discogs_release_id=?
            """.format(complete, signature),
            (str(release_id),),
        ).fetchone()
        if row is None:
            return None
        keys = (
            "release_id", "folder_id", "folder_name", "playlist_id",
            "playlist_name", "description_hash", "artwork_hash", "last_sync",
            "sync_complete", "sync_signature",
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
                artwork_hash, last_sync, sync_complete, sync_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), 0, NULL)
            ON CONFLICT(discogs_release_id) DO UPDATE SET
                discogs_folder_id=excluded.discogs_folder_id,
                discogs_folder_name=excluded.discogs_folder_name,
                spotify_playlist_id=excluded.spotify_playlist_id,
                playlist_name=excluded.playlist_name,
                description_hash=COALESCE(excluded.description_hash,
                                          managed_releases.description_hash),
                artwork_hash=COALESCE(excluded.artwork_hash,
                                      managed_releases.artwork_hash),
                sync_complete=0,
                sync_signature=NULL,
                last_sync=datetime('now')
            """,
            (
                str(release_id), str(folder_id), folder_name, str(playlist_id),
                playlist_name, description_hash, artwork_hash,
            ),
        )
        self.connection.commit()

    def complete_release(self, release_id, sync_signature):
        self._write_check()
        self.connection.execute(
            """
            UPDATE managed_releases
            SET sync_complete=1, sync_signature=?, last_sync=datetime('now')
            WHERE discogs_release_id=?
            """,
            (sync_signature, str(release_id)),
        )
        self.connection.commit()

    def is_release_complete(self, release_id, sync_signature):
        stored = self.get_release(release_id)
        return bool(
            stored
            and stored.get("sync_complete")
            and stored.get("sync_signature") == sync_signature
        )

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
        cache_signature = (
            "cache_signature" if self._column_exists("track_mappings", "cache_signature")
            else "NULL"
        )
        rows = self.connection.execute(
            """
            SELECT track_key, track_position, track_title, track_artist,
                   spotify_track_id, spotify_track_uri, match_method,
                   match_score, status, {}
            FROM track_mappings WHERE discogs_release_id=?
            """.format(cache_signature),
            (str(release_id),),
        ).fetchall()
        keys = (
            "track_key", "position", "title", "artist", "spotify_id",
            "spotify_uri", "match_method", "score", "status", "cache_signature",
        )
        return {row[0]: dict(zip(keys, row)) for row in rows}

    def record_track(self, release_id, track, match, cache_signature):
        self._write_check()
        self.connection.execute(
            """
            INSERT INTO track_mappings (
                discogs_release_id, track_key, track_position, track_title,
                track_artist, spotify_track_id, spotify_track_uri,
                match_method, match_score, status, cache_signature
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discogs_release_id, track_key) DO UPDATE SET
                track_position=excluded.track_position,
                track_title=excluded.track_title,
                track_artist=excluded.track_artist,
                spotify_track_id=excluded.spotify_track_id,
                spotify_track_uri=excluded.spotify_track_uri,
                match_method=excluded.match_method,
                match_score=excluded.match_score,
                status=excluded.status,
                cache_signature=excluded.cache_signature
            """,
            (
                str(release_id), track.key, track.position, track.title,
                track.artist, match.spotify_id, match.uri, match.reason,
                match.score, match.status, cache_signature,
            ),
        )
        self.connection.commit()

    def record_tracks(self, release_id, tracks, matches, cache_signatures=None):
        self._write_check()
        previous = self.tracks(release_id)
        cache_signatures = cache_signatures or [None] * len(tracks)
        self.connection.execute(
            "DELETE FROM track_mappings WHERE discogs_release_id=?", (str(release_id),)
        )
        for track, match, cache_signature in zip(tracks, matches, cache_signatures):
            old = previous.get(track.key, {})
            spotify_id = match.spotify_id
            spotify_uri = match.uri
            if match.status != "MATCHED" and old.get("spotify_uri"):
                spotify_id = old.get("spotify_id")
                spotify_uri = old.get("spotify_uri")
                cache_signature = old.get("cache_signature")
            self.connection.execute(
                """
                INSERT INTO track_mappings (
                    discogs_release_id, track_key, track_position, track_title,
                    track_artist, spotify_track_id, spotify_track_uri,
                    match_method, match_score, status, cache_signature
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(release_id), track.key, track.position, track.title,
                    track.artist, spotify_id, spotify_uri, match.reason,
                    match.score, match.status, cache_signature,
                ),
            )
        self.connection.commit()

    def update_sync_progress(
        self,
        status,
        reason=None,
        current_release_id=None,
        retry_after=None,
        retry_at=None,
        completed_releases=0,
        pending_releases=0,
    ):
        self._write_check()
        self.connection.execute(
            """
            INSERT INTO sync_progress (
                run_id, status, reason, current_release_id, retry_after,
                retry_at, completed_releases, pending_releases, updated_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(run_id) DO UPDATE SET
                status=excluded.status,
                reason=excluded.reason,
                current_release_id=excluded.current_release_id,
                retry_after=excluded.retry_after,
                retry_at=excluded.retry_at,
                completed_releases=excluded.completed_releases,
                pending_releases=excluded.pending_releases,
                updated_at=datetime('now')
            """,
            (
                status, reason, current_release_id, retry_after, retry_at,
                completed_releases, pending_releases,
            ),
        )
        self.connection.commit()

    def sync_progress(self):
        if not self._table_exists("sync_progress"):
            return None
        row = self.connection.execute(
            """
            SELECT status, reason, current_release_id, retry_after, retry_at,
                   completed_releases, pending_releases, updated_at
            FROM sync_progress WHERE run_id=1
            """
        ).fetchone()
        if row is None:
            return None
        keys = (
            "status", "reason", "current_release_id", "retry_after", "retry_at",
            "completed_releases", "pending_releases", "updated_at",
        )
        return dict(zip(keys, row))

    def _write_check(self):
        if self.readonly:
            raise RuntimeError("Cannot write to the synchronization database in dry-run mode")

    def close(self):
        self.connection.close()
