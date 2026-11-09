import logging
import hashlib
from .artwork import UnusableArtwork, make_spotify_jpeg, select_release_image
from .errors import APIError, SyncError
from .matching import match_track, normalize_text
from .metadata import discogs_marker, generate_description, playlist_name, stable_hash
from .models import ReleasePlan, SyncReport, TrackMatch

LOGGER = logging.getLogger("discogs2spotify")


def _source_signature(track):
    return normalize_text(track.title), normalize_text(track.artist)


def _resolve_tracks(release, spotify, previous, settings):
    matches = []
    uris = []
    source_by_uri = {}
    duplicate_count = 0
    for track in release.tracks:
        candidates = spotify.search_tracks(track)
        match = match_track(
            track,
            candidates,
            threshold=settings["MATCH_THRESHOLD"],
            ambiguous_action=settings["AMBIGUOUS_MATCH_ACTION"],
        )
        LOGGER.debug(
            "Track match release=%s position=%s title=%r candidates=%d result=%s score=%.1f reason=%s",
            track.release_id,
            track.position,
            track.title,
            len(candidates),
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
    return matches, uris, duplicate_count


def _image_url(release, config):
    image = select_release_image(
        release.raw.get("images") or [], config.get("ARTWORK_IMAGE_INDEX", "primary")
    )
    return (image or {}).get("uri") if image else None


def build_release_plan(
    release, spotify, discogs, state, config, playlists_by_release,
    unmanaged_names=None,
):
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

    name_collisions = (unmanaged_names or {}).get(playlist_name(release), [])
    if not playlist_id and name_collisions:
        return ReleasePlan(
            release=release,
            playlist_name=playlist_name(release),
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
            playlist_name=playlist_name(release),
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
    matches, desired_uris, duplicate_tracks = _resolve_tracks(
        release, spotify, previous_tracks, config
    )
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
    if playlist_id and (tracklist_changed or description_changed or cover_changed):
        playlist_action = "UPDATE"

    return ReleasePlan(
        release=release,
        playlist_name=playlist_name(release),
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
        description_status=description_status,
        duplicate_track_matches=duplicate_tracks,
    )


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


def _apply_plan(plan, spotify, state, config, dry_run):
    release = plan.release
    playlist_id = plan.playlist_id
    if dry_run:
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

    if plan.description_status in ("CREATED", "UPDATED") and not created_now:
        spotify.update_description(playlist_id, plan.description)
        state.update_hashes(release.release_id, description_hash=plan.description_hash)
    elif config["PLAYLIST_DESCRIPTION"] and plan.description_status == "UNCHANGED":
        state.update_hashes(release.release_id, description_hash=plan.description_hash)

    if plan.desired_uris != plan.current_uris and (
        plan.desired_uris or not release.tracks
    ):
        spotify.replace_tracks(playlist_id, plan.desired_uris)

    if plan.artwork_bytes and plan.cover_status in ("CREATED", "UPDATED"):
        spotify.update_cover(playlist_id, plan.artwork_jpeg)
        plan.artwork_hash = hashlib.sha256(plan.artwork_bytes).hexdigest()
        state.update_hashes(release.release_id, artwork_hash=plan.artwork_hash)
    state.record_tracks(release.release_id, plan.tracks, plan.matches)


def synchronize(
    releases,
    all_release_ids,
    spotify,
    discogs,
    state,
    config,
    dry_run=True,
    verbose=False,
):
    report = SyncReport(releases_total=len(all_release_ids))
    LOGGER.info("Checking existing Spotify playlists and managed release markers")
    managed_playlists, unmanaged_names = discover_managed_playlists(
        spotify, state, verbose
    )
    LOGGER.info(
        "Spotify inventory complete: %d managed release mappings",
        len(managed_playlists),
    )
    report.releases_skipped = max(0, len(all_release_ids) - len(releases))
    existing = {str(release_id) for release_id in all_release_ids}
    orphaned_records = [
        record
        for record in state.managed_releases()
        if record["release_id"] not in existing
    ]
    if config["SYNC_DELETIONS"] and orphaned_records and not dry_run:
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

    total = len(releases)
    LOGGER.info("Planning %d selected releases", total)
    for index, release in enumerate(releases, 1):
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
                release, spotify, discogs, state, config,
                managed_playlists, unmanaged_names,
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
                        release.release_id, ", ".join(plan.duplicate_playlist_ids)
                    )
                )
                continue
            _apply_plan(plan, spotify, state, config, dry_run)
            report.releases_processed += 1
            if plan.playlist_action == "CREATE":
                report.playlists_created += 1
            elif plan.playlist_action == "UPDATE":
                report.playlists_updated += 1
            else:
                report.playlists_unchanged += 1
            report.duplicate_tracks += plan.duplicate_track_matches
            for track, match in zip(plan.tracks, plan.matches):
                if match.status == "MATCHED":
                    report.tracks_matched += 1
                elif match.status == "MISSING":
                    report.tracks_missing += 1
                    if config["MISSING_TRACK_ACTION"] == "report":
                        report.warnings.append(
                            "MISSING ON SPOTIFY | Discogs release: {} | Artist: {} | Track: {} | Reason: {}".format(
                                release.release_id, track.artist, track.title, match.reason
                            )
                        )
                elif match.status == "AMBIGUOUS":
                    report.tracks_ambiguous += 1
                elif match.status == "REJECTED":
                    report.tracks_rejected += 1
            if verbose:
                LOGGER.info(
                    "%s | %s/%s | %s | COVER %s | DESCRIPTION %s",
                    plan.playlist_action,
                    release.folder_name,
                    plan.playlist_name,
                    release.release_id,
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
            if error.status == 429:
                raise
            report.api_errors += 1
            report.releases_skipped += 1
            report.warnings.append(
                "API error for Discogs release {}: {}".format(release.release_id, error)
            )
            LOGGER.error("%s", error)
        except (SyncError, KeyError, ValueError) as error:
            report.other_errors += 1
            report.releases_skipped += 1
            report.warnings.append(
                "Error for Discogs release {}: {}".format(release.release_id, error)
            )
            LOGGER.error("%s", error)

    report.orphaned = [
        {
            "release_id": record["release_id"],
            "playlist_id": record["playlist_id"],
            "playlist_name": record["playlist_name"],
        }
        for record in orphaned_records
    ]
    report.playlists_orphaned = len(report.orphaned)

    if config["SYNC_DELETIONS"] and report.orphaned and not dry_run:
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
                report.api_errors += 1
                report.warnings.append(
                    "Could not clear orphaned playlist {}: {}".format(
                        orphan["playlist_id"], error
                    )
                )
    return report
