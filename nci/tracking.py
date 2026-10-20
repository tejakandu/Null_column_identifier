"""Run tracking: one BigQuery row per table scanned, for usage analytics.

The columns mirror the Overview sheet of the Excel report, plus the run id,
time and user, so each row stands on its own (no joins or UNNEST needed).

Rows are written with the streaming API (no query job, so users only need
write access to the table, not permission to run jobs in the admin project).
Writing is best effort: a failure is logged and never interrupts the user.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import __version__
from .core import TableResult
from .identity import RunIdentity
from .report import table_summary

# (name, type, mode, description) — the single source of truth;
# `python -m nci.admin setup` creates the table from it.
RUN_LOG_SCHEMA = [
    ("run_id", "STRING", "REQUIRED", "Id of the run; shared by all tables scanned together"),
    ("execution_time", "TIMESTAMP", "REQUIRED", "When the run started (UTC)"),
    ("execution_user", "STRING", "NULLABLE", "Person who ran it, e.g. Jane Doe (jdoe) on LAPTOP-42"),
    ("project", "STRING", "REQUIRED", "Project of the scanned table"),
    ("dataset", "STRING", "REQUIRED", "Dataset of the scanned table"),
    ("table", "STRING", "REQUIRED", "Scanned table"),
    ("status", "STRING", "REQUIRED", "Scanned, Failed or Cancelled"),
    ("total_rows", "INTEGER", "NULLABLE", "Rows in the table"),
    ("total_columns", "INTEGER", "NULLABLE", "Columns checked, nested fields included"),
    ("null_columns", "INTEGER", "NULLABLE", "Columns with no value in any row"),
    ("columns_with_data", "INTEGER", "NULLABLE", "Columns with at least one value"),
    ("coverage", "FLOAT", "NULLABLE", "Percent of columns with data (0-100)"),
    ("not_covered", "FLOAT", "NULLABLE", "Percent of columns that are null (0-100)"),
    ("message", "STRING", "NULLABLE", "Error or note for this table"),
    ("google_account", "STRING", "NULLABLE", "Google account BigQuery saw (SESSION_USER)"),
    ("app_version", "STRING", "REQUIRED", "Version of the app that ran the scan"),
]

INSERT_BATCH = 500  # rows per streaming request (BigQuery recommends <= 500)


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local naive time -> aware
    return dt.astimezone(timezone.utc).isoformat()


def _percent(fraction: float | None) -> float | None:
    return None if fraction is None else round(fraction * 100, 2)


def build_rows(*, run_id: str, results: list[TableResult], identity: RunIdentity,
               started: datetime) -> list[dict]:
    when = _ts(started)
    user = identity.display if (identity.full_name or identity.login) else None
    rows = []
    for res in results:
        t = table_summary(res)
        rows.append({
            "run_id": run_id,
            "execution_time": when,
            "execution_user": user,
            "project": t["project"],
            "dataset": t["dataset"],
            "table": t["table"],
            "status": t["status"],
            "total_rows": t["total_rows"],
            "total_columns": t["total_columns"],
            "null_columns": t["null_columns"],
            "columns_with_data": t["columns_with_data"],
            "coverage": _percent(t["coverage"]),
            "not_covered": _percent(t["not_covered"]),
            "message": t["message"][:2000] or None,
            "google_account": identity.google_account or None,
            "app_version": __version__,
        })
    return rows


def record_run(client, table: str, rows: list[dict], timeout: float = 30.0) -> None:
    """Insert the rows; raises with BigQuery's message if any are rejected.

    Row ids (run id + position) let BigQuery drop duplicates if a request is
    retried.
    """
    for start in range(0, len(rows), INSERT_BATCH):
        batch = rows[start:start + INSERT_BATCH]
        ids = [f"{r['run_id']}:{start + i}" for i, r in enumerate(batch)]
        errors = client.insert_rows_json(table, batch, row_ids=ids, timeout=timeout)
        if errors:
            raise RuntimeError(f"BigQuery rejected {len(errors)} run-tracking row(s): {errors[:3]}")


def to_bigquery_schema(spec=RUN_LOG_SCHEMA):
    from google.cloud import bigquery

    fields = []
    for name, ftype, mode, desc, *sub in spec:
        fields.append(bigquery.SchemaField(name, ftype, mode=mode, description=desc or None,
                                           fields=to_bigquery_schema(sub[0]) if sub else ()))
    return fields
