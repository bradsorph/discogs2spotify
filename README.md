# discogs2spotify

Synchronize the artwork and concise edition metadata of an exact Discogs release
to its mapped Spotify playlist. Each configured playlist is explicitly paired
with a Discogs release ID; this utility does not search for artist or album
images. It complements an existing track-list workflow and does not modify
playlist tracks.

## Requirements

- Python 3.9+
- A Spotify user access token with the `ugc-image-upload` scope and permission
  to edit the configured playlists
- Pillow (`python3 -m pip install -r requirements.txt`)

Discogs release data and its image are retrieved from the release ID in the
configuration. The default artwork selection is the release's primary image.
Image conversion and compression happen in memory; no artwork files are
persisted. The base64 JPEG sent to Spotify is kept within 256 KB.

## Configure

Copy `config.example.json` to `config.json`, then set the real Spotify playlist
IDs and exact Discogs release IDs. Keep `config.json` private; it is ignored by
Git. Set `SPOTIFY_ACCESS_TOKEN` in the environment to a valid user token that
includes `ugc-image-upload`. The utility does not create or refresh OAuth
tokens.

The description defaults are configurable in `config.json` or with environment
variables of the same names:

```text
PLAYLIST_DESCRIPTION=true
DESCRIPTION_INCLUDE_CATALOG_NUMBER=true
DESCRIPTION_INCLUDE_FORMAT=true
DESCRIPTION_INCLUDE_GENRE=true
DESCRIPTION_INCLUDE_STYLE=true
DESCRIPTION_INCLUDE_NOTES=true
DESCRIPTION_INCLUDE_MATRIX=false
DESCRIPTION_INCLUDE_DISCOGS_URL=true
```

Only available fields are included. Notes are shortened, credits are excluded,
and descriptions are limited to Spotify's 300-character limit. The Discogs
release ID and URL are included by default. Set `ARTWORK_IMAGE_INDEX` to
`primary` (default), or to a zero-based index in the release's image list to
select another image.

## Run

```bash
python3 -m pip install -r requirements.txt
cp config.example.json config.json
export SPOTIFY_ACCESS_TOKEN='your-user-access-token'
./sync-playlists.sh
```

You can also run `python3 discogs2spotify.py --config /path/to/config.json`.
By default, state is stored in `.discogs2spotify.sqlite3` beside the config;
override this with `--state-db`. SQLite records a deterministic hash of each
description and source artwork per playlist/release pair, so unchanged
metadata is not sent again. An absent Spotify cover also triggers an upload.
If Discogs has no usable release image, the existing Spotify cover is left
untouched and the report says `COVER MISSING`.

The report prints `COVER CREATED`, `COVER UPDATED`, `COVER UNCHANGED`, or
`COVER MISSING`, and the corresponding description status. Description and
cover hashes are only saved after the related Spotify update succeeds.
