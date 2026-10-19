"""Main window: pick tables, choose a connection, run, read results."""

from __future__ import annotations

import json
import os
import time

from PySide6.QtCore import QSettings, QSortFilterProxyModel, QStandardPaths, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFontDatabase, QGuiApplication,
                           QKeySequence, QShortcut)
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSizePolicy, QSplitter,
                               QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from .. import APP_NAME, __version__
from ..catalog import Catalog
from ..core import ClientPool, TableResult, TableTarget, format_duration, make_client
from ..inputs import load_targets, save_targets
from .browse import BrowseDialog
from .dialogs import AddTablesDialog
from .models import TargetModel, TargetView
from .style import DANGER, MUTED, SUCCESS, WARNING, app_icon
from .workers import RunOutcome, RunSettings, ScanWorker, Task

LIST_FILTER = "Table lists (*.xlsx *.xlsm *.xls *.csv *.txt);;All files (*)"
HELP_TEXT = f"""<h3>{APP_NAME} {__version__}</h3>
<p>Finds columns in BigQuery tables that have <b>no value in any row</b>.</p>
<ol>
<li><b>Add tables</b>: browse a project, paste names (<i>project.dataset.table</i>), or import an
Excel/CSV list. The original <i>Table_name_credentials.xlsx</i> works as is.</li>
<li><b>Choose how to connect</b>: your Google login (run
<code>gcloud auth application-default login</code> once), or a service-account key file. A key file
set on a table row overrides this.</li>
<li>Click <b>Run scan</b>. Each table is read once. A report opens when it finishes.</li>
</ol>
<p><b>How "null" is decided:</b> a column is null when not a single row has a value. Arrays count as
empty when they have no elements. A field inside a repeated record is null when no element in any
row has a value.</p>
<p><b>Cost:</b> a scan reads every column once, like <code>SELECT *</code>. Set a safety limit to
skip tables that are too big.</p>
<p><b>Browse:</b> search covers every dataset of the project (listings are cached; ↻ refreshes).
Use several words or <code>*</code> wildcards, e.g. <code>sales *_2024</code>. Ticked tables from
several projects collect on the right.</p>
<p>Shortcuts: <b>Ctrl+V</b> pastes table names, <b>Delete</b> removes rows, <b>F5</b> runs.</p>"""


