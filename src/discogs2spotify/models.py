from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class DiscogsTrack:
    release_id: str
    position: str
    ordinal: int
    title: str
    artist: str
    album: str
    year: Optional[int] = None

    @property
    def key(self):
        return "{}:{}".format(self.position, self.ordinal)


@dataclass
class DiscogsRelease:
    release_id: str
    folder_id: str
    folder_name: str
    artist: str
    title: str
    country: str
    year: Optional[int]
    tracks: List[DiscogsTrack] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackCandidate:
    spotify_id: str
    uri: str
    title: str
    artist: str
    album: str
    year: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TrackMatch:
    status: str
    spotify_id: Optional[str] = None
    uri: Optional[str] = None
    score: float = 0.0
    reason: str = ""
    candidate_count: int = 0


@dataclass
class ReleasePlan:
    release: DiscogsRelease
    playlist_name: str
    playlist_id: Optional[str]
    playlist_action: str
    tracks: List[DiscogsTrack]
    matches: List[TrackMatch]
    desired_uris: List[str]
    current_uris: List[str]
    description: str
    description_hash: str
    artwork_hash: Optional[str] = None
    artwork_bytes: Optional[bytes] = None
    artwork_jpeg: Optional[bytes] = None
    cover_status: str = "MISSING"
    description_status: str = "UNCHANGED"
    duplicate_track_matches: int = 0
    duplicate_playlist_ids: List[str] = field(default_factory=list)
    unmanaged_name_collisions: List[str] = field(default_factory=list)
    error: Optional[str] = None
    cache_signatures: List[str] = field(default_factory=list)


@dataclass
class SyncReport:
    releases_total: int = 0
    releases_processed: int = 0
    releases_completed: int = 0
    releases_pending: int = 0
    releases_skipped: int = 0
    playlists_created: int = 0
    playlists_updated: int = 0
    playlists_unchanged: int = 0
    playlists_orphaned: int = 0
    tracks_matched: int = 0
    tracks_missing: int = 0
    tracks_ambiguous: int = 0
    tracks_rejected: int = 0
    duplicate_tracks: int = 0
    tracks_searched: int = 0
    tracks_resolved_from_cache: int = 0
    spotify_api_requests: int = 0
    http_429_responses: int = 0
    rate_limit_pauses: int = 0
    quota_exhaustion_pauses: int = 0
    description_updates: int = 0
    artwork_updates: int = 0
    skipped_operations: int = 0
    api_errors: int = 0
    other_errors: int = 0
    sync_paused: bool = False
    pause_reason: str = ""
    current_release_id: Optional[str] = None
    retry_after: Optional[float] = None
    retry_at: Optional[str] = None
    limit_reached: bool = False
    plans: List[ReleasePlan] = field(default_factory=list)
    orphaned: List[Dict[str, str]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
