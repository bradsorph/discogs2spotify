#!/usr/bin/env python3
"""Compatibility launcher; use discogs2spotify.sh as the primary interface."""

import sys

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from discogs2spotify.main import main


if __name__ == "__main__":
    sys.exit(main())
