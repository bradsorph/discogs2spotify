import argparse
import copy
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
from .metadata import playlist_names
from .models import SyncReport
from .oauth import SpotifyTokenManager
from .process_lock import SyncProcessLock
from .reporting import print_report
from .spotify import SpotifyClient
from .sync import _release_sync_signature, synchronize


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[1;32m",
        logging.INFO: "\033[1;36m",
        logging.WARNING: "\033[1;33m",
        logging.ERROR: "\033[1;31m",
    }

    def __init__(self, *args, use_color=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.use_color = use_color

    def format(self, record):
        formatted_record = copy.copy(record)
        color = self.COLORS.get(formatted_record.levelno)
        if self.use_color and color:
            formatted_record.levelname = "{}{}{}".format(
                color, formatted_record.levelname, "\033[0m"
            )
        return super().format(formatted_record)


def _positive_int(value):
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a positive integer")
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


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
    parser.add_argument(
        "--release-id",
        "--discogs-release",
        dest="release_id",
        type=_positive_int,
        help="synchronize one Discogs release by its numeric ID",
    )
    parser.add_argument("--state-file", help="override SQLite state path")
    parser.add_argument(
        "--max-releases",
        type=_positive_int,
        help="process at most this many pending releases",
    )
    parser.add_argument("--auth", action="store_true", help="start Spotify OAuth and save/refresh the local token")
    parser.add_argument("--verbose", action="store_true", help="show per-release synchronization decisions")
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        type=str.upper,
        help="set logging verbosity (overrides LOG_LEVEL in config)",
    )
    parser.add_argument("--debug", action="store_true", help="shorthand for --log-level DEBUG")
    log_file = parser.add_mutually_exclusive_group()
    log_file.add_argument(
        "--log-file",
        dest="log_to_file",
        action="store_true",
        default=None,
        help="also write logs to ./logs/discogs2spotify.log",
    )
    log_file.add_argument(
        "--no-log-file",
        dest="log_to_file",
        action="store_false",
        default=None,
        help="disable file logging",
    )
    return parser


def _configure_logging(args, configured_level="INFO", log_to_file=False):
    level_name = args.log_level or configured_level
    if args.debug:
        level_name = "DEBUG"
    elif args.verbose and level_name in ("WARNING", "ERROR"):
        level_name = "INFO"
    handler = logging.StreamHandler()
    handler.setFormatter(
        ColorFormatter(
            "%(asctime)s %(levelname)s: %(message)s",
            datefmt="%H:%M:%S",
            use_color=sys.stderr.isatty(),
        )
    )
    handlers = [handler]
    if log_to_file:
        log_directory = Path.cwd() / "logs"
        log_directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_directory / "discogs2spotify.log", encoding="utf-8"
        )
        file_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        handlers.append(file_handler)
    logging.basicConfig(
        level=getattr(logging, level_name),
        handlers=handlers,
        force=True,
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)


