import logging


def print_report(report, dry_run, root_name):
    print("Discogs collection: {} releases".format(report.releases_total))
    print("Discogs releases processed: {}".format(report.releases_processed))
    print("Discogs releases completed: {}".format(report.releases_completed))
    print("Discogs releases pending: {}".format(report.releases_pending))
    print("Discogs releases skipped: {}".format(report.releases_skipped))
    print("Spotify logical root: {}".format(root_name))
    print()
    print("PLAYLISTS")
    for plan in report.plans:
        label = plan.playlist_action
        if plan.duplicate_playlist_ids:
            label = "DUPLICATE"
        print(
            "  {:9} {}/{} (Discogs {})".format(
                label,
                plan.release.folder_name,
                plan.playlist_name,
                plan.release.release_id,
            )
        )
        if plan.duplicate_playlist_ids:
            print("            Spotify IDs: {}".format(", ".join(plan.duplicate_playlist_ids)))
        print("            COVER {}".format(plan.cover_status))
        print("            DESCRIPTION {}".format(plan.description_status))
    print()
    print("TRACKS")
    print("  Matched:   {}".format(report.tracks_matched))
    print("  Missing:   {}".format(report.tracks_missing))
    print("  Ambiguous: {}".format(report.tracks_ambiguous))
    print("  Rejected:  {}".format(report.tracks_rejected))
    print("  Duplicates: {}".format(report.duplicate_tracks))
    print()
    print("ORPHANED PLAYLISTS: {}".format(report.playlists_orphaned))
    for orphan in report.orphaned:
        print(
            "  ORPHANED {} (Discogs {}, Spotify {})".format(
                orphan["playlist_name"], orphan["release_id"], orphan["playlist_id"]
            )
        )
    print()
    print("Spotify playlists orphaned: {}".format(report.playlists_orphaned))
    print("Spotify playlists created: {}".format(report.playlists_created))
    print("Spotify playlists updated: {}".format(report.playlists_updated))
    print("Spotify playlists unchanged: {}".format(report.playlists_unchanged))
    print("Tracks matched: {}".format(report.tracks_matched))
    print("Tracks missing: {}".format(report.tracks_missing))
    print("Tracks ambiguous: {}".format(report.tracks_ambiguous))
    print("Tracks rejected: {}".format(report.tracks_rejected))
    print("Duplicate tracks: {}".format(report.duplicate_tracks))
    print("Tracks searched: {}".format(report.tracks_searched))
    print("Tracks resolved from cache: {}".format(report.tracks_resolved_from_cache))
    print("Spotify API requests: {}".format(report.spotify_api_requests))
    print("HTTP 429 responses: {}".format(report.http_429_responses))
    print("Rate-limit pauses: {}".format(report.rate_limit_pauses))
    print("Quota-exhaustion pauses: {}".format(report.quota_exhaustion_pauses))
    print("Description updates: {}".format(report.description_updates))
    print("Artwork updates: {}".format(report.artwork_updates))
    print("Skipped operations: {}".format(report.skipped_operations))
    print("API errors: {}".format(report.api_errors))
    print("Other errors: {}".format(report.other_errors))
    if report.sync_paused:
        print("\nSYNC PAUSED")
        print("Reason: {}".format(report.pause_reason))
        if report.current_release_id:
            print("Current release: Discogs {}".format(report.current_release_id))
        print(
            "Completed: {}/{} releases".format(
                report.releases_completed, report.releases_total
            )
        )
        print("Pending:   {} releases".format(report.releases_pending))
        if report.retry_after is not None:
            hours, remainder = divmod(int(report.retry_after), 3600)
            minutes, seconds = divmod(remainder, 60)
            print(
                "Spotify requested a retry after {}h {}m {}s.".format(
                    hours, minutes, seconds
                )
            )
            if report.retry_at:
                print("Do not retry before: {}".format(report.retry_at))
        print("Completed synchronization state has been preserved.")
    elif report.limit_reached:
        print("\nRELEASE LIMIT REACHED")
        print("Completed: {}/{} releases".format(
            report.releases_completed, report.releases_total
        ))
        print("Pending:   {} releases".format(report.releases_pending))
        print("Run the synchronization again to continue pending releases.")
    for warning in report.warnings:
        print("WARNING: {}".format(warning))
    if dry_run:
        print("\nNO CHANGES MADE (dry-run)")
    logging.getLogger("discogs2spotify").info(
        "Completed with %d API errors and %d other errors",
        report.api_errors,
        report.other_errors,
    )
