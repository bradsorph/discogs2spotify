import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

from .api import HTTPTransport
from .config import apply_overrides, filter_releases, load_config
from .database import StateStore
from .discogs import DiscogsClient, read_discogs_token, release_from_api
from .errors import APIError, SyncError
from .models import SyncReport
from .oauth import SpotifyTokenManager
from .reporting import print_report
from .spotify import SpotifyClient
from .sync import synchronize


def build_parser():
    parser = argparse.ArgumentParser(
        prog="discogs2spotify.sh",
        description="Synchronize a Discogs collection to managed Spotify playlists.",
        epilog=(
            "The default is a safe dry run. Use --sync to write changes. "
            "Spotify playlists are not physically organized into folders by the Web API."
        ),
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parents[2] / "discogs2spotify.conf"),
        help="configuration file (KEY=value; YAML is also accepted by extension)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=None, help="plan changes without modifying Spotify (default)")
    mode.add_argument("--sync", action="store_true", help="apply the synchronization plan")
    mode.add_argument("--report", action="store_true", help="read and print a synchronization report without writing")
    parser.add_argument("--folder", action="append", help="include this Discogs folder (repeatable)")
    parser.add_argument("--exclude", action="append", help="exclude this Discogs folder (repeatable)")
    parser.add_argument("--discogs-user", help="override DISCOGS_USER")
    parser.add_argument("--state-file", help="override SQLite state path")
    parser.add_argument("--auth", action="store_true", help="start Spotify OAuth and save/refresh the local token")
    parser.add_argument("--verbose", action="store_true", help="show per-release synchronization decisions")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        type=str.upper,
        help="set logging verbosity (overrides LOG_LEVEL in config)",
    )
    parser.add_argument("--debug", action="store_true", help="shorthand for --log-level DEBUG")
    return parser


def _configure_logging(args, configured_level="INFO"):
    level_name = args.log_level or configured_level
    if args.debug:
        level_name = "DEBUG"
    elif args.verbose and level_name in ("WARNING", "ERROR"):
        level_name = "INFO"
    logging.basicConfig(
        level=getattr(logging, level_name),
        format="%(asctime)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)


def _load_releases(discogs, collection, report):
    releases = []
    total = len(collection)
    logger = logging.getLogger("discogs2spotify")
    logger.info("Loading full Discogs release metadata for %d selected releases", total)
    for index, record in enumerate(collection, 1):
        if index == 1 or index % 10 == 0 or index == total:
            logger.info("Loading release metadata: %d/%d", index, total)
        logger.debug(
            "Fetching Discogs release metadata: release %s (%d/%d)",
            record["release_id"], index, total,
        )
        try:
            details = discogs.get_release(record["release_id"])
            releases.append(release_from_api(record, details))
        except APIError as error:
            report.api_errors += 1
            report.releases_skipped += 1
            report.warnings.append(
                "API error for Discogs release {}: {}".format(record["release_id"], error)
            )
            logging.getLogger("discogs2spotify").error("%s", error)
    logger.info(
        "Finished release metadata: %d loaded, %d failed",
        len(releases), report.api_errors,
    )
    return releases


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        _configure_logging(args, config["LOG_LEVEL"])
        logger = logging.getLogger("discogs2spotify")
        config = apply_overrides(
            config,
            discogs_user=args.discogs_user,
            folders=args.folder,
            excludes=args.exclude,
            dry_run=True if args.report else args.dry_run,
            sync=args.sync,
            state_file=args.state_file,
        )
        token_manager = SpotifyTokenManager(
            redirect_uri=config["OAUTH_REDIRECT_URI"],
            public_playlists=config["SPOTIFY_PUBLIC_PLAYLISTS"],
        )
        if args.auth:
            logger.info("Starting Spotify authorization")
            token_manager.access_token(force_auth=True)
            print("Spotify authorization saved under ~/.config/discogs2spotify/")
            return 0

        username = str(config["DISCOGS_USER"]).strip()
        if not username:
            raise SyncError("Set DISCOGS_USER in discogs2spotify.conf or use --discogs-user")
        discogs_token = read_discogs_token(
            os.environ.get("DISCOGS_TOKEN_FILE", "~/.config/discogs/token")
        )
        discogs_transport = HTTPTransport(delay=config["API_DELAY"])
        spotify_transport = HTTPTransport(
            delay=max(config["SPOTIFY_API_DELAY"], 1.05)
        )
        discogs = DiscogsClient(discogs_token, transport=discogs_transport)
        spotify = SpotifyClient(token_manager, transport=spotify_transport)

        logger.info("Reading Discogs folders and collection for %s", username)
        collection, duplicates, folders = discogs.list_collection(username)
        logger.info(
            "Discogs collection loaded: %d unique releases across %d folders",
            len(collection),
            len([folder for folder in folders if str(folder.get("id")) != "0"]),
        )
        if config["INCLUDE_FOLDERS"]:
            available_folders = {
                str(folder.get("name") or "")
                for folder in folders
                if str(folder.get("id")) != "0"
            }
            unknown_folders = sorted(
                set(config["INCLUDE_FOLDERS"]) - available_folders
            )
            if unknown_folders:
                raise SyncError(
                    "Discogs include folder(s) not found: {}. Available folders: {}".format(
                        ", ".join(unknown_folders),
                        ", ".join(sorted(available_folders)) or "(none)",
                    )
                )
        selected = filter_releases(
            collection, config["INCLUDE_FOLDERS"], config["EXCLUDE_FOLDERS"]
        )
        logger.info(
            "Selected %d of %d releases; filters include=%s exclude=%s",
            len(selected),
            len(collection),
            config["INCLUDE_FOLDERS"] or "(all)",
            config["EXCLUDE_FOLDERS"] or "(none)",
        )
        report_errors = SyncReport()
        releases = _load_releases(discogs, selected, report_errors)

        dry_run = bool(config["DRY_RUN"] or args.report)
        logger.info(
            "Starting %s synchronization planning; Spotify playlist inventory follows",
            "read-only dry-run" if dry_run else "write-enabled",
        )
        state = StateStore(config["STATE_FILE"], readonly=dry_run)
        try:
            report = synchronize(
                releases,
                [record["release_id"] for record in collection],
                spotify,
                discogs,
                state,
                config,
                dry_run=dry_run,
                verbose=args.verbose or args.debug,
            )
        finally:
            state.close()
        report.releases_total = len(collection)
        report.api_errors += report_errors.api_errors
        report.warnings = report_errors.warnings + report.warnings
        if duplicates:
            report.warnings.append(
                "Discogs contained repeated release IDs across folders; synchronized one playlist per unique release"
            )

        print(
            "Folders: {} (selected: {})".format(
                len([folder for folder in folders if str(folder.get("id")) != "0"]),
                ", ".join(config["INCLUDE_FOLDERS"]) if config["INCLUDE_FOLDERS"] else "all except excluded",
            )
        )
        print(
            "Discogs collection: {} releases; selected: {}".format(
                len(collection), len(selected)
            )
        )
        print_report(report, dry_run, config["SPOTIFY_PLAYLIST_ROOT"])
        logger.info("Synchronization finished")
        return 1 if report.api_errors or report.other_errors else 0
    except (SyncError, APIError, sqlite3.Error, OSError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; rerun to resume safely.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
