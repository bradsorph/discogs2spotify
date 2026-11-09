import logging


def print_report(report, dry_run, root_name):
    print("Discogs collection: {} releases".format(report.releases_total))
    print("Discogs releases processed: {}".format(report.releases_processed))
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
    print("API errors: {}".format(report.api_errors))
    print("Other errors: {}".format(report.other_errors))
    for warning in report.warnings:
        print("WARNING: {}".format(warning))
    if dry_run:
        print("\nNO CHANGES MADE (dry-run)")
    logging.getLogger("discogs2spotify").info(
        "Completed with %d API errors and %d other errors",
        report.api_errors,
        report.other_errors,
    )
