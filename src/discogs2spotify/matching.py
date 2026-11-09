import re
import unicodedata
from difflib import SequenceMatcher

from .models import TrackCandidate, TrackMatch


_VERSION_PATTERNS = {
    "live": re.compile(r"\b(live|concert|in concert)\b", re.I),
    "remix": re.compile(r"\b(remix|remixed)\b", re.I),
    "instrumental": re.compile(r"\binstrumental\b", re.I),
    "karaoke": re.compile(r"\bkaraoke\b", re.I),
    "demo": re.compile(r"\bdemo\b", re.I),
    "alternate": re.compile(r"\b(alternate|alternative|outtake)\b", re.I),
}


def normalize_text(value):
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"\b(feat|featuring|ft)\.?\b", " ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _similarity(left, right):
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return None
    seq = SequenceMatcher(None, a, b).ratio()
    at, bt = set(a.split()), set(b.split())
    token = 2 * len(at & bt) / float(len(at) + len(bt)) if at or bt else 0
    return max(seq, token)


def _versions(value):
    text = str(value or "")
    return {name for name, pattern in _VERSION_PATTERNS.items() if pattern.search(text)}


def score_candidate(track, candidate):
    track_versions = _versions("{} {}".format(track.title, track.album))
    candidate_versions = _versions("{} {}".format(candidate.title, candidate.album))
    hard_mismatch = (candidate_versions ^ track_versions) & set(_VERSION_PATTERNS)
    if hard_mismatch:
        return None, "incompatible version: {}".format(", ".join(sorted(hard_mismatch)))

    components = [
        (_similarity(track.title, candidate.title), 0.50),
        (_similarity(track.artist, candidate.artist), 0.35),
        (_similarity(track.album, candidate.album), 0.10),
    ]
    year_score = None
    if track.year and candidate.year:
        year_score = max(0.0, 1.0 - abs(track.year - candidate.year) / 20.0)
    components.append((year_score, 0.05))
    available = [(score, weight) for score, weight in components if score is not None]
    if not available:
        return 0.0, "no comparable metadata"
    score = 100.0 * sum(value * weight for value, weight in available) / sum(
        weight for _, weight in available
    )
    if _similarity(track.artist, candidate.artist) is not None and _similarity(
        track.artist, candidate.artist
    ) < 0.45:
        return score, "unrelated artist"
    return score, ""


def match_track(track, candidates, threshold=90, ambiguous_action="skip"):
    scored = []
    rejected_reasons = []
    for candidate in candidates:
        score, reason = score_candidate(track, candidate)
        if reason:
            rejected_reasons.append(reason)
        if score is not None and not reason:
            scored.append((score, candidate))
    if not scored:
        status = "REJECTED" if rejected_reasons else "MISSING"
        reason = rejected_reasons[0] if rejected_reasons else "no acceptable Spotify result"
        return TrackMatch(status=status, reason=reason, candidate_count=len(candidates))

    scored.sort(key=lambda item: (-item[0], item[1].spotify_id))
    best_score, best = scored[0]
    if best_score < threshold:
        return TrackMatch(
            status="MISSING",
            score=best_score,
            reason="best candidate below threshold {:.1f}".format(threshold),
            candidate_count=len(candidates),
        )
    if (
        len(scored) > 1
        and scored[1][0] >= threshold
        and best_score - scored[1][0] < 3.0
        and scored[1][1].spotify_id != best.spotify_id
    ):
        if ambiguous_action == "best":
            return TrackMatch(
                "MATCHED", best.spotify_id, best.uri, best_score,
                "ambiguous; selected best by configuration", len(candidates)
            )
        return TrackMatch(
            status="AMBIGUOUS",
            score=best_score,
            reason="multiple candidates have similar scores",
            candidate_count=len(candidates),
        )
    return TrackMatch(
        status="MATCHED",
        spotify_id=best.spotify_id,
        uri=best.uri,
        score=best_score,
        reason="normalized similarity",
        candidate_count=len(candidates),
    )


def candidate_from_spotify(item):
    if not item:
        return None
    album = item.get("album") or {}
    artists = item.get("artists") or []
    release_date = str(album.get("release_date") or "")
    year = None
    if release_date[:4].isdigit():
        year = int(release_date[:4])
    spotify_id = item.get("id")
    uri = item.get("uri")
    if not spotify_id or not uri:
        return None
    return TrackCandidate(
        spotify_id=str(spotify_id),
        uri=str(uri),
        title=str(item.get("name") or ""),
        artist=", ".join(str(artist.get("name") or "") for artist in artists),
        album=str(album.get("name") or ""),
        year=year,
        raw=item,
    )
