"""App releases and forced updates.

Admins publish a version by adding a row to the releases table
(``python -m nci.admin release ...``). On start-up and every hour the app
reads that table. If a *mandatory* release newer than the running version
exists, the app tells the user to download it and then quits; a newer
optional release only shows an "update available" notice.

The table is read with ``list_rows`` (free, no query job: users only need
read access). If the check cannot run (offline, no permission, no sign-in)
the app keeps working: it fails open, so a misconfiguration never locks
everyone out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from . import __version__

RELEASES_SCHEMA = [
    ("version", "STRING", "REQUIRED", "Version number, e.g. 2.3.0"),
    ("released_at", "TIMESTAMP", "REQUIRED", "When it was published"),
    ("mandatory", "BOOLEAN", "REQUIRED", "TRUE = older versions must update and will close"),
    ("download_url", "STRING", "NULLABLE", "Where users download this version"),
    ("release_notes", "STRING", "NULLABLE", "What changed, shown to users"),
    ("released_by", "STRING", "NULLABLE", "Who published it"),
    ("withdrawn", "BOOLEAN", "NULLABLE", "TRUE = ignore this row (pulled release)"),
]

MAX_ROWS = 500


def parse_version(text: str) -> tuple[int, ...]:
    """'2.10.1' -> (2, 10, 1); ignores a leading 'v' and suffixes like '-beta'."""
    nums = re.findall(r"\d+", str(text).split("-")[0].split("+")[0])
    parts = [int(n) for n in nums[:4]] or [0]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


@dataclass(frozen=True)
class Release:
    version: str
    released_at: datetime | None = None
    mandatory: bool = False
    download_url: str = ""
    release_notes: str = ""
    released_by: str = ""

    @property
    def key(self) -> tuple[int, ...]:
        return parse_version(self.version)


@dataclass(frozen=True)
class UpdateDecision:
    kind: str                        # "none", "optional" or "forced"
    latest: Release | None = None    # newest release overall
    required: Release | None = None  # newest mandatory release, when forced
    notes: tuple[str, ...] = ()      # release notes of every newer version, newest first

    @property
    def target(self) -> Release | None:
        return self.latest if self.kind != "none" else None


def decide(releases: list[Release], current: str = __version__) -> UpdateDecision:
    now = parse_version(current)
    newer = sorted((r for r in releases if r.key > now), key=lambda r: r.key, reverse=True)
    if not newer:
        return UpdateDecision("none", max(releases, key=lambda r: r.key, default=None))
    notes = tuple(f"{r.version}: {r.release_notes}" for r in newer if r.release_notes)
    mandatory = [r for r in newer if r.mandatory]
    if mandatory:
        return UpdateDecision("forced", newer[0], mandatory[0], notes)
    return UpdateDecision("optional", newer[0], None, notes)


def fetch_releases(client, table: str, timeout: float = 20.0) -> list[Release]:
    rows = client.list_rows(table, max_results=MAX_ROWS, timeout=timeout)
    out = []
    for row in rows:
        data = dict(row.items())
        if data.get("withdrawn") or not data.get("version"):
            continue
        out.append(Release(
            version=str(data["version"]).strip(),
            released_at=data.get("released_at"),
            mandatory=bool(data.get("mandatory")),
            download_url=str(data.get("download_url") or ""),
            release_notes=str(data.get("release_notes") or ""),
            released_by=str(data.get("released_by") or ""),
        ))
    return out


def check_for_update(client, table: str, current: str = __version__) -> UpdateDecision:
    return decide(fetch_releases(client, table), current)
