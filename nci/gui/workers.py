"""Background work so the window never freezes."""

from __future__ import annotations

import os
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from .. import APP_NAME, CREDITS, __version__
from ..core import ClientPool, Scanner, TableResult, TableTarget, format_bytes, format_duration, friendly_error
from ..identity import google_account, local_identity
from ..report import RunInfo, write_report
from ..tracking import build_rows, record_run


@dataclass
class RunSettings:
    use_key_file: bool = False
    key_file: str = ""
    billing_project: str = ""
    output_dir: str = ""
    max_gb_per_query: float = 0.0  # 0 = no limit
    parallel: int = 4
    open_when_done: bool = True
    save_pdf: bool = True
    run_log_table: str = ""  # from the admin config; empty = no run tracking

    @property
    def auth_method(self) -> str:
        return "Service account key" if self.use_key_file else "Google login"

    def key_for(self, target: TableTarget | None = None) -> str:
        if target is not None and target.key_file:
            return target.key_file
        return self.key_file if self.use_key_file else ""

    def client_for(self, pool: ClientPool):
        def get(target: TableTarget):
            key = self.key_for(target)
            if key and not os.path.isfile(key):
                raise FileNotFoundError(2, "Key file not found", key)
            return pool.get(self.billing_project or target.project, key)
        return get

    def client_for_project(self, pool: ClientPool):
        def get(project: str):
            return pool.get(self.billing_project or project, self.key_for())
        return get


class TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Run ``fn()`` on the thread pool; emits done(result) or failed(message)."""

    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = TaskSignals()

    @Slot()
    def run(self):
        try:
            result = self.fn()
        except Exception as exc:  # noqa: BLE001 - shown to the user
            self.signals.failed.emit(friendly_error(exc))
        else:
            self.signals.done.emit(result)


@dataclass
class RunOutcome:
    results: list[TableResult]
    report_path: str = ""
    log_path: str = ""
    pdf_path: str = ""
    cancelled: bool = False
    seconds: float = 0.0
    report_error: str = ""
    run_id: str = ""
    tracked: bool = False


class ScanWorker(QObject):
    log = Signal(str)
    table_started = Signal(int)
    table_progress = Signal(int, int, int)
    table_done = Signal(int, object)
    finished = Signal(object)

    def __init__(self, targets: list[TableTarget], settings: RunSettings, pool: ClientPool):
        super().__init__()
        self.targets = targets
        self.settings = settings
        self.pool = pool
        self.cancel_event = threading.Event()
        self._lines: list[str] = []
        self._lock = threading.Lock()

    def cancel(self):
        self.cancel_event.set()

    def _log(self, msg: str):
        line = f"[{datetime.now():%H:%M:%S}] {msg}"
        with self._lock:
            self._lines.append(line)
        self.log.emit(line)

    @Slot()
    def run(self):
        try:
            self._run()
        except Exception:  # noqa: BLE001 - never leave the UI hanging
            self._log("Unexpected error:\n" + traceback.format_exc())
            self.finished.emit(RunOutcome(results=[], report_error="Unexpected error, see the log."))

    def _scan_one(self, scanner: Scanner, i: int, target: TableTarget) -> TableResult:
        if self.cancel_event.is_set():
            res = TableResult(target, status="cancelled", error="Cancelled before it started")
        else:
            self.table_started.emit(i)
            self._log(f"Scanning {target.ref}")
            res = scanner.scan(target, lambda d, t: self.table_progress.emit(i, d, t))
            if res.status == "ok":
                rows = f"{res.total_rows:,}" if res.total_rows is not None else "?"
                lines = [f"Done {target.ref}: {rows} rows, {len(res.columns)} columns, "
                         f"{len(res.null_columns)} null, {format_bytes(res.bytes_processed)} scanned, "
                         f"{format_duration(res.seconds)}"]
                lines += [f"    - {c.name}" for c in res.null_columns]
                self._log("\n".join(lines))
        self.table_done.emit(i, res)
        return res

    def _run(self):
        s = self.settings
        run_id = str(uuid.uuid4())
        started = datetime.now()
        identity = local_identity(s.auth_method)
        limit = int(s.max_gb_per_query * 1024 ** 3) if s.max_gb_per_query else None
        client_for = s.client_for(self.pool)
        scanner = Scanner(client_for, max_bytes_billed=limit, log=self._log, cancel_event=self.cancel_event)
        workers = max(1, min(s.parallel, len(self.targets)))

        self._log(f"{APP_NAME} {__version__}  ·  {CREDITS}")
        self._log(f"Run ID: {run_id}")
        self._log(f"Executed by: {identity.display} ({identity.os}) · sign-in: {identity.auth_method}")
        self._log(f"Scanning {len(self.targets)} table(s), {workers} at a time"
                  + (f", safety limit {format_bytes(limit)} per query" if limit else ""))

        with ThreadPoolExecutor(max_workers=workers + 1) as ex:
            # Ask BigQuery which account we are, alongside the scans (free query).
            who = ex.submit(self._google_account, client_for, self.targets[0]) if self.targets else None
            futures = [ex.submit(self._scan_one, scanner, i, t) for i, t in enumerate(self.targets)]
            results = [f.result() for f in futures]
            identity.google_account = who.result() if who else ""

        finished = datetime.now()
        cancelled = self.cancel_event.is_set()
        outcome = RunOutcome(results, cancelled=cancelled, seconds=(finished - started).total_seconds(),
                             run_id=run_id)
        ok = [r for r in results if r.status == "ok"]
        self._log(f"{'Cancelled' if cancelled else 'Finished'}: {len(ok)} of {len(results)} table(s) "
                  f"scanned, {sum(len(r.null_columns) for r in ok)} null column(s) found, "
                  f"{format_duration(outcome.seconds)} total")

        info = RunInfo(executed_by=identity.display, started=started, finished=finished, run_id=run_id,
                       identity=identity, query_project=s.billing_project, parallel=workers,
                       safety_limit_gb=s.max_gb_per_query)
        if cancelled:
            info.notes.append("The run was cancelled; this report is partial.")
        base = os.path.join(s.output_dir, f"NullColumns_{started:%Y-%m-%d_%H-%M-%S}")
        if any(r.status != "cancelled" for r in results):
            try:
                os.makedirs(s.output_dir, exist_ok=True)
                outcome.report_path = write_report(base + ".xlsx", results, info)
                self._log(f"Report saved: {outcome.report_path}")
            except Exception as exc:  # noqa: BLE001
                outcome.report_error = f"Could not save the report: {exc}"
                self._log(outcome.report_error)

        outcome.tracked = self._track(outcome, info)

        if outcome.report_path:
            if s.save_pdf:
                try:
                    from .pdflog import write_pdf_log

                    outcome.pdf_path = base + "_log.pdf"
                    self._log(f"PDF log: {outcome.pdf_path}")
                    write_pdf_log(outcome.pdf_path, info, results, self._lines, outcome.report_path)
                except Exception as exc:  # noqa: BLE001 - the report itself is fine
                    outcome.pdf_path = ""
                    self._log(f"PDF log skipped: {exc}")
            try:
                outcome.log_path = base + ".log"
                with open(outcome.log_path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(self._lines) + "\n")
            except OSError as exc:
                outcome.log_path = ""
                self._log(f"Log file skipped: {exc}")
        self.finished.emit(outcome)

    def _google_account(self, client_for, target: TableTarget) -> str:
        try:
            account = google_account(client_for(target))
        except Exception as exc:  # noqa: BLE001 - only informational
            self._log(f"Google account: not available ({friendly_error(exc)})")
            return ""
        self._log(f"Google account: {account}")
        return account

    def _track(self, outcome: RunOutcome, info: RunInfo) -> bool:
        """Record the run in BigQuery for analytics; never fails the run."""
        table = self.settings.run_log_table
        if table.count(".") != 2:
            return False
        try:
            rows = build_rows(run_id=outcome.run_id, results=outcome.results, identity=info.identity,
                              started=info.started)
            if not rows:
                return False
            client = self.pool.get(table.split(".")[0], self.settings.key_for())
            record_run(client, table, rows)
        except Exception as exc:  # noqa: BLE001
            self._log(f"Run tracking skipped: {friendly_error(exc)}")
            return False
        self._log(f"Run recorded for usage tracking ({len(rows)} table row(s))")
        return True
