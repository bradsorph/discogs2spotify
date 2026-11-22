# Discogs to Graph

`discogs2graph.py` builds a conservative, updateable music-history graph from a
Discogs collection CSV. It does not create a visualization or infer historical
relationships.

## Project purpose

The project separates three kinds of data:

1. **Collection data** - the original Discogs CSV values plus parsed release
   metadata.
2. **Canonical taxonomy** - stable music-concept nodes in `data/taxonomy.json`.
3. **Historical graph** - separately reviewed relationships in
   `data/relations.json`.

This separation prevents collection facts, taxonomy choices, and historical
interpretation from being mixed together.

## Architecture

```text
discogs2spotify/
  discogs2graph.py
  data/
    collection.csv
    taxonomy.json
    relations.json
    artist_relations.json
  output/
    collection_map.json
    ambiguous_mappings.json
    validation_report.json
  src/
    discogs2graph/
      csv_loader.py
      normalizer.py
      taxonomy.py
      graph.py
      validator.py
      main.py
  tests/
    test_discogs2graph.py
```

## Data flow

1. `csv_loader.py` reads the CSV and preserves every original column in `raw`.
2. Genre and style strings are split on semicolons.
3. `normalizer.py` maps observed Discogs values to stable canonical IDs.
4. `taxonomy.py` loads the canonical nodes.
5. `graph.py` calculates collection coverage without inventing missing nodes.
6. `validator.py` checks identifiers, mappings, graph endpoints, schema
   versions, and relationship values.
7. `main.py` writes the generated JSON files.

## Collection CSV

The input columns are detected from the actual export. Known collection and
Discogs fields include `release_id`, `Artist`, `Title`, `discogs_genre`,
`discogs_style`, `discogs_year`, `discogs_country`, `discogs_format`,
collection conditions, folder, date, label, catalog number, and Discogs URL.

Every release preserves the original row unchanged in `raw`. A year of `0` is
treated as unknown rather than converted to an actual calendar year.

## Taxonomy structure

`data/taxonomy.json` contains:

```json
{
  "schema_version": "1.0",
  "nodes": [
    {
      "id": "delta_blues",
      "name": "Delta Blues",
      "category": "style",
      "description": "Optional description."
    }
  ]
}
```

IDs are permanent machine-readable identifiers; display names are never used
as internal graph IDs. Node categories may be `genre`, `style`, `scene`,
`tradition`, or another explicit category.

To add or change a node:

1. Choose a stable ID.
2. Add the node to `taxonomy.json`.
3. Update normalization mappings if necessary.
4. Run the validator or full pipeline.
5. Never rename an existing ID merely to rename its display label.

## Historical relations

`data/relations.json` contains directed relationships:

```json
{
  "schema_version": "1.0",
  "relations": [
    {
      "from": "country_blues",
      "to": "delta_blues",
      "type": "regional_development",
      "strength": 0.8,
      "confidence": 0.9,
      "status": "verified"
    }
  ]
}
```

Valid relation types are `influence`, `evolved_into`, `derived_from`,
`precursor`, `parallel`, `associated_with`, `scene`,
`regional_development`, and `artist_bridge`.

Valid statuses are `verified`, `proposed`, `uncertain`, and `ambiguous`.
Strength and confidence must be in the range `0` to `1`. The initial relations
file is intentionally empty, so no music history is invented.

To add a relation:

1. Verify that both node IDs exist.
2. Choose the relation type that best describes the relationship.
3. Set evidence, source, status, strength, and confidence if available.
4. Run the CLI and review the validation report.

Artist relationships may later be stored in `data/artist_relations.json`.

## Mapping process

Discogs genres and styles are split into separate values. Each value is mapped
to one or more canonical node IDs. The first version uses a conservative exact
source-to-ID mapping. It does not create inferred mappings.

If a Discogs value cannot be confidently mapped, it appears in
`output/ambiguous_mappings.json` or as an unmapped source value in the
validation report. Repeated `needs_review` entries can be resolved as the
taxonomy develops.

## Validation

The validation report separates:

- `errors`: invalid data that makes the graph inconsistent
- `warnings`: incomplete mappings or review-needed data
- `information`: dataset and coverage summaries

It checks duplicate IDs, missing release/artist/title fields, malformed JSON,
schema versions, relation endpoints and types, confidence and strength ranges,
and unmapped values.

## CLI usage

The graph utility reads the same `discogs2spotify.conf` file as the main
Discogs2Spotify application. The `MUSIC_MAP_*` paths in that file are relative
to the configuration file; command-line path arguments override them. The
shared `EXCLUDE_FOLDERS` setting also applies: releases in those Discogs
collection folders are omitted from the generated graph, and included/excluded
counts are recorded in the output.

Run from the `discogs2spotify` project directory:

```bash
python3 discogs2graph.py
```

Specify a different config or override paths:

```bash
python3 discogs2graph.py \
  --config /path/to/discogs2spotify.conf \
  --input data/collection.csv \
  --taxonomy data/taxonomy.json \
  --relations data/relations.json \
  --output output/collection_map.json \
  --report output/validation_report.json
```

The shared `DRY_RUN` setting controls whether files are written. Use
`--dry-run` to force a dry run or `--write` to generate outputs regardless of
that setting.

```bash
python3 discogs2graph.py --dry-run
python3 discogs2graph.py --write
```

The six graph-specific config keys are `MUSIC_MAP_INPUT`,
`MUSIC_MAP_TAXONOMY`, `MUSIC_MAP_RELATIONS`, `MUSIC_MAP_OUTPUT`,
`MUSIC_MAP_REPORT`, and `MUSIC_MAP_AMBIGUOUS_OUTPUT`. Defaults are the
corresponding files under `data/` and `output/`.

## Testing

```bash
python3 -m unittest discover -s tests -v
```

## Consuming `collection_map.json`

Future visualization tools can use:

- `collection.releases` for owned releases and raw Discogs fields
- `taxonomy.nodes` for canonical graph nodes
- `relations` for verified or proposed historical edges
- `coverage.represented_node_ids` for historical nodes represented by the
  collection
- `coverage.not_represented_node_ids` for taxonomy gaps
- Release `canonical_mapping` fields for release-to-node membership

Missing nodes are only gaps in collection coverage. They are not purchase
recommendations.
