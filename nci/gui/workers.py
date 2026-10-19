"""Background work so the window never freezes."""

from __future__ import annotations

import getpass
import os
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..core import ClientPool, Scanner, TableResult, TableTarget, format_bytes, format_duration, friendly_error
from ..report import RunInfo, write_report


@dataclass
class RunSettings:
    use_key_file: bool = False
    key_file: str = ""
    billing_project: str = ""
    output_dir: str = ""
    max_gb_per_query: float = 0.0  # 0 = no limit
    parallel: int = 4
    open_when_done: bool = True

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
    cancelled: bool = False
    seconds: float = 0.0
    report_error: str = ""


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
        started = datetime.now()
        user = current_user()
        limit = int(s.max_gb_per_query * 1024 ** 3) if s.max_gb_per_query else None
        scanner = Scanner(s.client_for(self.pool), max_bytes_billed=limit, log=self._log,
                          cancel_event=self.cancel_event)
        workers = max(1, min(s.parallel, len(self.targets)))
        self._log(f"Scan started by {user}: {len(self.targets)} table(s), {workers} at a time")
        if limit:
            self._log(f"Safety limit: {format_bytes(limit)} per query")

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(self._scan_one, scanner, i, t) for i, t in enumerate(self.targets)]
            results = [f.result() for f in futures]

        finished = datetime.now()
        cancelled = self.cancel_event.is_set()
        outcome = RunOutcome(results, cancelled=cancelled, seconds=(finished - started).total_seconds())
        ok = [r for r in results if r.status == "ok"]
        self._log(f"{'Cancelled' if cancelled else 'Finished'}: {len(ok)} of {len(results)} table(s) "
                  f"scanned, {sum(len(r.null_columns) for r in ok)} null column(s) found, "
                  f"{format_duration(outcome.seconds)} total")

        if any(r.status != "cancelled" for r in results):
            try:
                os.makedirs(s.output_dir, exist_ok=True)
                base = os.path.join(s.output_dir, f"NullColumns_{started:%Y-%m-%d_%H-%M-%S}")
                info = RunInfo(executed_by=user, started=started, finished=finished)
                if cancelled:
                    info.notes.append("The run was cancelled; this report is partial.")
                outcome.report_path = write_report(base + ".xlsx", results, info)
                self._log(f"Report saved: {outcome.report_path}")
                outcome.log_path = base + ".log"
                with open(outcome.log_path, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(self._lines) + "\n")
            except Exception as exc:  # noqa: BLE001
                outcome.report_error = f"Could not save the report: {exc}"
                self._log(outcome.report_error)
        self.finished.emit(outcome)


def current_user() -> str:
    """Windows full name when available (domain), otherwise the login name."""
    name = getpass.getuser()
    if sys.platform == "win32":
        try:
            import win32api
            import win32net

            info = win32net.NetUserGetInfo(win32net.NetGetAnyDCName(), win32api.GetUserName(), 2)
            if info.get("full_name"):
                return info["full_name"]
        except Exception:  # noqa: BLE001 - off VPN / pywin32 missing
            pass
    return name
