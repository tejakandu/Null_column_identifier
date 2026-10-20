"""Run tracking: one BigQuery row per scan, for usage analytics.

Rows are written with the streaming API (no query job, so users only need
write access to the table, not permission to run jobs in the admin project).
Writing is best effort: a failure is logged and never interrupts the user.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import __version__
from .core import TableResult
from .identity import RunIdentity

# (name, type, mode, description[, sub-fields]) — the single source of truth;
# `python -m nci.admin setup` creates the table from it.
RUN_LOG_SCHEMA = [
    ("run_id", "STRING", "REQUIRED", "Unique id of the run"),
    ("app_version", "STRING", "REQUIRED", "Version of the app that ran the scan"),
    ("started_at", "TIMESTAMP", "REQUIRED", "When the scan started (UTC)"),
    ("finished_at", "TIMESTAMP", "NULLABLE", "When the scan finished (UTC)"),
    ("duration_seconds", "FLOAT", "NULLABLE", "Wall-clock duration"),
    ("status", "STRING", "REQUIRED", "completed, completed_with_errors, cancelled or failed"),
    ("run_by_name", "STRING", "NULLABLE", "Windows display name of the person"),
    ("run_by_login", "STRING", "NULLABLE", "OS login name"),
    ("machine", "STRING", "NULLABLE", "Computer name"),
    ("os", "STRING", "NULLABLE", "Operating system"),
    ("google_account", "STRING", "NULLABLE", "Account BigQuery saw (SESSION_USER)"),
    ("auth_method", "STRING", "NULLABLE", "Google login or Service account key"),
    ("query_project", "STRING", "NULLABLE", "Project the queries ran in, if overridden"),
    ("parallel_tables", "INTEGER", "NULLABLE", "Tables scanned at the same time"),
    ("safety_limit_gb", "FLOAT", "NULLABLE", "Per-query byte limit; 0 = none"),
    ("tables_requested", "INTEGER", "NULLABLE", ""),
    ("tables_scanned", "INTEGER", "NULLABLE", ""),
    ("tables_failed", "INTEGER", "NULLABLE", ""),
    ("tables_cancelled", "INTEGER", "NULLABLE", ""),
    ("columns_checked", "INTEGER", "NULLABLE", ""),
    ("null_columns_found", "INTEGER", "NULLABLE", ""),
    ("bytes_processed", "INTEGER", "NULLABLE", "Data scanned by all queries"),
    ("report_saved", "BOOLEAN", "NULLABLE", "Whether the Excel report was written"),
    ("tables", "RECORD", "REPEATED", "One entry per table", [
        ("project", "STRING", "NULLABLE", ""),
        ("dataset", "STRING", "NULLABLE", ""),
        ("table", "STRING", "NULLABLE", ""),
        ("status", "STRING", "NULLABLE", "ok, error or cancelled"),
        ("total_rows", "INTEGER", "NULLABLE", ""),
        ("total_columns", "INTEGER", "NULLABLE", ""),
        ("null_columns", "INTEGER", "NULLABLE", ""),
        ("unchecked_columns", "INTEGER", "NULLABLE", ""),
        ("coverage", "FLOAT", "NULLABLE", "Share of columns with data, 0..1"),
        ("bytes_processed", "INTEGER", "NULLABLE", ""),
        ("seconds", "FLOAT", "NULLABLE", ""),
        ("error", "STRING", "NULLABLE", ""),
    ]),
]


def run_status(results: list[TableResult], cancelled: bool, report_saved: bool) -> str:
    if cancelled:
        return "cancelled"
    if not report_saved or (results and all(r.status == "error" for r in results)):
        return "failed"
    if any(r.status == "error" for r in results):
        return "completed_with_errors"
    return "completed"


def _ts(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local naive time -> aware
    return dt.astimezone(timezone.utc).isoformat()


def build_row(*, run_id: str, results: list[TableResult], identity: RunIdentity,
              started: datetime, finished: datetime | None, cancelled: bool, report_saved: bool,
              query_project: str = "", parallel: int = 1, safety_limit_gb: float = 0.0) -> dict:
    ok = [r for r in results if r.status == "ok"]
    return {
        "run_id": run_id,
        "app_version": __version__,
        "started_at": _ts(started),
        "finished_at": _ts(finished),
        "duration_seconds": round((finished - started).total_seconds(), 3) if finished else None,
        "status": run_status(results, cancelled, report_saved),
        "run_by_name": identity.full_name or None,
        "run_by_login": identity.login or None,
        "machine": identity.machine or None,
        "os": identity.os or None,
        "google_account": identity.google_account or None,
        "auth_method": identity.auth_method or None,
        "query_project": query_project or None,
        "parallel_tables": parallel,
        "safety_limit_gb": float(safety_limit_gb or 0),
        "tables_requested": len(results),
        "tables_scanned": len(ok),
        "tables_failed": sum(r.status == "error" for r in results),
        "tables_cancelled": sum(r.status == "cancelled" for r in results),
        "columns_checked": sum(len(r.columns) for r in ok),
        "null_columns_found": sum(len(r.null_columns) for r in ok),
        "bytes_processed": sum(r.bytes_processed for r in results),
        "report_saved": report_saved,
        "tables": [{
            "project": r.target.project,
            "dataset": r.target.dataset,
            "table": r.target.table,
            "status": r.status,
            "total_rows": r.total_rows,
            "total_columns": len(r.columns) if r.columns else None,
            "null_columns": len(r.null_columns) if r.status == "ok" else None,
            "unchecked_columns": len(r.error_columns) if r.status == "ok" else None,
            "coverage": round(r.coverage, 6) if r.coverage is not None else None,
            "bytes_processed": r.bytes_processed,
            "seconds": round(r.seconds, 3),
            "error": (r.error or None) and r.error[:1000],
        } for r in results],
    }


def record_run(client, table: str, row: dict, timeout: float = 30.0) -> None:
    """Insert the row; raises with BigQuery's message if it is rejected."""
    errors = client.insert_rows_json(table, [row], row_ids=[row["run_id"]], timeout=timeout)
    if errors:
        raise RuntimeError(f"BigQuery rejected the run row: {errors}")


def to_bigquery_schema(spec=RUN_LOG_SCHEMA):
    from google.cloud import bigquery

    fields = []
    for name, ftype, mode, desc, *sub in spec:
        fields.append(bigquery.SchemaField(name, ftype, mode=mode, description=desc or None,
                                           fields=to_bigquery_schema(sub[0]) if sub else ()))
    return fields
