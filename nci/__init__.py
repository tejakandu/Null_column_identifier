"""Null Column Identifier: find BigQuery columns that hold no values at all."""

import os as _os

# Version of the source code. Releases don't edit this: `release.bat` stamps
# the version it publishes into _build_version.txt, which is bundled into the
# .exe and never committed.
BASE_VERSION = "2.2.1"
BUILD_VERSION_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "_build_version.txt")


def _read_version() -> str:
    try:
        with open(BUILD_VERSION_FILE, encoding="utf-8") as fh:
            stamped = fh.read().strip()
    except OSError:
        stamped = ""
    return stamped or BASE_VERSION


__version__ = _read_version()
APP_NAME = "Null Column Identifier"
DESIGNED_BY = "Teja Kandukuri"
AUTHOR = "Balasudharam Murugesan"
CREDITS = f"Designed by {DESIGNED_BY} · Author: {AUTHOR}"
