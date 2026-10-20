"""A tiny in-memory stand-in for google.cloud.bigquery.Client.

It evaluates the generated COUNTIF predicates against Python rows, so tests
check real null-detection results, not just SQL text.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from google.api_core.exceptions import BadRequest, NotFound


class FakeJob:
    def __init__(self, row, total_bytes=1024):
        self._row = row
        self.total_bytes_processed = total_bytes
        self.cancelled = False

    def done(self):
        return True

    def result(self, timeout=None):
        return [self._row]

    def cancel(self):
        self.cancelled = True


class FakeClient:
    """Tables: {ref: (schema, rows)}; rows are nested dicts."""

    def __init__(self, tables, bad_columns=(), fail_query=None, session_user="tester@example.com",
                 releases=(), insert_error=None):
        self.tables = tables
        self.bad_columns = set(bad_columns)  # display names whose SQL "fails"
        self.fail_query = fail_query
        self.queries = []
        self.session_user = session_user
        self.releases = list(releases)       # rows (dicts) returned by list_rows
        self.inserted = []                   # (table, rows) from insert_rows_json
        self.insert_error = insert_error

    def get_table(self, ref):
        if ref not in self.tables:
            raise NotFound(f"Table {ref} was not found")
        schema, rows = self.tables[ref]
        return SimpleNamespace(schema=schema, num_rows=len(rows), table_type="TABLE")

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        if "SESSION_USER()" in sql:
            if isinstance(self.session_user, Exception):
                raise self.session_user
            return FakeJob({"who": self.session_user}, 0)
        if self.fail_query:
            raise self.fail_query
        from nci.core import flatten_schema

        ref = re.search(r"FROM `(.+?)`\.`(.+?)`\.`(.+?)` AS t", sql).groups()
        schema, rows = self.tables[".".join(ref)]
        specs = {s.predicate: s for s in flatten_schema(schema)}
        out = {"total_rows": len(rows)}
        for pred, alias in re.findall(r"COUNTIF\((.+)\) AS (c\d+)$", sql, flags=re.M):
            spec = specs[pred]
            if spec.name in self.bad_columns:
                raise BadRequest(f"Syntax error near {spec.name}")
            out[alias] = sum(1 for r in rows if _has_value(r, spec.name.split(".")))
        return FakeJob(out)

    def insert_rows_json(self, table, rows, row_ids=None, timeout=None):
        if isinstance(self.insert_error, Exception):
            raise self.insert_error
        if self.insert_error:
            return [{"index": 0, "errors": [{"reason": "invalid", "message": self.insert_error}]}]
        self.inserted.append((table, rows))
        return []

    def list_rows(self, table, max_results=None, timeout=None):
        return [_Row(r) for r in self.releases]


def _has_value(value, path):
    if not path:
        if isinstance(value, list):
            return len(value) > 0
        return value is not None
    if value is None:
        return False
    if isinstance(value, list):
        return any(_has_value(v, path) for v in value)
    return _has_value(value.get(path[0]), path[1:])


class FakeCatalogClient(FakeClient):
    """Adds the listing calls used by the Browse dialog."""

    def list_projects(self):
        return [SimpleNamespace(project_id=p) for p in sorted({r.split(".")[0] for r in self.tables})]

    def list_datasets(self, project):
        names = sorted({r.split(".")[1] for r in self.tables if r.startswith(project + ".")})
        return [SimpleNamespace(dataset_id=d) for d in names]

    def list_tables(self, dataset_ref):
        return [SimpleNamespace(table_id=r.split(".")[2], table_type="TABLE")
                for r in sorted(self.tables) if r.startswith(dataset_ref + ".")]


class _Row(dict):
    """Mimics google.cloud.bigquery.Row enough for list_rows users."""
