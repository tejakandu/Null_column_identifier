"""Scanning engine: find columns in a BigQuery table that have no non-NULL value.

Every leaf field of the schema (including fields nested in RECORDs and in
REPEATED RECORDs at any depth) becomes one boolean predicate meaning "this row
has a value for this field". A single query per table then counts, for every
field at once, how many rows satisfy its predicate:

    SELECT COUNT(*) AS total_rows,
           COUNTIF(t.`a` IS NOT NULL)                    AS c0,
           COUNTIF(ARRAY_LENGTH(t.`tags`) > 0)           AS c1,
           COUNTIF(EXISTS(SELECT 1 FROM UNNEST(t.`items`) AS _u0
                          WHERE _u0.`sku` IS NOT NULL))  AS c2
    FROM `project`.`dataset`.`table` AS t

A field whose count is 0 is a null column. Very wide tables are split into
batches, and a batch that BigQuery rejects is split in half until the bad
field is isolated, so one odd column never sinks the whole table.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable, Sequence

BATCH_SIZE = 400
JOB_LABELS = {"app": "null-column-identifier"}
POLL_SECONDS = 0.5


class ScanCancelled(Exception):
    """Raised when the user cancels a scan."""


# --------------------------------------------------------------------------- targets


@dataclass(frozen=True)
class TableTarget:
    project: str
    dataset: str
    table: str
    key_file: str = ""  # service-account JSON for this table; empty = app default

    @property
    def ref(self) -> str:
        return f"{self.project}.{self.dataset}.{self.table}"

    @property
    def sql_ref(self) -> str:
        return ".".join(quote_ident(p) for p in (self.project, self.dataset, self.table))

    @classmethod
    def parse(cls, text: str, key_file: str = "", default_project: str = "",
              default_dataset: str = "") -> "TableTarget":
        """Parse ``project.dataset.table`` (or ``project:dataset.table``).

        Missing leading parts are filled from the defaults, so ``dataset.table``
        or a bare ``table`` also work when defaults are given.
        """
        cleaned = text.strip().strip("`")
        head, sep, rest = cleaned.partition(":")
        if sep and "." not in head:
            cleaned = f"{head}.{rest}"  # legacy "project:dataset.table"
        parts = [p.strip().strip("`") for p in cleaned.split(".") if p.strip()]
        if len(parts) > 3:
            # Domain-scoped projects ("example.com:proj") contain a dot.
            parts = [".".join(parts[:-2])] + parts[-2:]
        if len(parts) == 2 and default_project:
            parts = [default_project] + parts
        elif len(parts) == 1 and default_project and default_dataset:
            parts = [default_project, default_dataset] + parts
        if len(parts) != 3 or not all(parts):
            raise ValueError(f"'{text.strip()}' is not in the form project.dataset.table")
        return cls(parts[0], parts[1], parts[2], key_file)


def quote_ident(name: str) -> str:
    return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"


# --------------------------------------------------------------------------- schema


@dataclass(frozen=True)
class ColumnSpec:
    name: str        # display path, e.g. "items.sku"
    data_type: str   # BigQuery type of the leaf, e.g. "STRING"
    mode: str        # NULLABLE / REQUIRED / REPEATED (of the leaf itself)
    predicate: str   # SQL boolean: this row has a value for the field
    in_array: bool = False  # True if it sits inside a REPEATED RECORD


def flatten_schema(schema: Iterable) -> list[ColumnSpec]:
    """Turn a BigQuery schema into one ColumnSpec per leaf field."""
    columns: list[ColumnSpec] = []
    _walk(list(schema), "t", "", lambda pred: pred, 0, False, columns)
    return columns


def _walk(fields, ctx: str, prefix: str, wrap: Callable[[str], str], depth: int,
          in_array: bool, out: list[ColumnSpec]) -> None:
    for f in fields:
        ident = f"{ctx}.{quote_ident(f.name)}"
        display = f"{prefix}.{f.name}" if prefix else f.name
        mode = (f.mode or "NULLABLE").upper()
        ftype = (f.field_type or "").upper()
        if ftype in ("RECORD", "STRUCT"):
            if mode == "REPEATED":
                alias = f"_u{depth}"

                def inner_wrap(pred, ident=ident, alias=alias, wrap=wrap):
                    return wrap(f"EXISTS(SELECT 1 FROM UNNEST({ident}) AS {alias} WHERE {pred})")

                _walk(f.fields, alias, display, inner_wrap, depth + 1, True, out)
            else:
                _walk(f.fields, ident, display, wrap, depth, in_array, out)
        else:
            # BigQuery stores an empty array rather than NULL, so an array
            # column "has a value" only when it has at least one element.
            pred = f"ARRAY_LENGTH({ident}) > 0" if mode == "REPEATED" else f"{ident} IS NOT NULL"
            out.append(ColumnSpec(display, ftype, mode, wrap(pred), in_array))


def build_query(target: TableTarget, columns: Sequence[ColumnSpec],
                indices: Sequence[int] | None = None) -> str:
    """One query counting total rows and non-null rows for every column given.

    Result columns are ``total_rows`` and ``c{i}`` for each index in
    ``indices`` (default 0, 1, 2, ...).
    """
    if indices is None:
        indices = range(len(columns))
    lines = ["SELECT COUNT(*) AS total_rows"]
    for i, col in zip(indices, columns):
        lines.append(f"  , COUNTIF({col.predicate}) AS c{i}")
    lines.append(f"FROM {target.sql_ref} AS t")
    return "\n".join(lines)


# --------------------------------------------------------------------------- results


@dataclass
class ColumnResult:
    spec: ColumnSpec
    non_null: int | None = None  # None = could not be checked
    error: str = ""

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def is_null(self) -> bool:
        return self.non_null == 0


@dataclass
class TableResult:
    target: TableTarget
    status: str = "pending"  # ok / error / cancelled
    total_rows: int | None = None
    columns: list[ColumnResult] = field(default_factory=list)
    error: str = ""
    table_type: str = ""
    bytes_processed: int = 0
    started: datetime | None = None
    finished: datetime | None = None

    @property
    def null_columns(self) -> list[ColumnResult]:
        return [c for c in self.columns if c.is_null]

    @property
    def data_columns(self) -> list[ColumnResult]:
        return [c for c in self.columns if c.non_null]

    @property
    def error_columns(self) -> list[ColumnResult]:
        return [c for c in self.columns if c.non_null is None]

    @property
    def coverage(self) -> float | None:
        """Share of columns that have at least one value (0..1)."""
        if not self.columns or self.status != "ok":
            return None
        return len(self.data_columns) / len(self.columns)

    @property
    def seconds(self) -> float:
        if self.started and self.finished:
            return (self.finished - self.started).total_seconds()
        return 0.0


# --------------------------------------------------------------------------- clients


def make_client(project: str, key_file: str = ""):
    """BigQuery client from a service-account key file, or the Google login."""
    from google.cloud import bigquery

    if key_file:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            key_file, scopes=["https://www.googleapis.com/auth/cloud-platform"])
        return bigquery.Client(project=project or creds.project_id, credentials=creds)
    return bigquery.Client(project=project or None)


class ClientPool:
    """Caches clients per (project, key file), one set per thread.

    Scans and catalog listings run on several threads at once; giving each
    thread its own client avoids sharing one HTTP session between threads.
    """

    def __init__(self, factory: Callable[[str, str], object] = make_client):
        self._factory = factory
        self._local = threading.local()

    def get(self, project: str, key_file: str = ""):
        clients = getattr(self._local, "clients", None)
        if clients is None:
            clients = self._local.clients = {}
        key = (project, key_file)
        if key not in clients:
            clients[key] = self._factory(project, key_file)
        return clients[key]


def friendly_error(exc: BaseException) -> str:
    """Short, human readable message for common Google/BigQuery errors."""
    text = str(exc)
    name = type(exc).__name__
    lowered = text.lower()
    if name == "DefaultCredentialsError" or "default credentials" in lowered:
        return ("No Google login found. Choose a service-account key file, or run "
                "'gcloud auth application-default login' and try again.")
    if "bytesbilledlimitexceeded" in lowered.replace(" ", "") or "bytes billed" in lowered:
        return "Skipped: the query would scan more data than the safety limit allows."
    if name == "NotFound" or " 404 " in f" {text} ":
        return "Not found: check the project, dataset and table names. " + _first_line(text)
    if name == "Forbidden" or "access denied" in lowered or "permission" in lowered:
        return "Access denied: the account has no permission for this table. " + _first_line(text)
    if "partition" in lowered and "filter" in lowered:
        return "This table requires a partition filter, so a full scan is not allowed. " + _first_line(text)
    if isinstance(exc, FileNotFoundError):
        return f"Key file not found: {exc.filename}"
    return _first_line(text) or name


def _first_line(text: str) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line[:400]


def _is_query_error(exc: BaseException) -> bool:
    """True for errors caused by the SQL itself (worth splitting the batch)."""
    lowered = str(exc).lower()
    if "bytesbilledlimitexceeded" in lowered.replace(" ", "") or "bytes billed" in lowered:
        return False
    return type(exc).__name__ in ("BadRequest", "InvalidArgument") or "invalidquery" in lowered.replace(" ", "")


# --------------------------------------------------------------------------- scanner


ProgressFn = Callable[[int, int], None]
LogFn = Callable[[str], None]


class Scanner:
    """Runs null-column scans. Safe to use from a worker thread."""

    def __init__(self, client_for: Callable[[TableTarget], object], *,
                 max_bytes_billed: int | None = None, batch_size: int = BATCH_SIZE,
                 log: LogFn | None = None, cancel_event: threading.Event | None = None):
        self.client_for = client_for
        self.max_bytes_billed = max_bytes_billed or None
        self.batch_size = max(1, batch_size)
        self.log = log or (lambda msg: None)
        self.cancel_event = cancel_event or threading.Event()

    # -- public ---------------------------------------------------------------

    def scan(self, target: TableTarget, on_progress: ProgressFn | None = None) -> TableResult:
        result = TableResult(target, started=datetime.now())
        progress = on_progress or (lambda done, total: None)
        try:
            self._check_cancel()
            client = self.client_for(target)
            table = client.get_table(target.ref)
            result.table_type = getattr(table, "table_type", "") or ""
            specs = flatten_schema(table.schema)
            result.columns = [ColumnResult(s) for s in specs]
            self.log(f"{target.ref}: {len(specs)} columns to check")
            if not specs:
                result.total_rows = getattr(table, "num_rows", None)
                result.status = "ok"
                return result

            done = 0
            progress(0, len(specs))
            for start in range(0, len(specs), self.batch_size):
                batch = list(range(start, min(start + self.batch_size, len(specs))))
                self._run_indices(client, target, result, batch)
                done += len(batch)
                progress(done, len(specs))
            result.status = "ok"
            if result.error_columns:
                self.log(f"{target.ref}: {len(result.error_columns)} column(s) could not be checked")
        except ScanCancelled:
            result.status = "cancelled"
            result.error = "Cancelled"
        except Exception as exc:  # noqa: BLE001 - reported to the user
            result.status = "error"
            result.error = friendly_error(exc)
            self.log(f"{target.ref}: ERROR {result.error}")
        finally:
            result.finished = datetime.now()
        return result

    def cancel(self) -> None:
        self.cancel_event.set()

    # -- internals ------------------------------------------------------------

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise ScanCancelled()

    def _run_indices(self, client, target: TableTarget, result: TableResult,
                     indices: list[int]) -> None:
        """Count non-nulls for the given column indices, splitting on bad SQL."""
        self._check_cancel()
        specs = [result.columns[i].spec for i in indices]
        sql = build_query(target, specs, indices)
        try:
            row = self._run_query(client, sql, result)
        except ScanCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            if not _is_query_error(exc):
                raise
            if len(indices) == 1:
                col = result.columns[indices[0]]
                col.error = friendly_error(exc)
                self.log(f"{target.ref}: could not check '{col.name}': {col.error}")
                return
            mid = len(indices) // 2
            self._run_indices(client, target, result, indices[:mid])
            self._run_indices(client, target, result, indices[mid:])
            return
        result.total_rows = int(row["total_rows"])
        for i in indices:
            result.columns[i].non_null = int(row[f"c{i}"])

    def _run_query(self, client, sql: str, result: TableResult):
        from google.cloud import bigquery

        cfg = bigquery.QueryJobConfig(labels=JOB_LABELS)
        if self.max_bytes_billed:
            cfg.maximum_bytes_billed = int(self.max_bytes_billed)
        job = client.query(sql, job_config=cfg)
        while not job.done():
            if self.cancel_event.is_set():
                try:
                    job.cancel()
                except Exception:  # noqa: BLE001 - best effort
                    pass
                raise ScanCancelled()
            time.sleep(POLL_SECONDS)
        rows = list(job.result())
        result.bytes_processed += int(getattr(job, "total_bytes_processed", 0) or 0)
        return rows[0]


def format_bytes(n: int | float | None) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} PB"


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"