def _card(title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(16, 14, 16, 14)
    layout.setSpacing(10)
    if title:
        label = QLabel(title)
        label.setObjectName("sectionTitle")
        layout.addWidget(label)
    return frame, layout


def _hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


class MainWindow(QMainWindow):
    def __init__(self, client_factory=make_client, settings: QSettings | None = None):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self.resize(1280, 820)
        self.setMinimumSize(980, 640)

        self.pool = ClientPool(client_factory)
        self._catalog: Catalog | None = None
        self._catalog_identity = ""
        self.settings = settings or QSettings("NullColumnIdentifier", "NullColumnIdentifier")
        self.model = TargetModel(self)
        self.thread: QThread | None = None
        self.worker: ScanWorker | None = None
        self._tasks: set[Task] = set()
        self._last_outcome: RunOutcome | None = None
        self._run_started = 0.0
        self._tick = QTimer(self)
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._update_elapsed)

        self._build_ui()
        self._load_settings()
        self._update_counts()
        self._set_busy(False)

    # ======================================================================= UI

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 14, 18, 14)
        outer.setSpacing(12)
        self.setCentralWidget(root)

        outer.addLayout(self._build_header())

        splitter = QSplitter(Qt.Vertical)
        splitter.setChildrenCollapsible(False)
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(12)
        top_layout.addWidget(self._build_tables_card(), 1)
        top_layout.addWidget(self._build_settings_card())
        splitter.addWidget(top)
        splitter.addWidget(self._build_bottom_tabs())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([500, 260])
        outer.addWidget(splitter, 1)

        outer.addWidget(self._build_footer())

        QShortcut(QKeySequence.Paste, self.view, activated=self.paste_tables, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence.Delete, self.view, activated=self.remove_selected, context=Qt.WidgetShortcut)
        QShortcut(QKeySequence("F5"), self, activated=self.run_scan)

    def _build_header(self):
        row = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(44, 44))
        row.addWidget(icon)
        texts = QVBoxLayout()
        texts.setSpacing(0)
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel("Find the BigQuery columns that have no data in any row.")
        subtitle.setObjectName("subtitle")
        texts.addWidget(title)
        texts.addWidget(subtitle)
        row.addLayout(texts, 1)
        version = QLabel(f"v{__version__}")
        version.setObjectName("hint")
        help_btn = QPushButton("How it works")
        help_btn.setObjectName("link")
        help_btn.clicked.connect(lambda: QMessageBox.information(self, "How it works", HELP_TEXT))
        row.addWidget(help_btn, 0, Qt.AlignVCenter)
        row.addWidget(version, 0, Qt.AlignVCenter)
        return row

    def _build_tables_card(self):
        card, layout = _card()
        head = QHBoxLayout()
        title = QLabel("1  Tables to scan")
        title.setObjectName("sectionTitle")
        self.count_label = _hint("")
        self.list_filter = QLineEdit()
        self.list_filter.setPlaceholderText("Filter this list…")
        self.list_filter.setClearButtonEnabled(True)
        self.list_filter.setFixedWidth(220)
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.count_label)
        head.addSpacing(8)
        head.addWidget(self.list_filter)
        layout.addLayout(head)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.browse_btn = QPushButton("Browse BigQuery…")
        self.browse_btn.setToolTip("Pick datasets and tables from a project")
        self.add_btn = QPushButton("Add tables…")
        self.add_btn.setToolTip("Type or paste table names")
        self.import_btn = QPushButton("Import list…")
        self.import_btn.setToolTip("Load an Excel or CSV list (the old Table_name_credentials.xlsx works)")
        self.save_list_btn = QPushButton("Save list…")
        self.remove_btn = QPushButton("Remove")
        self.remove_btn.setObjectName("danger")
        self.clear_btn = QPushButton("Clear all")
        for b in (self.browse_btn, self.add_btn, self.import_btn, self.save_list_btn):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.remove_btn)
        bar.addWidget(self.clear_btn)
        layout.addLayout(bar)

        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.view = TargetView()
        self.view.setModel(self.proxy)
        layout.addWidget(self.view, 1)
        layout.addWidget(_hint("Double-click a cell to edit it. Right-click rows to set a key file for them."))

        self.browse_btn.clicked.connect(self.browse_bigquery)
        self.add_btn.clicked.connect(self.add_tables)
        self.import_btn.clicked.connect(lambda: self.import_lists())
        self.save_list_btn.clicked.connect(self.save_list)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn.clicked.connect(self.clear_tables)
        self.list_filter.textChanged.connect(self._filter_list)
        self.view.filesDropped.connect(self.import_lists)
        self.view.customContextMenuRequested.connect(self._table_menu)
        self.view.selectionModel().selectionChanged.connect(lambda *_: self._update_counts())
        self.model.changed.connect(self._update_counts)
        self.model.changed.connect(self._save_tables)
        return card

    def _build_settings_card(self):
        card, layout = _card("2  Connection & output")
        card.setFixedWidth(380)

        layout.addWidget(QLabel("Sign in with"))
        self.auth_group = QButtonGroup(self)
        self.adc_radio = QRadioButton("My Google account")
        self.key_radio = QRadioButton("Service account key file (.json)")
        self.auth_group.addButton(self.adc_radio)
        self.auth_group.addButton(self.key_radio)
        layout.addWidget(self.adc_radio)
        self.adc_hint = _hint("First time on this computer? Run once:\n"
                              "gcloud auth application-default login")
        self.adc_hint.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.adc_hint.setContentsMargins(24, 0, 0, 0)
        layout.addWidget(self.adc_hint)
        layout.addWidget(self.key_radio)
        key_row = QHBoxLayout()
        key_row.setContentsMargins(24, 0, 0, 0)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("Path to the key .json file")
        self.key_browse = QPushButton("Browse…")
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(self.key_browse)
        layout.addLayout(key_row)

        layout.addWidget(QLabel("Run queries in project (optional)"))
        self.billing_edit = QLineEdit()
        self.billing_edit.setPlaceholderText("Leave empty to use each table's own project")
        self.billing_edit.setToolTip("Queries run in, and are billed to, this project. Useful when you can "
                                     "read a table but cannot run jobs in its project.")
        layout.addWidget(self.billing_edit)

        layout.addWidget(QLabel("Save reports to"))
        out_row = QHBoxLayout()
        self.out_edit = QLineEdit()
        self.out_browse = QPushButton("Browse…")
        self.out_open = QPushButton("Open")
        out_row.addWidget(self.out_edit, 1)
        out_row.addWidget(self.out_browse)
        out_row.addWidget(self.out_open)
        layout.addLayout(out_row)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.addWidget(QLabel("Safety limit per query"), 0, 0)
        self.limit_spin = LimitCombo()
        self.limit_spin.setToolTip("Tables that would scan more than this are skipped, so nothing is billed "
                                   "for them.")
        grid.addWidget(self.limit_spin, 0, 1)
        grid.addWidget(QLabel("Tables at the same time"), 1, 0)
        self.parallel_combo = QComboBox()
        for n in (1, 2, 4, 6, 8):
            self.parallel_combo.addItem(str(n), n)
        self.parallel_combo.setToolTip("How many tables are scanned in parallel. More is faster; "
                                       "BigQuery runs each table as its own query.")
        grid.addWidget(self.parallel_combo, 1, 1)
        grid.setColumnStretch(1, 1)
        layout.addLayout(grid)
        self.open_check = QCheckBox("Open the report when finished")
        layout.addWidget(self.open_check)
        layout.addStretch(1)

        self.key_browse.clicked.connect(self._pick_key_file)
        self.out_browse.clicked.connect(self._pick_output_dir)
        self.out_open.clicked.connect(lambda: self._open_path(self._output_dir()))
        self.auth_group.buttonToggled.connect(lambda *_: self._sync_auth_widgets())
        return card

    def _build_bottom_tabs(self):
        self.tabs = QTabWidget()

        results = QWidget()
        rl = QVBoxLayout(results)
        rl.setContentsMargins(10, 10, 10, 10)
        bar = QHBoxLayout()
        self.result_filter = QLineEdit()
        self.result_filter.setPlaceholderText("Filter by table or column name…")
        self.result_filter.setClearButtonEnabled(True)
        self.only_nulls = QCheckBox("Only tables with null columns")
        self.copy_btn = QPushButton("Copy null columns")
        self.copy_btn.setToolTip("Copy the null column names of the selected tables (or all tables)")
        self.open_report_btn = QPushButton("Open report")
        self.open_report_btn.setEnabled(False)
        bar.addWidget(self.result_filter, 1)
        bar.addWidget(self.only_nulls)
        bar.addWidget(self.copy_btn)
        bar.addWidget(self.open_report_btn)
        rl.addLayout(bar)
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["Table / column", "Type", "Rows", "Null columns", "Coverage", "Status"])
        self.results_tree.setAlternatingRowColors(True)
        self.results_tree.setUniformRowHeights(True)
        self.results_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        h = self.results_tree.header()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        for col, w in ((1, 110), (2, 110), (3, 110), (4, 90), (5, 260)):
            h.setSectionResizeMode(col, QHeaderView.Interactive)
            self.results_tree.setColumnWidth(col, w)
        h.setStretchLastSection(False)
        rl.addWidget(self.results_tree, 1)
        self.tabs.addTab(results, "Results")

        log = QWidget()
        ll = QVBoxLayout(log)
        ll.setContentsMargins(10, 10, 10, 10)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setFont(QFontDatabase.systemFont(QFontDatabase.FixedFont))
        self.log_view.setMaximumBlockCount(50_000)
        ll.addWidget(self.log_view, 1)
        lb = QHBoxLayout()
        lb.addStretch(1)
        copy_log = QPushButton("Copy log")
        clear_log = QPushButton("Clear")
        lb.addWidget(copy_log)
        lb.addWidget(clear_log)
        ll.addLayout(lb)
        self.tabs.addTab(log, "Log")

        self.result_filter.textChanged.connect(self._filter_results)
        self.only_nulls.toggled.connect(lambda *_: self._filter_results())
        self.copy_btn.clicked.connect(self._copy_null_columns)
        self.open_report_btn.clicked.connect(self._open_report)
        copy_log.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.log_view.toPlainText()))
        clear_log.clicked.connect(self.log_view.clear)
        return self.tabs

    def _build_footer(self):
        card, layout = _card()
        layout.setContentsMargins(16, 10, 16, 10)
        row = QHBoxLayout()
        info = QVBoxLayout()
        info.setSpacing(4)
        self.status_label = QLabel("Ready")
        self.status_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        info.addWidget(self.status_label)
        info.addWidget(self.progress)
        row.addLayout(info, 1)
        row.addSpacing(16)
        self.elapsed_label = _hint("")
        row.addWidget(self.elapsed_label)
        self.report_btn = QPushButton("Open report")
        self.report_btn.setObjectName("success")
        self.folder_btn = QPushButton("Open folder")
        self.report_btn.hide()
        self.folder_btn.hide()
        row.addWidget(self.report_btn)
        row.addWidget(self.folder_btn)
        row.addSpacing(12)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("danger")
        self.run_btn = QPushButton("Run scan")
        self.run_btn.setObjectName("primary")
        self.run_btn.setToolTip("Scan the selected tables, or all tables if none are selected (F5)")
        row.addWidget(self.cancel_btn)
        row.addWidget(self.run_btn)
        layout.addLayout(row)

        self.report_btn.clicked.connect(self._open_report)
        self.folder_btn.clicked.connect(self._open_report_folder)
        self.cancel_btn.clicked.connect(self.cancel_scan)
        self.run_btn.clicked.connect(self.run_scan)
        return card

    # ================================================================ settings

    def _load_settings(self):
        s = self.settings
        use_key = s.value("use_key_file", False, type=bool)
        (self.key_radio if use_key else self.adc_radio).setChecked(True)
        self.key_edit.setText(s.value("key_file", "", type=str))
        self.billing_edit.setText(s.value("billing_project", "", type=str))
        default_out = os.path.join(os.path.expanduser("~"), "Documents", "Null Column Reports")
        self.out_edit.setText(s.value("output_dir", default_out, type=str) or default_out)
        self.limit_spin.setValue(s.value("max_gb", 0.0, type=float))
        idx = self.parallel_combo.findData(s.value("parallel", 4, type=int))
        self.parallel_combo.setCurrentIndex(idx if idx >= 0 else 2)
        self.open_check.setChecked(s.value("open_when_done", True, type=bool))
        try:
            rows = json.loads(s.value("tables", "[]", type=str) or "[]")
            self.model.add(TableTarget(**r) for r in rows)
        except (ValueError, TypeError):
            pass
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self._sync_auth_widgets()

    def _save_settings(self):
        s = self.settings
        s.setValue("use_key_file", self.key_radio.isChecked())
        s.setValue("key_file", self.key_edit.text().strip())
        s.setValue("billing_project", self.billing_edit.text().strip())
        s.setValue("output_dir", self.out_edit.text().strip())
        s.setValue("max_gb", self.limit_spin.value())
        s.setValue("parallel", self.parallel_combo.currentData())
        s.setValue("open_when_done", self.open_check.isChecked())
        s.setValue("geometry", self.saveGeometry())
        self._save_tables()
        s.sync()

    def _save_tables(self):
        rows = [{"project": t.project, "dataset": t.dataset, "table": t.table, "key_file": t.key_file}
                for t in self.model.targets()]
        self.settings.setValue("tables", json.dumps(rows))

    def _run_settings(self) -> RunSettings:
        return RunSettings(
            use_key_file=self.key_radio.isChecked(),
            key_file=self.key_edit.text().strip(),
            billing_project=self.billing_edit.text().strip(),
            output_dir=self._output_dir(),
            max_gb_per_query=self.limit_spin.value(),
            parallel=int(self.parallel_combo.currentData()),
            open_when_done=self.open_check.isChecked(),
        )

    def _output_dir(self) -> str:
        return os.path.expanduser(self.out_edit.text().strip()) or os.path.expanduser("~")

    def _sync_auth_widgets(self):
        use_key = self.key_radio.isChecked()
        self.key_edit.setEnabled(use_key and not self._busy())
        self.key_browse.setEnabled(use_key and not self._busy())
        self.adc_hint.setVisible(not use_key)

    # ================================================================ tables

    def browse_bigquery(self):
        settings = self._run_settings()
        if not self._check_credentials(settings, need_rows=False):
            return
        identity = settings.key_for() or "google-login"
        if self._catalog is None or self._catalog_identity != identity:
            cache = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
            self._catalog = Catalog(settings.client_for_project(self.pool),
                                    os.path.join(cache, "catalog_cache"), identity)
            self._catalog_identity = identity

        targets = self.model.targets()
        recent = json.loads(self.settings.value("recent_projects", "[]", type=str) or "[]")
        recent = list(dict.fromkeys(recent + [t.project for t in reversed(targets)]))[:8]
        start = recent[0] if recent else ""
        dialog = BrowseDialog(self._catalog, self, recent_projects=recent, existing=targets,
                              start_project=start)
        dialog.exec()
        self.settings.setValue("recent_projects", json.dumps(dialog.recent[:8]))
        if dialog.result() and dialog.targets:
            self._add_targets(dialog.targets)

    def add_tables(self, text: str = ""):
        last = self.model.targets()[-1] if self.model.rowCount() else None
        dialog = AddTablesDialog(self, project=last.project if last else "",
                                 dataset=last.dataset if last else "")
        if text:
            dialog.text.setPlainText(text)
        if dialog.exec():
            self._add_targets(dialog.targets)

    def paste_tables(self):
        text = QGuiApplication.clipboard().text()
        if text.strip():
            self.add_tables(text)

    def import_lists(self, paths=None):
        if not paths:
            start = self.settings.value("last_import_dir", os.path.expanduser("~"), type=str)
            paths, _ = QFileDialog.getOpenFileNames(self, "Import table list", start, LIST_FILTER)
            if not paths:
                return
        targets, warnings = [], []
        for path in paths:
            self.settings.setValue("last_import_dir", os.path.dirname(path))
            try:
                found, warn = load_targets(path)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{os.path.basename(path)}: could not read the file ({exc})")
                continue
            targets.extend(found)
            warnings.extend(f"{os.path.basename(path)}: {w}" for w in warn)
        added = self._add_targets(targets)
        if warnings:
            QMessageBox.warning(self, "Import", f"Added {added} table(s).\n\n" + "\n".join(warnings[:20]))

    def save_list(self):
        if not self.model.rowCount():
            return
        start = os.path.join(self.settings.value("last_import_dir", os.path.expanduser("~"), type=str),
                             "tables.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Save table list", start,
                                              "Excel (*.xlsx);;CSV (*.csv)")
        if path:
            save_targets(path, self.model.targets())
            self._set_status(f"Saved {self.model.rowCount()} table(s) to {path}")

    def _add_targets(self, targets) -> int:
        targets = list(targets)
        added = self.model.add(targets)
        skipped = len(list(targets)) - added
        msg = f"Added {added} table(s)"
        if skipped:
            msg += f" ({skipped} already in the list)"
        self._set_status(msg)
        return added

    def remove_selected(self):
        if self._busy():
            return
        rows = self._selected_rows()
        if rows:
            self.model.remove(rows)

    def clear_tables(self):
        if self._busy() or not self.model.rowCount():
            return
        if QMessageBox.question(self, "Clear all", f"Remove all {self.model.rowCount()} table(s) from the list?") \
                == QMessageBox.Yes:
            self.model.clear()

    def _selected_rows(self) -> list[int]:
        rows = self.view.selectionModel().selectedRows()
        return sorted({self.proxy.mapToSource(i).row() for i in rows})

    def _visible_rows(self) -> list[int]:
        return sorted(self.proxy.mapToSource(self.proxy.index(r, 0)).row()
                      for r in range(self.proxy.rowCount()))

    def _filter_list(self, text: str):
        self.proxy.setFilterFixedString(text.strip())
        self._update_counts()

    def _table_menu(self, pos):
        if self._busy():
            return
        rows = self._selected_rows()
        menu = QMenu(self)
        set_key = menu.addAction("Set key file for selected…")
        clear_key = menu.addAction("Use default connection for selected")
        menu.addSeparator()
        paste = menu.addAction("Paste table names")
        remove = menu.addAction("Remove selected")
        for a in (set_key, clear_key, remove):
            a.setEnabled(bool(rows))
        chosen = menu.exec(self.view.viewport().mapToGlobal(pos))
        if chosen == set_key:
            path, _ = QFileDialog.getOpenFileName(self, "Service account key", self.key_edit.text(),
                                                  "JSON key (*.json);;All files (*)")
            if path:
                self.model.set_key_file(rows, path)
        elif chosen == clear_key:
            self.model.set_key_file(rows, "")
        elif chosen == paste:
            self.paste_tables()
        elif chosen == remove:
            self.model.remove(rows)

    def _update_counts(self):
        n = self.model.rowCount()
        sel = len(self._selected_rows())
        shown = self.proxy.rowCount()
        text = f"{n:,} table(s)" if n else ""
        if shown != n:
            text += f" · {shown:,} shown"
        if sel and sel != n:
            text += f" · {sel:,} selected"
        self.count_label.setText(text)
        to_run = sel or shown
        self.run_btn.setText(f"Run scan ({to_run:,})" if to_run != n else "Run scan")
        if not self._busy():
            for b in (self.clear_btn, self.save_list_btn):
                b.setEnabled(n > 0)
            self.run_btn.setEnabled(to_run > 0)
            self.remove_btn.setEnabled(sel > 0)

    # ================================================================ run

    def _rows_to_run(self) -> list[int]:
        return self._selected_rows() or self._visible_rows()

    def _check_credentials(self, settings: RunSettings, need_rows=True) -> bool:
        if need_rows and not self.model.rowCount():
            QMessageBox.information(self, APP_NAME, "Add at least one table first.")
            return False
        if settings.use_key_file:
            if not settings.key_file:
                QMessageBox.warning(self, APP_NAME, "Choose a service account key file, or switch to "
                                                    "“My Google account”.")
                return False
            if not os.path.isfile(settings.key_file):
                QMessageBox.warning(self, APP_NAME, f"Key file not found:\n{settings.key_file}")
                return False
        return True

    def run_scan(self):
        if self._busy() or not self.model.rowCount():
            return
        settings = self._run_settings()
        if not self._check_credentials(settings):
            return
        try:
            os.makedirs(settings.output_dir, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, APP_NAME, f"Cannot use the output folder:\n{exc}")
            return
        self._save_settings()

        rows = self._rows_to_run()
        self._run_rows = rows
        targets = [self.model.rows[r].target for r in rows]
        for r in range(self.model.rowCount()):
            if r in rows:
                self.model.set_state(r, "queued", "Queued")
                self.model.rows[r].result = None
        self.results_tree.clear()
        self._show_summary("", "", False)
        self.log_view.clear()
        self.progress.setValue(0)
        self._last_outcome = None
        self.open_report_btn.setEnabled(False)

        self.thread = QThread(self)
        self.worker = ScanWorker(targets, settings, self.pool)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self._append_log)
        self.worker.table_started.connect(self._on_table_started)
        self.worker.table_progress.connect(self._on_table_progress)
        self.worker.table_done.connect(self._on_table_done)
        self.worker.finished.connect(self._on_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._thread_cleanup)

        self._run_started = time.monotonic()
        self._fractions = [0.0] * len(rows)
        self._done_tables = 0
        self._set_busy(True)
        self._set_status(f"Starting scan of {len(targets)} table(s)…")
        self._tick.start()
        self._update_elapsed()
        self.thread.start()

    def cancel_scan(self):
        if self.worker:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self._set_status("Cancelling… finishing the current query")

    def _thread_cleanup(self):
        if self.thread:
            self.thread.deleteLater()
        self.thread = None
        self.worker = None

    def _on_table_started(self, i):
        row = self._run_rows[i]
        self.model.set_state(row, "running", "Scanning…")
        self.view.scrollTo(self.proxy.mapFromSource(self.model.index(row, 0)))
        self._update_run_status()

    def _on_table_progress(self, i, done, total):
        row = self._run_rows[i]
        if total:
            self.model.set_state(row, "running", f"Scanning… {done * 100 // total}%")
            # Leave room for the final step; a table counts fully once it is done.
            self._fractions[i] = 0.95 * done / total
            self._update_progress()

    def _update_progress(self):
        self.progress.setValue(int(sum(self._fractions) / max(1, len(self._fractions)) * 1000))

    def _update_run_status(self):
        running = sum(1 for r in self._run_rows if self.model.rows[r].state == "running")
        total = len(self._run_rows)
        self._set_status(f"Scanning… {self._done_tables:,} of {total:,} done"
                         + (f" · {running} running" if running else ""))

    def _on_table_done(self, i, res: TableResult):
        row = self._run_rows[i]
        if res.status == "ok":
            n = len(res.null_columns)
            state = "nulls" if n else "ok"
            text = f"{n} null column{'s' if n != 1 else ''}" if n else "No null columns"
            if res.error_columns:
                text += f" · {len(res.error_columns)} unchecked"
        elif res.status == "cancelled":
            state, text = "cancelled", "Cancelled"
        else:
            state, text = "error", "Failed – hover for details"
        self.model.set_state(row, state, text, res)
        self._fractions[i] = 1.0
        self._done_tables += 1
        self._update_progress()
        if self._busy() and not self.worker.cancel_event.is_set():
            self._update_run_status()
        self._add_result_item(res)

    def _on_finished(self, outcome: RunOutcome):
        self._tick.stop()
        self._last_outcome = outcome
        self._set_busy(False)
        self._update_counts()
        results = outcome.results
        ok = [r for r in results if r.status == "ok"]
        failed = [r for r in results if r.status == "error"]
        nulls = sum(len(r.null_columns) for r in ok)
        self.progress.setValue(1000 if not outcome.cancelled else self.progress.value())

        parts = [f"Scanned {len(ok)} of {len(results)} table(s)", f"found {nulls} null column(s)"]
        if failed:
            parts.append(f"{len(failed)} failed")
        summary = ", ".join(parts) + f" in {format_duration(outcome.seconds)}."
        if outcome.cancelled:
            summary = "Scan cancelled. " + summary
        kind = "error" if (failed and not ok) or outcome.report_error else ("warn" if failed or outcome.cancelled else "")
        if outcome.report_error:
            summary += f"  {outcome.report_error}"
        self._show_summary(summary, kind, bool(outcome.report_path))
        self.open_report_btn.setEnabled(bool(outcome.report_path))
        self.tabs.setCurrentIndex(0)
        if outcome.report_path and self._run_settings().open_when_done and not outcome.cancelled:
            self._open_path(outcome.report_path)
        QApplication.alert(self, 0)  # flash the taskbar when a long scan ends

    # ================================================================ results

    def _add_result_item(self, res: TableResult):
        item = QTreeWidgetItem([res.target.ref, res.table_type.title() if res.table_type else "",
                                f"{res.total_rows:,}" if res.total_rows is not None else "",
                                f"{len(res.null_columns)} of {len(res.columns)}" if res.status == "ok" else "",
                                f"{res.coverage:.1%}" if res.coverage is not None else "", ""])
        item.setData(0, Qt.UserRole, res)
        for c in (2, 3, 4):
            item.setTextAlignment(c, Qt.AlignRight | Qt.AlignVCenter)
        if res.status == "ok":
            if res.null_columns:
                status, color = f"{len(res.null_columns)} null column(s)", WARNING
            else:
                status, color = "All columns have data", SUCCESS
            if res.total_rows == 0:
                status, color = "Table is empty (0 rows)", WARNING
            if res.error_columns:
                status += f", {len(res.error_columns)} not checked"
        elif res.status == "cancelled":
            status, color = "Cancelled", MUTED
        else:
            status, color = res.error, DANGER
        item.setText(5, status)
        item.setToolTip(5, status)
        item.setForeground(5, _brush(color))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        for col in res.null_columns:
            child = QTreeWidgetItem([col.name, col.spec.data_type + (" (array)" if col.spec.mode == "REPEATED" else ""),
                                     "", "", "", "No value in any row"])
            child.setForeground(5, _brush(WARNING))
            item.addChild(child)
        for col in res.error_columns:
            child = QTreeWidgetItem([col.name, col.spec.data_type, "", "", "", f"Not checked: {col.error}"])
            child.setForeground(5, _brush(DANGER))
            child.setToolTip(5, col.error)
            item.addChild(child)
        self.results_tree.addTopLevelItem(item)
        if res.null_columns and len(res.null_columns) <= 25 and self.results_tree.topLevelItemCount() <= 50:
            item.setExpanded(True)
        self._filter_item(item)

    def _filter_results(self, *_):
        self.results_tree.setUpdatesEnabled(False)
        for i in range(self.results_tree.topLevelItemCount()):
            self._filter_item(self.results_tree.topLevelItem(i))
        self.results_tree.setUpdatesEnabled(True)

    def _filter_item(self, item: QTreeWidgetItem):
        text = self.result_filter.text().strip().lower()
        res: TableResult = item.data(0, Qt.UserRole)
        table_match = text in item.text(0).lower()
        child_match = False
        for j in range(item.childCount()):
            child = item.child(j)
            show = not text or table_match or text in child.text(0).lower()
            child.setHidden(not show)
            child_match = child_match or show
        hidden = (self.only_nulls.isChecked() and not res.null_columns) or \
            (bool(text) and not table_match and not child_match)
        item.setHidden(hidden)

    def _copy_null_columns(self):
        items = [i if i.parent() is None else i.parent() for i in self.results_tree.selectedItems()]
        if not items:
            items = [self.results_tree.topLevelItem(i) for i in range(self.results_tree.topLevelItemCount())]
        lines = []
        for item in dict.fromkeys(items):
            res: TableResult = item.data(0, Qt.UserRole)
            lines += [f"{res.target.ref}\t{c.name}" for c in res.null_columns]
        QGuiApplication.clipboard().setText("\n".join(lines))
        self._set_status(f"Copied {len(lines)} null column name(s) to the clipboard")

    def _append_log(self, line):
        self.log_view.appendPlainText(line)

    # ================================================================ helpers

    def _busy(self) -> bool:
        return getattr(self, "_is_busy", False)

    def _set_busy(self, busy: bool):
        self._is_busy = busy
        self.model.locked = busy
        for w in (self.browse_btn, self.add_btn, self.import_btn, self.save_list_btn, self.remove_btn,
                  self.clear_btn, self.run_btn, self.adc_radio, self.key_radio,
                  self.billing_edit, self.out_edit, self.out_browse, self.limit_spin, self.parallel_combo,
                  self.open_check):
            w.setEnabled(not busy)
        self.cancel_btn.setEnabled(busy)
        self.cancel_btn.setVisible(busy)
        self.view.setAcceptDrops(not busy)
        self._sync_auth_widgets()
        if not busy:
            self._update_counts()

    def _update_elapsed(self):
        self.elapsed_label.setText(format_duration(time.monotonic() - self._run_started))

    def _set_status(self, text: str):
        self.status_label.setStyleSheet("")
        self.status_label.setText(text)

    def _show_summary(self, text: str, kind: str, has_report: bool):
        color = {"error": DANGER, "warn": WARNING}.get(kind, SUCCESS)
        self.status_label.setStyleSheet(f"color: {color}; font-weight: 600;" if text else "")
        if text:
            self.status_label.setText(text)
        self.report_btn.setVisible(has_report)
        self.folder_btn.setVisible(has_report)
        if has_report and self._last_outcome:
            self.report_btn.setToolTip(self._last_outcome.report_path)

    def _pick_key_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Service account key", self.key_edit.text(),
                                              "JSON key (*.json);;All files (*)")
        if path:
            self.key_edit.setText(path)

    def _pick_output_dir(self):
        path = QFileDialog.getExistingDirectory(self, "Save reports to", self._output_dir())
        if path:
            self.out_edit.setText(path)

    def _open_report(self):
        if self._last_outcome and self._last_outcome.report_path:
            self._open_path(self._last_outcome.report_path)

    def _open_report_folder(self):
        if self._last_outcome and self._last_outcome.report_path:
            self._open_path(os.path.dirname(self._last_outcome.report_path))

    def _open_path(self, path: str):
        if path and os.path.exists(path):
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        else:
            QMessageBox.information(self, APP_NAME, f"Not found yet:\n{path}")

    def closeEvent(self, event):
        if self.worker is not None:
            answer = QMessageBox.question(self, APP_NAME, "A scan is still running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.worker.cancel()
            if self.thread:
                self.thread.quit()
                self.thread.wait(15000)
        self._save_settings()
        super().closeEvent(event)


def _brush(color: str) -> QBrush:
    return QBrush(QColor(color))


class LimitCombo(QComboBox):
    """Safety limit picker; value() / setValue() in GB, 0 = no limit."""

    PRESETS = (0, 1, 10, 50, 100, 250, 500, 1024, 5120)

    def __init__(self):
        super().__init__()
        for gb in self.PRESETS:
            self.addItem(self._label(gb), gb)

    @staticmethod
    def _label(gb: float) -> str:
        if not gb:
            return "No limit"
        return f"{gb / 1024:g} TB" if gb >= 1024 else f"{gb:g} GB"

    def value(self) -> float:
        return float(self.currentData() or 0)

    def setValue(self, gb: float):
        idx = self.findData(gb)
        if idx < 0:
            self.addItem(self._label(gb), gb)
            idx = self.count() - 1
        self.setCurrentIndex(idx)