def _load_releases(
    discogs,
    collection,
    report,
    state,
    config,
    target_names,
    max_releases=None,
):
    releases = []
    total = len(collection)
    logger = logging.getLogger("discogs2spotify")
    if max_releases is None:
        logger.info("Loading full Discogs release metadata for %d selected releases", total)
    else:
        logger.info(
            "Loading full Discogs release metadata for up to %d pending releases",
            max_releases,
        )
    pending_count = 0
    limit_reached = False
    for index, record in enumerate(collection, 1):
        if max_releases is not None and pending_count >= max_releases:
            limit_reached = True
            break
        if index == 1 or index % 10 == 0 or index == total:
            logger.info("Loading release metadata: %d/%d", index, total)
        logger.debug(
            "Fetching Discogs release metadata: release %s (%d/%d)",
            record["release_id"], index, total,
        )
        try:
            details = discogs.get_release(record["release_id"])
            release = release_from_api(record, details)
            releases.append(release)
            signature = _release_sync_signature(
                release, config, target_names[release.release_id]
            )
            if not state.is_release_complete(release.release_id, signature):
                pending_count += 1
        except APIError as error:
            report.api_errors += 1
            report.releases_skipped += 1
            pending_count += 1
            report.warnings.append(
                "API error for Discogs release {}: {}".format(record["release_id"], error)
            )
            logging.getLogger("discogs2spotify").error("%s", error)
    logger.info(
        "Finished release metadata: %d loaded, %d failed",
        len(releases), report.api_errors,
    )
    if limit_reached:
        logger.info(
            "Discogs metadata loading stopped at --max-releases=%d; remaining selected releases were not fetched",
            max_releases,
        )
    return releases, limit_reached


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    process_lock = None
    try:
        config = load_config(args.config)
        if args.log_to_file is not None:
            config["LOG_TO_FILE"] = args.log_to_file
        _configure_logging(args, config["LOG_LEVEL"], config["LOG_TO_FILE"])
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

        process_lock = SyncProcessLock(config["STATE_FILE"])
        process_lock.acquire()
        discogs_token = read_discogs_token(
            os.environ.get("DISCOGS_TOKEN_FILE", "~/.config/discogs/token")
        )
        discogs_transport = HTTPTransport(delay=config["API_DELAY"])
        spotify_transport = HTTPTransport(
            delay=max(config["SPOTIFY_API_DELAY"], 1.05)
        )
        discogs = DiscogsClient(discogs_token, transport=discogs_transport)
        spotify = SpotifyClient(token_manager, transport=spotify_transport)

        single_release = None
        if args.release_id is not None:
            release_id = str(args.release_id)
            logger.info("Fetching Discogs release %s directly", release_id)
            record = {
                "release_id": release_id,
                "folder_id": "0",
                "folder_name": "Uncategorized",
                "basic_information": {},
            }
            single_release = release_from_api(record, discogs.get_release(release_id))
            collection, duplicates, folders = [record], [], []
            selected = [record]
            logger.info("Selected Discogs release %s", release_id)
        else:
            username = str(config["DISCOGS_USER"]).strip()
            if not username:
                raise SyncError(
                    "Set DISCOGS_USER in discogs2spotify.conf or use --discogs-user"
                )
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
        dry_run = bool(config["DRY_RUN"] or args.report)
        logger.info(
            "Starting %s synchronization planning; loading Discogs release metadata",
            "read-only dry-run" if dry_run else "write-enabled",
        )
        state = StateStore(config["STATE_FILE"], readonly=dry_run)
        try:
            if single_release is not None:
                stored_release = state.get_release(single_release.release_id)
                if stored_release:
                    single_release.folder_id = stored_release["folder_id"]
                    single_release.folder_name = stored_release["folder_name"]
                releases = [single_release]
                target_names = playlist_names(
                    releases, config.get("SPOTIFY_PLAYLIST_ROOT")
                )
                metadata_limit_reached = False
            else:
                target_names = playlist_names(
                    [
                        release_from_api(record, {})
                        for record in selected
                    ],
                    config.get("SPOTIFY_PLAYLIST_ROOT"),
                )
                releases, metadata_limit_reached = _load_releases(
                    discogs,
                    selected,
                    report_errors,
                    state,
                    config,
                    target_names,
                    max_releases=args.max_releases,
                )
            report = synchronize(
                releases,
                [record["release_id"] for record in collection],
                spotify,
                discogs,
                state,
                config,
                dry_run=dry_run,
                verbose=args.verbose or args.debug,
                max_releases=args.max_releases,
                selected_release_count=len(selected),
                target_names=target_names,
                metadata_limit_reached=metadata_limit_reached,
                detect_orphans=single_release is None,
            )
        finally:
            state.close()
        report.releases_total = len(collection)
        report.releases_selected = len(selected)
        report.api_errors += report_errors.api_errors
        report.releases_skipped += report_errors.releases_skipped
        report.warnings = report_errors.warnings + report.warnings
        if duplicates:
            report.warnings.append(
                "Discogs contained repeated release IDs across folders; synchronized one playlist per unique release"
            )

        if single_release is not None:
            print("Discogs release: {}".format(single_release.release_id))
        else:
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
        if report.sync_paused:
            return 3
        return 1 if report.api_errors or report.other_errors else 0
    except (SyncError, APIError, sqlite3.Error, OSError) as error:
        print("Error: {}".format(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; rerun to resume safely.", file=sys.stderr)
        return 130
    finally:
        if process_lock is not None:
            process_lock.release()


if __name__ == "__main__":
    sys.exit(main())
