import csv
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.discogs2graph.csv_loader import load_collection, split_multi_value
from src.discogs2graph.main import main, run
from src.discogs2graph.normalizer import normalize_release
from src.discogs2graph.taxonomy import build_observed_taxonomy
from src.discogs2graph.validator import (
    validate_collection,
    validate_malformed_json,
    validate_relations,
    validate_taxonomy,
)


class Discogs2GraphTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def write_csv(self, rows, fieldnames=None):
        fieldnames = fieldnames or [
            "release_id", "Artist", "Title", "CollectionFolder",
            "discogs_genre", "discogs_style", "discogs_year",
        ]
        path = os.path.join(self.tempdir.name, "collection.csv")
        with open(path, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        return path

    def write_json(self, name, data):
        path = os.path.join(self.tempdir.name, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        return path

    def test_csv_loading_and_multi_value_parsing(self):
        path = self.write_csv([{
            "release_id": "123",
            "Artist": "Test Artist",
            "Title": "Test Title",
            "discogs_genre": "Rock; Pop",
            "discogs_style": "Pop Rock; Hard Rock",
            "discogs_year": "1979",
        }])
        collection = load_collection(path)
        release = collection["releases"][0]
        self.assertEqual(release["release_id"], "123")
        self.assertEqual(release["discogs"]["genres"], ["Rock", "Pop"])
        self.assertEqual(release["discogs"]["styles"], ["Pop Rock", "Hard Rock"])
        self.assertEqual(split_multi_value("A; B"), ["A", "B"])

    def test_normalization_maps_multiple_nodes(self):
        release = {
            "release_id": "123",
            "discogs": {
                "genres": ["Rock", "Blues"],
                "styles": ["Blues Rock", "Chicago Blues"],
            },
        }
        result = normalize_release(release, {
            "rock", "blues", "blues_rock", "chicago_blues",
        })
        self.assertEqual(
            set(result["canonical_node_ids"]),
            {"rock", "blues", "blues_rock", "chicago_blues"},
        )

    def test_duplicate_release_detection(self):
        report = {"errors": [], "warnings": [], "information": []}
        validate_collection([
            {"release_id": "1", "artist": "A", "title": "X"},
            {"release_id": "1", "artist": "B", "title": "Y"},
        ], report)
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("DUPLICATE_RELEASE_ID", codes)

    def test_taxonomy_duplicate_and_missing_fields(self):
        taxonomy = {
            "schema_version": "1.0",
            "nodes": [
                {"id": "rock", "name": "Rock"},
                {"id": "rock", "name": "Rock again"},
                {"id": "missing_name"},
            ],
        }
        report = {"errors": [], "warnings": [], "information": []}
        validate_taxonomy(taxonomy, report)
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("DUPLICATE_NODE_ID", codes)
        self.assertIn("MISSING_NODE_NAME", codes)

    def test_relation_validation_endpoints_and_values(self):
        taxonomy = {
            "schema_version": "1.0",
            "nodes": [{"id": "blues", "name": "Blues"}],
        }
        relations = {
            "schema_version": "1.0",
            "relations": [{
                "from": "blues",
                "to": "unknown",
                "type": "not_a_type",
                "strength": 2,
                "confidence": -1,
                "status": "verified",
            }],
        }
        report = {"errors": [], "warnings": [], "information": []}
        validate_relations(relations, taxonomy, report)
        codes = [item["code"] for item in report["errors"]]
        self.assertIn("UNKNOWN_RELATION_ENDPOINT", codes)
        self.assertIn("INVALID_RELATION_TYPE", codes)
        self.assertIn("INVALID_STRENGTH", codes)
        self.assertIn("INVALID_CONFIDENCE", codes)

    def test_malformed_json_detection(self):
        path = os.path.join(self.tempdir.name, "bad.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{ not json")
        report = {"errors": [], "warnings": [], "information": []}
        result = validate_malformed_json(path, report, "test")
        self.assertIsNone(result)
        self.assertEqual(report["errors"][0]["code"], "MALFORMED_JSON")

    def test_collection_map_generation(self):
        csv_path = self.write_csv([{
            "release_id": "123",
            "Artist": "Test Artist",
            "Title": "Test Title",
            "discogs_genre": "Rock",
            "discogs_style": "Blues Rock",
            "discogs_year": "1979",
        }])
        taxonomy = build_observed_taxonomy(["Rock", "Blues Rock"])
        taxonomy_path = self.write_json("taxonomy.json", taxonomy)
        relations_path = self.write_json(
            "relations.json", {"schema_version": "1.0", "relations": []})
        output_path = os.path.join(self.tempdir.name, "output", "collection_map.json")
        report_path = os.path.join(self.tempdir.name, "output", "validation_report.json")

        result = run(csv_path, taxonomy_path, relations_path,
                     output_path, report_path)
        self.assertEqual(
            result["collection_map"]["collection"]["source_row_count"], 1)
        self.assertTrue(os.path.exists(output_path))
        self.assertTrue(os.path.exists(os.path.join(
            self.tempdir.name, "output", "ambiguous_mappings.json")))
        self.assertTrue(os.path.exists(report_path))

    def test_dry_run_writes_no_files(self):
        csv_path = self.write_csv([{
            "release_id": "123",
            "Artist": "Test Artist",
            "Title": "Test Title",
            "discogs_genre": "Rock",
            "discogs_style": "",
            "discogs_year": "0",
        }])
        taxonomy_path = self.write_json(
            "taxonomy.json", build_observed_taxonomy(["Rock"]))
        relations_path = self.write_json(
            "relations.json", {"schema_version": "1.0", "relations": []})
        output_path = os.path.join(self.tempdir.name, "output", "collection_map.json")
        report_path = os.path.join(self.tempdir.name, "output", "validation_report.json")

        run(csv_path, taxonomy_path, relations_path,
            output_path, report_path, dry_run=True)
        self.assertFalse(os.path.exists(output_path))
        self.assertFalse(os.path.exists(report_path))

    def test_shared_config_paths_and_write_override(self):
        csv_path = self.write_csv([{
            "release_id": "123",
            "Artist": "Test Artist",
            "Title": "Test Title",
            "CollectionFolder": "Rock",
            "discogs_genre": "Rock",
            "discogs_style": "",
            "discogs_year": "1979",
        }, {
            "release_id": "456",
            "Artist": "For-Sale Artist",
            "Title": "For-Sale Release",
            "CollectionFolder": "For-Sale",
            "discogs_genre": "Rock",
            "discogs_style": "",
            "discogs_year": "1980",
        }])
        taxonomy_path = self.write_json(
            "taxonomy.json", build_observed_taxonomy(["Rock"]))
        relations_path = self.write_json(
            "relations.json", {"schema_version": "1.0", "relations": []})
        output_path = os.path.join(self.tempdir.name, "output", "map.json")
        report_path = os.path.join(self.tempdir.name, "output", "report.json")
        ambiguous_path = os.path.join(self.tempdir.name, "output", "ambiguous.json")
        config_path = os.path.join(self.tempdir.name, "discogs2spotify.conf")
        with open(config_path, "w", encoding="utf-8") as handle:
            handle.write(
                "DRY_RUN=true\n"
                "EXCLUDE_FOLDERS=For-Sale\n"
                "MUSIC_MAP_INPUT={}\n"
                "MUSIC_MAP_TAXONOMY={}\n"
                "MUSIC_MAP_RELATIONS={}\n"
                "MUSIC_MAP_OUTPUT={}\n"
                "MUSIC_MAP_REPORT={}\n"
                "MUSIC_MAP_AMBIGUOUS_OUTPUT={}\n".format(
                    csv_path,
                    taxonomy_path,
                    relations_path,
                    output_path,
                    report_path,
                    ambiguous_path,
                )
            )

        self.assertEqual(main(["--config", config_path]), 0)
        self.assertFalse(os.path.exists(output_path))
        self.assertEqual(main(["--config", config_path, "--write"]), 0)
        self.assertTrue(os.path.exists(output_path))
        self.assertTrue(os.path.exists(report_path))
        self.assertTrue(os.path.exists(ambiguous_path))
        with open(output_path, encoding="utf-8") as handle:
            collection_map = json.load(handle)["collection"]
        self.assertEqual(collection_map["source_row_count"], 2)
        self.assertEqual(collection_map["included_release_count"], 1)
        self.assertEqual(collection_map["excluded_release_count"], 1)
        self.assertEqual(collection_map["excluded_folders"], ["For-Sale"])
        self.assertEqual(
            [release["release_id"] for release in collection_map["releases"]],
            ["123"],
        )


if __name__ == "__main__":
    unittest.main()
