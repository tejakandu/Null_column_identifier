"""Admin settings shipped with the app (``nci_config.json``).

The copy bundled inside the app is read first. An ``nci_config.json`` next to
the .exe, or the file named by ``NCI_CONFIG``, can then change values
without a rebuild. Blank values in those files are ignored, so dropping in
an empty file cannot switch off run tracking or update checks.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass

FILE_NAME = "nci_config.json"


@dataclass(frozen=True)
class AppConfig:
    run_log_table: str = ""     # project.dataset.table for one row per run
    releases_table: str = ""    # project.dataset.table of published versions
    download_url: str = ""      # fallback link when a release row has none
    update_check_minutes: int = 60
    source: str = ""            # file the settings came from

    @property
    def tracking_enabled(self) -> bool:
        return self.run_log_table.count(".") == 2

    @property
    def updates_enabled(self) -> bool:
        return self.releases_table.count(".") == 2


def candidate_paths() -> list[str]:
    """Bundled file first, then overrides (later files win)."""
    paths = [os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_NAME)]
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(os.path.dirname(sys.executable), FILE_NAME))
    if os.environ.get("NCI_CONFIG"):
        paths.append(os.environ["NCI_CONFIG"])
    return paths


def load_config() -> AppConfig:
    merged: dict = {}
    source = ""
    for path in candidate_paths():
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        merged.update({k: v for k, v in data.items() if v not in (None, "")})
        source = path
    minutes = str(merged.get("update_check_minutes", 60))
    return AppConfig(
        run_log_table=str(merged.get("run_log_table") or "").strip(),
        releases_table=str(merged.get("releases_table") or "").strip(),
        download_url=str(merged.get("download_url") or "").strip(),
        update_check_minutes=max(5, int(minutes)) if minutes.isdigit() else 60,
        source=source,
    )
