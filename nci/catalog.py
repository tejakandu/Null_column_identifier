"""Listing projects / datasets / tables, with an on-disk cache.

Big organisations have hundreds of projects and tens of thousands of tables.
Listing them is free but slow (one API call per page per dataset), so every
listing is cached on disk per sign-in identity and reused until the user
clicks Refresh. ``load_tables_bulk`` lists many datasets in parallel so a
search can cover a whole project quickly.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

TableInfo = tuple[str, str]  # (table_id, type label)


class Catalog:
    def __init__(self, client_for_project: Callable[[str], object], cache_dir: str = "",
                 identity: str = "default"):
        self.client_for_project = client_for_project
        self.cache_dir = cache_dir
        self.identity = hashlib.sha1(identity.encode()).hexdigest()[:12]
        self._lock = threading.Lock()
        self._mem: dict[str, dict] = {}

    # -- cache -------------------------------------------------------------------

    def _path(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
        return os.path.join(self.cache_dir, self.identity, safe + ".json")

    def _read(self, name: str) -> dict:
        with self._lock:
            if name in self._mem:
                return self._mem[name]
        data = {}
        if self.cache_dir:
            try:
                with open(self._path(name), encoding="utf-8") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                data = {}
        with self._lock:
            self._mem.setdefault(name, data)
            return self._mem[name]

    def _write(self, name: str) -> None:
        if not self.cache_dir:
            return
        with self._lock:
            data = json.dumps(self._mem.get(name, {}))
        path = self._path(name)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            pass  # the cache is only a speed-up

    # -- listings ------------------------------------------------------------------

    def projects(self, refresh: bool = False) -> tuple[list[str], float]:
        data = self._read("_projects")
        if refresh or "items" not in data:
            client = self.client_for_project("")
            items = sorted(p.project_id for p in client.list_projects())
            with self._lock:
                data.update(items=items, fetched=time.time())
            self._write("_projects")
        return list(data["items"]), data["fetched"]

    def datasets(self, project: str, refresh: bool = False) -> tuple[list[str], float]:
        data = self._read(project)
        if refresh or "datasets" not in data:
            client = self.client_for_project(project)
            names = sorted(d.dataset_id for d in client.list_datasets(project))
            with self._lock:
                old = data.get("tables", {})
                data.update(datasets=names, fetched=time.time(),
                            tables={} if refresh else {k: v for k, v in old.items() if k in names})
            self._write(project)
        return list(data["datasets"]), data["fetched"]

    def cached_tables(self, project: str, dataset: str) -> list[TableInfo] | None:
        entry = self._read(project).get("tables", {}).get(dataset)
        return [tuple(t) for t in entry] if entry is not None else None

    def tables(self, project: str, dataset: str, refresh: bool = False,
               save: bool = True) -> list[TableInfo]:
        if not refresh:
            cached = self.cached_tables(project, dataset)
            if cached is not None:
                return cached
        client = self.client_for_project(project)
        items = sorted((t.table_id, _type_label(t.table_type))
                       for t in client.list_tables(f"{project}.{dataset}"))
        data = self._read(project)
        with self._lock:
            data.setdefault("tables", {})[dataset] = [list(t) for t in items]
        if save:
            self._write(project)
        return items

    def load_tables_bulk(self, project: str, datasets: list[str],
                         on_loaded: Callable[[str, list[TableInfo] | None, str], None],
                         cancel: threading.Event, workers: int = 8) -> None:
        """List many datasets in parallel; calls on_loaded(dataset, tables, error)."""
        todo = [d for d in datasets if self.cached_tables(project, d) is None]
        for d in datasets:
            if d not in todo:
                on_loaded(d, self.cached_tables(project, d), "")
        if not todo:
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(self._list_one, project, d, cancel): d for d in todo}
            for fut in as_completed(futures):
                if cancel.is_set():
                    for f in futures:
                        f.cancel()
                    break
                d = futures[fut]
                try:
                    on_loaded(d, fut.result(), "")
                except Exception as exc:  # noqa: BLE001 - reported per dataset
                    on_loaded(d, None, str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__)
        self._write(project)

    def _list_one(self, project: str, dataset: str, cancel: threading.Event):
        if cancel.is_set():
            return []
        return self.tables(project, dataset, refresh=True, save=False)


def _type_label(table_type: str | None) -> str:
    return {"TABLE": "Table", "VIEW": "View", "MATERIALIZED_VIEW": "Materialized view",
            "EXTERNAL": "External", "SNAPSHOT": "Snapshot"}.get(table_type or "TABLE",
                                                                (table_type or "").title())


def age_text(fetched: float) -> str:
    seconds = max(0, time.time() - fetched)
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400 * 2:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} days ago"
