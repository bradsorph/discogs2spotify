import logging
import hashlib
import json
import math
from .artwork import UnusableArtwork, make_spotify_jpeg, select_release_image
from .errors import APIError, SyncError
from .matching import match_track, normalize_text, score_album_candidate
from .metadata import (
    discogs_marker,
    generate_description,
    playlist_name,
    playlist_names,
    stable_hash,
)
from .models import ReleasePlan, SyncReport, TrackMatch

LOGGER = logging.getLogger("discogs2spotify")


def _source_signature(track):
    return normalize_text(track.title), normalize_text(track.artist)


def _track_cache_signature(track, settings):
    source = (
        track.title,
        track.artist,
        track.album,
        track.year,
        settings["MATCH_THRESHOLD"],
        settings["AMBIGUOUS_MATCH_ACTION"],
    )
    return hashlib.sha256(
        json.dumps(source, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _album_first_matches(release, tracks, spotify, settings):
    search_albums = getattr(spotify, "search_albums", None)
    get_album_tracks = getattr(spotify, "album_tracks", None)
    if not tracks or not callable(search_albums) or not callable(get_album_tracks):
        return {}, "NO MATCH"

    albums = search_albums(release)
    ranked = []
    for album in albums:
        score, reason = score_album_candidate(release, album)
        if score is not None and not reason:
            ranked.append((score, album))
    ranked.sort(key=lambda item: (-item[0], item[1].spotify_id))
    if not ranked:
        LOGGER.debug(
            "Album match release=%s result=NO_MATCH candidates=%d",
            release.release_id,
            len(albums),
        )
        return {}, "NO MATCH"

    accepted = []
    top_score = ranked[0][0]
    for album_score, album in ranked:
        if accepted and album_score < top_score - 4.0:
            break
        album_tracks = get_album_tracks(album)
        matched_tracks = {}
        for track in tracks:
            match = match_track(
                track,
                album_tracks,
                threshold=settings["MATCH_THRESHOLD"],
                ambiguous_action=settings["AMBIGUOUS_MATCH_ACTION"],
            )
            if match.status == "MATCHED" and match.uri:
                matched_tracks[track.key] = TrackMatch(
                    status=match.status,
                    spotify_id=match.spotify_id,
                    uri=match.uri,
                    score=match.score,
                    reason="album tracklist: " + match.reason,
                    candidate_count=match.candidate_count,
                )

        match_count = len(matched_tracks)
        overlap = match_count / float(len(tracks))
        album_track_count = album.total_tracks or len(album_tracks)
        album_overlap = (
            match_count / float(album_track_count) if album_track_count else 0.0
        )
        minimum_matches = max(1, int(math.ceil(len(tracks) * 0.35)))
        if len(tracks) <= 3 and album_score >= 96.0 and match_count:
            minimum_matches = 1
        if match_count < minimum_matches or album_overlap < 0.2:
            continue

        evidence = (
            album_score * 0.35
            + overlap * 100.0 * 0.5
            + album_overlap * 100.0 * 0.15
        )
        accepted.append((evidence, album_score, album, matched_tracks))

    if not accepted:
        LOGGER.debug(
            "Album match release=%s result=NO_MATCH plausible_candidates=%d",
            release.release_id,
            len(ranked),
        )
        return {}, "NO MATCH"

    accepted.sort(key=lambda item: (-item[0], item[2].spotify_id))
    if len(accepted) > 1 and accepted[0][0] - accepted[1][0] < 3.0:
        LOGGER.debug(
            "Album match release=%s result=AMBIGUOUS candidates=%s",
            release.release_id,
            ",".join(item[2].spotify_id for item in accepted[:2]),
        )
        return {}, "AMBIGUOUS MATCH"

    evidence, album_score, album, matched_tracks = accepted[0]
    status = "FULL MATCH" if len(matched_tracks) == len(tracks) else "PARTIAL MATCH"
    LOGGER.debug(
        "Album match release=%s album=%s artist_title_score=%.1f overlap=%d/%d result=%s evidence=%.1f",
        release.release_id,
        album.spotify_id,
        album_score,
        len(matched_tracks),
        len(tracks),
        status,
        evidence,
    )
    return matched_tracks, status


def _resolve_tracks(
    release, spotify, previous, settings, state, dry_run, report
):
    cached_matches = {}
    pending_tracks = []
    cache_signatures = []
    for track in release.tracks:
        cache_signature = _track_cache_signature(track, settings)
        cache_signatures.append(cache_signature)
        cached = previous.get(track.key, {})
        if (
            cached.get("status") == "MATCHED"
            and cached.get("cache_signature") == cache_signature
            and cached.get("spotify_id")
            and cached.get("spotify_uri")
        ):
            cached_matches[track.key] = TrackMatch(
                "MATCHED",
                cached["spotify_id"],
                cached["spotify_uri"],
                float(cached.get("score") or 0.0),
                cached.get("match_method") or "cached match",
                1,
            )
            report.tracks_resolved_from_cache += 1
        else:
            pending_tracks.append(track)

    album_matches, album_status = _album_first_matches(
        release, pending_tracks, spotify, settings
    )
    matches = []
    uris = []
    source_by_uri = {}
    duplicate_count = 0
    for track, cache_signature in zip(release.tracks, cache_signatures):
        cached = previous.get(track.key, {})
        if track.key in cached_matches:
            match = cached_matches[track.key]
        elif track.key in album_matches:
            match = album_matches[track.key]
            if not dry_run and not state.readonly:
                state.record_track(
                    release.release_id, track, match, cache_signature
                )
        else:
            report.tracks_searched += 1
            candidates = spotify.search_tracks(track)
            match = match_track(
                track,
                candidates,
                threshold=settings["MATCH_THRESHOLD"],
                ambiguous_action=settings["AMBIGUOUS_MATCH_ACTION"],
            )
            if match.status == "MATCHED" and not dry_run and not state.readonly:
                state.record_track(
                    release.release_id, track, match, cache_signature
                )
        LOGGER.debug(
            "Track match release=%s position=%s title=%r candidates=%d result=%s score=%.1f reason=%s",
            track.release_id,
            track.position,
            track.title,
            match.candidate_count,
            match.status,
            match.score,
            match.reason,
        )
        if match.status == "MATCHED" and match.uri:
            signature = _source_signature(track)
            if match.uri in source_by_uri:
                duplicate_count += 1
            if (
                match.uri in source_by_uri
                and source_by_uri[match.uri] != signature
                and settings["DUPLICATE_TRACK_ACTION"] == "skip"
            ):
                duplicate_count += 1
                match = TrackMatch(
                    status="REJECTED",
                    score=match.score,
                    reason="Spotify track already matched to another Discogs track",
                    candidate_count=match.candidate_count,
                )
            else:
                source_by_uri[match.uri] = signature
                uris.append(match.uri)
        if match.status != "MATCHED":
            prior = previous.get(track.key)
            if prior and prior.get("spotify_uri"):
                uris.append(prior["spotify_uri"])
        matches.append(match)

    # Avoid clearing a managed playlist if every search failed transiently or
    # returned no acceptable result for a non-empty Discogs tracklist.
    if (
        release.tracks
        and not any(match.status == "MATCHED" for match in matches)
        and not any(previous.get(track.key, {}).get("spotify_uri") for track in release.tracks)
    ):
        uris = []
    matched_count = sum(match.status == "MATCHED" and bool(match.uri) for match in matches)
    if matches and matched_count == len(matches):
        match_status = "FULL MATCH"
    elif matched_count:
        match_status = "PARTIAL MATCH"
    elif any(match.status == "AMBIGUOUS" for match in matches):
        match_status = "AMBIGUOUS MATCH"
    elif album_status == "AMBIGUOUS MATCH":
        match_status = "AMBIGUOUS MATCH"
    else:
        match_status = "NO MATCH"
    return matches, uris, duplicate_count, cache_signatures, match_status


def _image_url(release, config):
    image = select_release_image(
        release.raw.get("images") or [], config.get("ARTWORK_IMAGE_INDEX", "primary")
    )
    return (image or {}).get("uri") if image else None


def _release_sync_signature(release, config, target_playlist_name=None):
    signature = {
        "release_id": release.release_id,
        "folder_id": release.folder_id,
        "folder_name": release.folder_name,
        "playlist_name": target_playlist_name or playlist_name(
            release, config.get("SPOTIFY_PLAYLIST_ROOT")
        ),
        "description": stable_hash(generate_description(release, config)),
        "artwork_url": _image_url(release, config),
        "tracks": [
            (
                track.key,
                track.position,
                track.title,
                track.artist,
                track.album,
                track.year,
                _track_cache_signature(track, config),
            )
            for track in release.tracks
        ],
    }
    return hashlib.sha256(
        json.dumps(signature, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()


def build_release_plan(
    release, spotify, discogs, state, config, playlists_by_release,
    report, dry_run=True, unmanaged_names=None, target_playlist_name=None,
):
    target_playlist_name = target_playlist_name or playlist_name(
        release, config.get("SPOTIFY_PLAYLIST_ROOT")
    )
    description = generate_description(release, config)
    description_hash = stable_hash(description)
    stored = state.get_release(release.release_id)
    if stored:
        playlist_id = stored["playlist_id"]
        duplicate_ids = playlists_by_release.get(release.release_id, [])
        if playlist_id not in duplicate_ids:
            duplicate_ids = [playlist_id] + duplicate_ids
        playlist_action = "EXIST"
    else:
        duplicate_ids = playlists_by_release.get(release.release_id, [])
        playlist_id = duplicate_ids[0] if len(duplicate_ids) == 1 else None
        playlist_action = "EXIST" if playlist_id else "CREATE"

    name_collisions = (unmanaged_names or {}).get(target_playlist_name, [])
    if not playlist_id and name_collisions:
        return ReleasePlan(
            release=release,
            playlist_name=target_playlist_name,
            playlist_id=None,
            playlist_action="COLLISION",
            tracks=release.tracks,
            matches=[],
            desired_uris=[],
            current_uris=[],
            description=description,
            description_hash=description_hash,
            unmanaged_name_collisions=sorted(set(name_collisions)),
            error="An unmanaged Spotify playlist has the same name",
        )

    if len(set(duplicate_ids)) > 1:
        return ReleasePlan(
            release=release,
            playlist_name=target_playlist_name,
            playlist_id=playlist_id,
            playlist_action="DUPLICATE",
            tracks=release.tracks,
            matches=[],
            desired_uris=[],
            current_uris=[],
            description=description,
            description_hash=description_hash,
            duplicate_playlist_ids=sorted(set(duplicate_ids)),
            error="Multiple Spotify playlists claim this Discogs release",
        )

    playlist = {}
    current_tracks = []
    if playlist_id:
        playlist = spotify.get_playlist(playlist_id)
        current_tracks = spotify.playlist_tracks(playlist_id)
    current_uris = [track["uri"] for track in current_tracks if track.get("uri")]
    previous_tracks = state.tracks(release.release_id)
    (
        matches,
        desired_uris,
        duplicate_tracks,
        cache_signatures,
        match_status,
    ) = _resolve_tracks(release, spotify, previous_tracks, config, state, dry_run, report)
    has_unresolved = any(match.status != "MATCHED" for match in matches)
    unresolved_without_mapping = any(
        match.status != "MATCHED"
        and not previous_tracks.get(track.key, {}).get("spotify_uri")
        for track, match in zip(release.tracks, matches)
    )
    if (
        has_unresolved
        and current_uris
        and unresolved_without_mapping
    ):
        # If a playlist contains unmatched legacy items whose mapping is unknown,
        # preserve the current sequence rather than deleting potentially valid tracks.
        desired_uris = list(current_uris)
    name_status = "CREATED" if not playlist_id else (
        "UPDATED" if playlist.get("name") != target_playlist_name else "UNCHANGED"
    )
    if match_status in ("NO MATCH", "AMBIGUOUS MATCH"):
        return ReleasePlan(
            release=release,
            playlist_name=target_playlist_name,
            playlist_id=playlist_id,
            playlist_action=(
                "AMBIGUOUS" if match_status == "AMBIGUOUS MATCH" else "NO_MATCH"
            ),
            tracks=release.tracks,
            matches=matches,
            desired_uris=desired_uris,
            current_uris=current_uris,
            description=description,
            description_hash=description_hash,
            name_status=name_status,
            match_status=match_status,
            duplicate_track_matches=duplicate_tracks,
            cache_signatures=cache_signatures,
        )
    description_status = "UNCHANGED"
    if not playlist_id:
        description_status = "CREATED"
    elif config["PLAYLIST_DESCRIPTION"]:
        old_hash = stored.get("description_hash") if stored else None
        if old_hash != description_hash and playlist.get("description") != description:
            description_status = "UPDATED" if playlist.get("description") else "CREATED"
        elif old_hash is None and playlist.get("description") == description:
            description_status = "UNCHANGED"
    cover_status = "MISSING"
    artwork_hash = None
    image_url = _image_url(release, config)
    artwork_bytes = None
    artwork_jpeg = None
    if image_url:
        try:
            artwork_bytes = discogs.get_image(image_url)
            if artwork_bytes is not None:
                artwork_hash = hashlib.sha256(artwork_bytes).hexdigest()
                has_cover = bool(playlist.get("images"))
                stored_hash = stored.get("artwork_hash") if stored else None
                if stored_hash == artwork_hash and has_cover:
                    cover_status = "UNCHANGED"
                else:
                    cover_status = "UPDATED" if playlist_id and has_cover else "CREATED"
                    artwork_jpeg = make_spotify_jpeg(artwork_bytes)
        except UnusableArtwork as error:
            LOGGER.warning("%s: %s", release.release_id, error)
            artwork_bytes = None
            artwork_hash = None
            artwork_jpeg = None
            cover_status = "MISSING"

    tracklist_changed = desired_uris != current_uris
    description_changed = description_status in ("CREATED", "UPDATED")
    cover_changed = cover_status in ("CREATED", "UPDATED")
    if playlist_id and (
        name_status == "UPDATED"
        or tracklist_changed
        or description_changed
        or cover_changed
    ):
        playlist_action = "UPDATE"

    return ReleasePlan(
        release=release,
        playlist_name=target_playlist_name,
        playlist_id=playlist_id,
        playlist_action=playlist_action,
        tracks=release.tracks,
        matches=matches,
        desired_uris=desired_uris,
        current_uris=current_uris,
        description=description,
        description_hash=description_hash,
        artwork_hash=artwork_hash,
        artwork_bytes=artwork_bytes,
        artwork_jpeg=artwork_jpeg,
        cover_status=cover_status,
        name_status=name_status,
        match_status=match_status,
        description_status=description_status,
        duplicate_track_matches=duplicate_tracks,
        cache_signatures=cache_signatures,
    )


def _record_track_results(report, plan, config):
    report.duplicate_tracks += plan.duplicate_track_matches
    for track, match in zip(plan.tracks, plan.matches):
        if match.status == "MATCHED":
            report.tracks_matched += 1
        elif match.status == "MISSING":
            report.tracks_missing += 1
            if config["MISSING_TRACK_ACTION"] == "report":
                report.warnings.append(
                    "MISSING ON SPOTIFY | Discogs release: {} | Artist: {} | Track: {} | Reason: {}".format(
                        plan.release.release_id,
                        track.artist,
                        track.title,
                        match.reason,
                    )
                )
        elif match.status == "AMBIGUOUS":
            report.tracks_ambiguous += 1
        elif match.status == "REJECTED":
            report.tracks_rejected += 1


def discover_managed_playlists(spotify, state, verbose=False):
    managed = {}
    known_ids = set()
    for record in state.managed_releases():
        managed.setdefault(record["release_id"], []).append(record["playlist_id"])
        known_ids.add(str(record["playlist_id"]))
    playlists = spotify.list_playlists()
    unmanaged_names = {}
    for playlist in playlists:
        release_id = discogs_marker(playlist.get("description"))
        if release_id:
            managed.setdefault(release_id, []).append(str(playlist["id"]))
            known_ids.add(str(playlist["id"]))
        elif str(playlist.get("id")) not in known_ids:
            unmanaged_names.setdefault(playlist.get("name") or "", []).append(
                str(playlist["id"])
            )
    result = {}
    for release_id, playlist_ids in managed.items():
        result[release_id] = sorted(set(playlist_ids))
    if verbose:
        LOGGER.info("Found %d Spotify playlists and %d managed release mappings",
                    len(playlists), len(result))
    return result, unmanaged_names


def _apply_plan(
    plan, spotify, state, config, dry_run, sync_signature, report
):
    release = plan.release
    playlist_id = plan.playlist_id
    if dry_run or state.readonly:
        return
    created_now = not playlist_id
    if created_now:
        created = spotify.create_playlist(
            plan.playlist_name,
            plan.description,
            public=config["SPOTIFY_PUBLIC_PLAYLISTS"],
        )
        playlist_id = str(created["id"])
        plan.playlist_id = playlist_id
        plan.playlist_action = "CREATE"
        state.record_release(
            release.release_id, release.folder_id, release.folder_name,
            playlist_id, plan.playlist_name,
            description_hash=plan.description_hash,
        )
    else:
        state.record_release(
            release.release_id, release.folder_id, release.folder_name,
            playlist_id, plan.playlist_name,
        )

    if not created_now and (
        plan.name_status == "UPDATED"
        or plan.description_status in ("CREATED", "UPDATED")
    ):
        spotify.update_playlist(
            playlist_id,
            name=plan.playlist_name if plan.name_status == "UPDATED" else None,
            description=(
                plan.description
                if plan.description_status in ("CREATED", "UPDATED")
                else None
            ),
        )
        if plan.description_status in ("CREATED", "UPDATED"):
            state.update_hashes(
                release.release_id, description_hash=plan.description_hash
            )
            report.description_updates += 1
    elif config["PLAYLIST_DESCRIPTION"] and plan.description_status == "UNCHANGED":
        state.update_hashes(release.release_id, description_hash=plan.description_hash)

    if plan.desired_uris != plan.current_uris and (
        plan.desired_uris or not release.tracks
    ):
        spotify.replace_tracks(playlist_id, plan.desired_uris)
    else:
        report.skipped_operations += 1

    if plan.artwork_bytes and plan.cover_status in ("CREATED", "UPDATED"):
        spotify.update_cover(playlist_id, plan.artwork_jpeg)
        plan.artwork_hash = hashlib.sha256(plan.artwork_bytes).hexdigest()
        state.update_hashes(release.release_id, artwork_hash=plan.artwork_hash)
        report.artwork_updates += 1
    elif plan.cover_status == "UNCHANGED":
        report.skipped_operations += 1
    if plan.description_status == "UNCHANGED":
        report.skipped_operations += 1
    state.record_tracks(
        release.release_id,
        plan.tracks,
        plan.matches,
        plan.cache_signatures,
    )
    state.complete_release(release.release_id, sync_signature)


def _pause_sync(report, error, state, dry_run, completed, pending, release_id=None):
    report.sync_paused = True
    report.current_release_id = release_id
    report.retry_after = getattr(error, "retry_after", None)
    retry_at = getattr(error, "retry_at", None)
    report.retry_at = (
        retry_at.strftime("%Y-%m-%d %H:%M:%S %Z") if retry_at else None
    )
    reason = str(getattr(error, "reason", "") or "").upper()
    if reason == "QUOTA_EXCEEDED":
        report.pause_reason = "Spotify quota exceeded"
        report.quota_exhaustion_pauses += 1
        LOGGER.error(
            "Spotify quota exhausted. Synchronization paused until the "
            "server-provided retry time."
        )
    elif report.retry_after is None:
        report.pause_reason = "Spotify HTTP 429 without a usable Retry-After value"
        LOGGER.error("%s", report.pause_reason)
    elif report.retry_after > 300:
        report.pause_reason = (
            "Spotify rate limit; server-requested wait exceeds automatic retry"
        )
        LOGGER.warning(
            "Spotify rate limit reached. Synchronization paused; requested wait is %.0f seconds.",
            report.retry_after,
        )
    else:
        report.pause_reason = "Spotify rate limit persisted after automatic retries"
        LOGGER.warning(
            "Spotify rate limit persisted after automatic retries at %s.",
            getattr(error, "endpoint", "an unknown endpoint"),
        )
    report.rate_limit_pauses += 1
    if report.retry_after is not None:
        LOGGER.warning(
            "Spotify requested a retry after %.0f seconds%s",
            report.retry_after,
            " (no earlier than {})".format(report.retry_at) if report.retry_at else "",
        )
    if not dry_run and not state.readonly:
        state.update_sync_progress(
            "PAUSED",
            reason=report.pause_reason,
            current_release_id=release_id,
            retry_after=report.retry_after,
            retry_at=report.retry_at,
            completed_releases=completed,
            pending_releases=pending,
        )


def synchronize(
    releases,
    all_release_ids,
    spotify,
    discogs,
    state,
    config,
    dry_run=True,
    verbose=False,
    max_releases=None,
):
    report = SyncReport(
        releases_total=len(all_release_ids),
        releases_selected=len(releases),
    )
    transport = getattr(spotify, "transport", None)
    starting_requests = getattr(transport, "request_count", 0)
    starting_429s = getattr(transport, "rate_limit_count", 0)
    report.releases_skipped = max(0, len(all_release_ids) - len(releases))
    target_names = playlist_names(
        releases, config.get("SPOTIFY_PLAYLIST_ROOT")
    )
    signatures = {
        release.release_id: _release_sync_signature(
            release, config, target_names[release.release_id]
        )
        for release in releases
    }
    completed_ids = {
        release.release_id
        for release in releases
        if state.is_release_complete(
            release.release_id, signatures[release.release_id]
        )
    }
    report.releases_completed = len(completed_ids)
    report.releases_pending = max(0, len(releases) - len(completed_ids))
    pending_releases = [
        release for release in releases if release.release_id not in completed_ids
    ]
    if not dry_run and not state.readonly:
        state.update_sync_progress(
            "RUNNING",
            completed_releases=report.releases_completed,
            pending_releases=report.releases_pending,
        )

    LOGGER.info("Checking existing Spotify playlists and managed release markers")
    try:
        managed_playlists, unmanaged_names = discover_managed_playlists(
            spotify, state, verbose
        )
    except KeyboardInterrupt:
        if not dry_run and not state.readonly:
            state.update_sync_progress(
                "INTERRUPTED",
                reason="User interrupted synchronization",
                completed_releases=len(completed_ids),
                pending_releases=max(0, len(releases) - len(completed_ids)),
            )
        raise
    except APIError as error:
        if error.service != "Spotify" or error.status != 429:
            raise
        _pause_sync(
            report,
            error,
            state,
            dry_run,
            report.releases_completed,
            report.releases_pending,
        )
        managed_playlists, unmanaged_names = {}, {}
    LOGGER.info(
        "Spotify inventory complete: %d managed release mappings",
        len(managed_playlists),
    )

    existing = {str(release_id) for release_id in all_release_ids}
    orphaned_records = [
        record
        for record in state.managed_releases()
        if record["release_id"] not in existing
    ]
    if config["SYNC_DELETIONS"] and orphaned_records and not dry_run and not state.readonly:
        print(
            "SYNC_DELETIONS enabled: will clear tracks (not delete playlists) from these managed orphans:"
        )
        for orphan in orphaned_records:
            print(
                "  {} | Discogs {} | Spotify {}".format(
                    orphan["playlist_name"],
                    orphan["release_id"],
                    orphan["playlist_id"],
                )
            )

    releases_to_process = (
        pending_releases[:max_releases]
        if max_releases is not None
        else pending_releases
    )
    skipped_by_schedule = len(releases) - len(releases_to_process)
    report.releases_skipped += skipped_by_schedule
    report.skipped_operations += skipped_by_schedule

    total = len(releases_to_process)
    LOGGER.info("Planning %d selected releases", total)
    if not report.sync_paused:
        try:
            for index, release in enumerate(releases_to_process, 1):
                if not dry_run and not state.readonly:
                    state.update_sync_progress(
                        "RUNNING",
                        current_release_id=release.release_id,
                        completed_releases=len(completed_ids),
                        pending_releases=max(0, len(releases) - len(completed_ids)),
                    )
                if index == 1 or index % 10 == 0 or index == total:
                    LOGGER.info(
                        "Planning release %d/%d: Discogs %s (%s)",
                        index,
                        total,
                        release.release_id,
                        release.title or "untitled",
                    )
                try:
                    plan = build_release_plan(
                        release,
                        spotify,
                        discogs,
                        state,
                        config,
                        managed_playlists,
                        report,
                        dry_run,
                        unmanaged_names,
                        target_names[release.release_id],
                    )
                    report.plans.append(plan)
                    if plan.playlist_action == "COLLISION":
                        report.releases_skipped += 1
                        report.other_errors += 1
                        report.warnings.append(
                            "Unmanaged playlist name collision for release {} ({}): {}".format(
                                release.release_id,
                                plan.playlist_name,
                                ", ".join(plan.unmanaged_name_collisions),
                            )
                        )
                        continue
                    if plan.playlist_action == "DUPLICATE":
                        report.releases_skipped += 1
                        report.other_errors += 1
                        report.warnings.append(
                            "Duplicate managed playlists for release {}: {}".format(
                                release.release_id,
                                ", ".join(plan.duplicate_playlist_ids),
                            )
                        )
                        continue
                    _record_track_results(report, plan, config)
                    if plan.match_status in ("NO MATCH", "AMBIGUOUS MATCH"):
                        report.releases_skipped += 1
                        report.skipped_operations += 1
                        report.warnings.append(
                            "{} | Discogs release {} was left pending; no Spotify playlist was created or changed.".format(
                                plan.match_status,
                                release.release_id,
                            )
                        )
                        LOGGER.warning(
                            "%s for Discogs release %s; leaving it pending without playlist writes",
                            plan.match_status,
                            release.release_id,
                        )
                        continue
                    _apply_plan(
                        plan,
                        spotify,
                        state,
                        config,
                        dry_run,
                        signatures[release.release_id],
                        report,
                    )
                    completed_ids.add(release.release_id)
                    if not dry_run and not state.readonly:
                        state.update_sync_progress(
                            "RUNNING",
                            completed_releases=len(completed_ids),
                            pending_releases=max(0, len(releases) - len(completed_ids)),
                        )
                    report.releases_processed += 1
                    if plan.playlist_action == "CREATE":
                        report.playlists_created += 1
                    elif plan.playlist_action == "UPDATE":
                        report.playlists_updated += 1
                    else:
                        report.playlists_unchanged += 1
                    if verbose:
                        LOGGER.info(
                            "%s | %s | %s | NAME %s | COVER %s | DESCRIPTION %s",
                            plan.playlist_action,
                            plan.playlist_name,
                            release.release_id,
                            plan.name_status,
                            plan.cover_status,
                            plan.description_status,
                        )
                    else:
                        LOGGER.debug(
                            "%s release=%s playlist=%s tracks=%d",
                            plan.playlist_action,
                            release.release_id,
                            plan.playlist_name,
                            len(release.tracks),
                        )
                except APIError as error:
                    if error.service == "Spotify" and error.status == 429:
                        _pause_sync(
                            report,
                            error,
                            state,
                            dry_run,
                            len(completed_ids),
                            max(0, len(releases) - len(completed_ids)),
                            release.release_id,
                        )
                        break
                    report.api_errors += 1
                    report.releases_skipped += 1
                    if not dry_run and not state.readonly:
                        state.update_sync_progress(
                            "INCOMPLETE",
                            reason=str(error),
                            current_release_id=release.release_id,
                            completed_releases=len(completed_ids),
                            pending_releases=max(0, len(releases) - len(completed_ids)),
                        )
                    report.warnings.append(
                        "API error for Discogs release {}: {}".format(
                            release.release_id, error
                        )
                    )
                    LOGGER.error("%s", error)
                except (SyncError, KeyError, ValueError) as error:
                    report.other_errors += 1
                    report.releases_skipped += 1
                    if not dry_run and not state.readonly:
                        state.update_sync_progress(
                            "INCOMPLETE",
                            reason=str(error),
                            current_release_id=release.release_id,
                            completed_releases=len(completed_ids),
                            pending_releases=max(0, len(releases) - len(completed_ids)),
                        )
                    report.warnings.append(
                        "Error for Discogs release {}: {}".format(
                            release.release_id, error
                        )
                    )
                    LOGGER.error("%s", error)
        except KeyboardInterrupt:
            if not dry_run and not state.readonly:
                state.update_sync_progress(
                    "INTERRUPTED",
                    reason="User interrupted synchronization",
                    completed_releases=len(completed_ids),
                    pending_releases=max(0, len(releases) - len(completed_ids)),
                )
            raise

    report.releases_completed = len(completed_ids)
    report.releases_pending = max(0, len(releases) - len(completed_ids))
    report.limit_reached = bool(
        max_releases is not None and report.releases_pending and not report.sync_paused
    )
    report.orphaned = [
        {
            "release_id": record["release_id"],
            "playlist_id": record["playlist_id"],
            "playlist_name": record["playlist_name"],
        }
        for record in orphaned_records
    ]
    report.playlists_orphaned = len(report.orphaned)

    if config["SYNC_DELETIONS"] and report.orphaned and not dry_run and not state.readonly and not report.sync_paused:
        try:
            for orphan in report.orphaned:
                try:
                    tracks = spotify.playlist_tracks(orphan["playlist_id"])
                    if tracks:
                        spotify.replace_tracks(orphan["playlist_id"], [])
                        LOGGER.info(
                            "Cleared tracks from orphaned managed playlist %s (%s); playlist retained",
                            orphan["playlist_name"],
                            orphan["playlist_id"],
                        )
                except APIError as error:
                    if error.service == "Spotify" and error.status == 429:
                        _pause_sync(
                            report,
                            error,
                            state,
                            dry_run,
                            report.releases_completed,
                            report.releases_pending,
                        )
                        break
                    report.api_errors += 1
                    report.warnings.append(
                        "Could not clear orphaned playlist {}: {}".format(
                            orphan["playlist_id"], error
                        )
                    )
        except KeyboardInterrupt:
            state.update_sync_progress(
                "INTERRUPTED",
                reason="User interrupted synchronization",
                completed_releases=report.releases_completed,
                pending_releases=report.releases_pending,
            )
            raise

    if not dry_run and not state.readonly and not report.sync_paused:
        status = (
            "LIMIT_REACHED" if report.limit_reached
            else "INCOMPLETE" if report.releases_pending
            else "COMPLETED"
        )
        state.update_sync_progress(
            status,
            reason="--max-releases limit reached" if report.limit_reached else None,
            completed_releases=report.releases_completed,
            pending_releases=report.releases_pending,
        )
    report.spotify_api_requests = (
        getattr(transport, "request_count", starting_requests) - starting_requests
    )
    report.http_429_responses = (
        getattr(transport, "rate_limit_count", starting_429s) - starting_429s
    )
    return report
