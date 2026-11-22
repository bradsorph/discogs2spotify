import argparse
import json
import os
from pathlib import Path

from .csv_loader import load_collection
from .graph import build_collection_coverage
from .normalizer import normalize_release
from src.discogs2spotify.config import filter_releases, load_config
from src.discogs2spotify.errors import SyncError
from .validator import (
    validate_collection,
    validate_malformed_json,
    validate_relations,
    validate_taxonomy,
)


SCHEMA_VERSION = "1.0"
GRAPH_PATH_DEFAULTS = {
    "MUSIC_MAP_INPUT": "data/collection.csv",
    "MUSIC_MAP_TAXONOMY": "data/taxonomy.json",
    "MUSIC_MAP_RELATIONS": "data/relations.json",
    "MUSIC_MAP_OUTPUT": "output/collection_map.json",
    "MUSIC_MAP_REPORT": "output/validation_report.json",
    "MUSIC_MAP_AMBIGUOUS_OUTPUT": "output/ambiguous_mappings.json",
}


def write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def run(input_path: str, taxonomy_path: str, relations_path: str,
        output_path: str, report_path: str, dry_run: bool = False,
        ambiguous_output_path: str = None,
        exclude_folders=None) -> dict:
    report = {"errors": [], "warnings": [], "information": []}
    taxonomy = validate_malformed_json(taxonomy_path, report, "taxonomy")
    relations = validate_malformed_json(relations_path, report, "relations")

    if taxonomy is None:
        taxonomy = {"schema_version": "1.0", "nodes": []}
    if relations is None:
        relations = {"schema_version": "1.0", "relations": []}

    validate_taxonomy(taxonomy, report)
    validate_relations(relations, taxonomy, report)

    collection = load_collection(input_path)
    node_ids = {node["id"] for node in taxonomy["nodes"]
                if isinstance(node, dict) and node.get("id")}

    source_releases = collection["releases"]
    releases = filter_releases(
        source_releases,
        excludes=exclude_folders or [],
    )
    excluded_release_count = len(source_releases) - len(releases)
    for release in releases:
        release["canonical_mapping"] = normalize_release(release, node_ids)

    mapping_counts = validate_collection(releases, report)
    coverage = build_collection_coverage(releases, node_ids)

    ambiguous_mappings = []
    unmapped_values = set()
    for release in releases:
        mapping = release["canonical_mapping"]
        for field in ("genres", "styles"):
            unmapped_values.update(mapping[field]["unmapped"])
            ambiguous_mappings.extend(mapping[field]["ambiguous"])

    for value in sorted(unmapped_values):
        report["warnings"].append({
            "code": "UNMAPPED_DISCOGS_VALUE",
            "message": "Discogs genre/style has no canonical taxonomy mapping",
            "source_value": value,
        })

    if mapping_counts["ambiguous"]:
        report["warnings"].append({
            "code": "AMBIGUOUS_MAPPINGS",
            "message": "Some mappings require human review",
            "count": mapping_counts["ambiguous"],
        })

    report["information"].append({
        "code": "COLLECTION_SUMMARY",
        "release_count": len(releases),
        "excluded_release_count": excluded_release_count,
        "taxonomy_node_count": len(taxonomy["nodes"]),
        "relation_count": len(relations["relations"]),
        "represented_node_count": coverage["represented_count"],
        "unmapped_source_value_count": len(unmapped_values),
    })

    collection_map = {
        "schema_version": SCHEMA_VERSION,
        "collection": {
            "source": "Discogs",
            "source_file": input_path,
            "source_row_count": collection["source_row_count"],
            "included_release_count": len(releases),
            "excluded_release_count": excluded_release_count,
            "excluded_folders": sorted(set(exclude_folders or [])),
            "releases": releases,
        },
        "taxonomy": taxonomy,
        "relations": relations["relations"],
        "coverage": coverage,
    }

    ambiguous_output = {
        "schema_version": SCHEMA_VERSION,
        "mappings": ambiguous_mappings,
        "unmapped_source_values": sorted(unmapped_values),
    }

    if not dry_run:
        write_json(output_path, collection_map)
        ambiguous_path = ambiguous_output_path or os.path.join(
            os.path.dirname(output_path) or ".", "ambiguous_mappings.json")
        write_json(ambiguous_path, ambiguous_output)
        write_json(report_path, report)

    return {
        "collection_map": collection_map,
        "ambiguous_mappings": ambiguous_output,
        "validation_report": report,
    }


def build_parser() -> argparse.ArgumentParser:
    project_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        description="Build a structured music-history graph from a Discogs CSV.")
    parser.add_argument(
        "--config",
        default=str(project_root / "discogs2spotify.conf"),
        help="shared Discogs2Spotify configuration file")
    parser.add_argument("--input", help="override MUSIC_MAP_INPUT")
    parser.add_argument("--taxonomy", help="override MUSIC_MAP_TAXONOMY")
    parser.add_argument("--relations", help="override MUSIC_MAP_RELATIONS")
    parser.add_argument("--output", help="override MUSIC_MAP_OUTPUT")
    parser.add_argument("--report", help="override MUSIC_MAP_REPORT")
    parser.add_argument(
        "--ambiguous-output",
        help="override MUSIC_MAP_AMBIGUOUS_OUTPUT")
    write_mode = parser.add_mutually_exclusive_group()
    write_mode.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="validate and summarize without writing output files")
    write_mode.add_argument(
        "--write",
        action="store_false",
        dest="dry_run",
        help="write outputs even when DRY_RUN is true in the config")
    return parser


def _configured_path(value: str, config_directory: Path) -> str:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_directory / path
    return str(path)


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = Path(args.config).expanduser().resolve()
    try:
        config = load_config(config_path)
    except SyncError as error:
        parser.error(str(error))

    def select_path(argument, key):
        if argument is not None:
            return argument
        return _configured_path(
            config.get(key, GRAPH_PATH_DEFAULTS[key]), config_path.parent)

    input_path = select_path(args.input, "MUSIC_MAP_INPUT")
    taxonomy_path = select_path(args.taxonomy, "MUSIC_MAP_TAXONOMY")
    relations_path = select_path(args.relations, "MUSIC_MAP_RELATIONS")
    output_path = select_path(args.output, "MUSIC_MAP_OUTPUT")
    report_path = select_path(args.report, "MUSIC_MAP_REPORT")
    ambiguous_output_path = select_path(
        args.ambiguous_output, "MUSIC_MAP_AMBIGUOUS_OUTPUT")
    dry_run = config["DRY_RUN"] if args.dry_run is None else args.dry_run

    result = run(
        input_path,
        taxonomy_path,
        relations_path,
        output_path,
        report_path,
        dry_run,
        ambiguous_output_path,
        config["EXCLUDE_FOLDERS"],
    )
    report = result["validation_report"]
    collection = result["collection_map"]["collection"]
    print("Dry run: no files written" if dry_run else "Generated collection map")
    print("Releases: {} included, {} excluded ({} source rows)".format(
        collection["included_release_count"],
        collection["excluded_release_count"],
        collection["source_row_count"],
    ))
    print("Errors: {}  Warnings: {}  Information: {}".format(
        len(report["errors"]), len(report["warnings"]), len(report["information"])))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
