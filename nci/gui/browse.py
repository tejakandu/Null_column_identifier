"""Browse BigQuery: pick tables from catalogs with many projects and tables.

Layout: projects | datasets & tables of the current project | selected tables.

- The project list is searchable, with recent projects on top. A project id
  that is not listed can be typed and opened with Enter.
- Typing in the table search lists every dataset of the project in the
  background (in parallel, cached on disk), so a search covers the whole
  project, not only expanded datasets. Space-separated words must all match;
  ``*`` is a wildcard (``sales.*_2024``).
- Selections are kept while switching projects and are shown on the right.
- Everything runs off the UI thread; the window never blocks.
"""

from __future__ import annotations

import re
import threading
import time
from collections import Counter

from PySide6.QtCore import (QModelIndex, QObject, QRunnable, QSortFilterProxyModel, Qt, QThreadPool,
                            QTimer, Signal, Slot)
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence, QShortcut, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QListView, QListWidget, QListWidgetItem, QPushButton,
                               QSplitter, QTreeView, QVBoxLayout, QWidget)

from ..catalog import Catalog, age_text
from ..core import TableTarget
from .style import MUTED
from .workers import Task

KIND = Qt.UserRole + 1          # "dataset" / "table" / "placeholder"
NAME = Qt.UserRole + 2          # dataset id or table id
DATASET = Qt.UserRole + 3       # dataset id (on table items)
RECENT = Qt.UserRole + 4
KEY = Qt.UserRole + 5           # lower-case text the search matches against

FRAME_BUDGET_S = 0.025          # max UI-thread work per batch while loading

SEARCH_DELAY_MS = 180


# ----------------------------------------------------------------------------- matching


def make_matcher(text: str):
    """All whitespace-separated terms must match; '*' and '?' are wildcards."""
    terms = [t for t in text.lower().split() if t]
    if not terms:
        return None
    tests = []
    for term in terms:
        if "*" in term or "?" in term:
            rx = re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in term))
            tests.append(rx.search)
        else:
            tests.append(lambda s, term=term: term in s)
    return lambda s: all(test(s) for test in tests)


def _refilter(proxy: QSortFilterProxyModel, apply) -> None:
    """Change filter state; uses the Qt 6.10+ API when available."""
    if hasattr(proxy, "beginFilterChange"):
        proxy.beginFilterChange()
        apply()
        proxy.endFilterChange()
    else:
        apply()
        proxy.invalidateFilter()


class TreeFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.matcher = None

    def set_text(self, text: str):
        _refilter(self, lambda: setattr(self, "matcher", make_matcher(text)))

    def filterAcceptsRow(self, row, parent):
        if self.matcher is None:
            return True
        key = self.sourceModel().index(row, 0, parent).data(KEY)
        return key is not None and self.matcher(key)


class ProjectFilter(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.matcher = None

    def set_text(self, text: str):
        _refilter(self, lambda: setattr(self, "matcher", make_matcher(text)))

    def filterAcceptsRow(self, row, parent):
        if self.matcher is None:
            return True
        return self.matcher(str(self.sourceModel().index(row, 0, parent).data(NAME)).lower())


# ----------------------------------------------------------------------------- background


class BulkSignals(QObject):
    loaded = Signal(str, str, object, str)   # project, dataset, tables, error
    finished = Signal(str)


class BulkLoad(QRunnable):
    def __init__(self, catalog: Catalog, project: str, datasets: list[str]):
        super().__init__()
        self.catalog, self.project, self.datasets = catalog, project, datasets
        self.cancel = threading.Event()
        self.signals = BulkSignals()

    @Slot()
    def run(self):
        try:
            self.catalog.load_tables_bulk(
                self.project, self.datasets,
                lambda d, tables, err: self.signals.loaded.emit(self.project, d, tables, err),
                self.cancel)
        finally:
            self.signals.finished.emit(self.project)


def _pane(title: str) -> tuple[QFrame, QVBoxLayout, QLabel]:
    frame = QFrame()
    frame.setObjectName("card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(8)
    label = QLabel(title)
    label.setObjectName("sectionTitle")
    layout.addWidget(label)
    return frame, layout, label


def _hint(text: str = "") -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def _button(text: str, tip: str = "", object_name: str = "") -> QPushButton:
    b = QPushButton(text)
    b.setAutoDefault(False)
    b.setDefault(False)
    if tip:
        b.setToolTip(tip)
    if object_name:
        b.setObjectName(object_name)
    return b


# ----------------------------------------------------------------------------- dialog


class BrowseDialog(QDialog):
    def __init__(self, catalog: Catalog, parent=None, recent_projects=(), existing=(),
                 start_project: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Browse BigQuery")
        self.resize(1180, 720)
        self.setMinimumSize(900, 520)
        self.catalog = catalog
        self.pool = QThreadPool.globalInstance()
        self.recent = [p for p in dict.fromkeys(recent_projects) if p]
        self.existing = {t.ref for t in existing}
        self.targets: list[TableTarget] = []

        self.selected: dict[TableTarget, None] = {}
        self.per_dataset: Counter = Counter()      # (project, dataset) -> selected count
        self.project = ""
        self._dataset_items: dict[str, QStandardItem] = {}
        self._table_items: dict[tuple[str, str], QStandardItem] = {}
        self._select_when_loaded: set[tuple[str, str]] = set()
        self._loading: set[tuple[str, str]] = set()
        self._bulk: BulkLoad | None = None
        self._bulk_done: set[str] = set()
        self._bulk_finished_pending = False
        self._bulk_progress = (0, 0)
        self._tasks: set[Task] = set()
        self._syncing = False
        self._project_items: dict[str, QStandardItem] = {}
        self._fill_queue: list[tuple[str, str, object, str]] = []
        self._fill_timer = QTimer(self, interval=0)
        self._fill_timer.timeout.connect(self._drain_fill_queue)

        self._build()
        self._load_projects()
        if start_project:
            QTimer.singleShot(0, lambda: self.open_project(start_project))

    # -- layout ------------------------------------------------------------------------

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_projects())
        splitter.addWidget(self._build_tree())
        splitter.addWidget(self._build_selected())
        splitter.setSizes([260, 600, 300])
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        bottom.addWidget(_hint("Tip: type to search, ↓ to move into the list, Space to tick, "
                               "Ctrl+A to tick everything shown."), 1)
        self.cancel_btn = _button("Cancel")
        self.add_btn = _button("Add tables", object_name="primary")
        self.add_btn.setEnabled(False)
        bottom.addWidget(self.cancel_btn)
        bottom.addWidget(self.add_btn)
        root.addLayout(bottom)

        self.cancel_btn.clicked.connect(self.reject)
        self.add_btn.clicked.connect(self._accept)
        QShortcut(QKeySequence.Find, self, activated=lambda: self._focus(self.table_search))
        QShortcut(QKeySequence("Ctrl+P"), self, activated=lambda: self._focus(self.project_search))

    def _build_projects(self) -> QWidget:
        frame, layout, _ = _pane("Projects")
        self.project_search = QLineEdit()
        self.project_search.setPlaceholderText("Find or type a project id…")
        self.project_search.setClearButtonEnabled(True)
        layout.addWidget(self.project_search)

        self.project_model = QStandardItemModel(self)
        self.project_proxy = ProjectFilter(self)
        self.project_proxy.setSourceModel(self.project_model)
        self.project_list = QListView()
        self.project_list.setModel(self.project_proxy)
        self.project_list.setUniformItemSizes(True)
        self.project_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self.project_list, 1)

        row = QHBoxLayout()
        self.project_status = _hint("Loading projects…")
        self.project_refresh = _button("↻", "Reload the project list")
        self.project_refresh.setFixedWidth(34)
        row.addWidget(self.project_status, 1)
        row.addWidget(self.project_refresh)
        layout.addLayout(row)

        self._project_timer = QTimer(self, singleShot=True, interval=SEARCH_DELAY_MS)
        self._project_timer.timeout.connect(lambda: self._filter_projects())
        self.project_search.textChanged.connect(lambda: self._project_timer.start())
        self.project_search.returnPressed.connect(self._project_enter)
        self.project_search.installEventFilter(self)
        self.project_list.selectionModel().currentChanged.connect(self._project_clicked)
        self.project_list.clicked.connect(lambda idx: self._project_clicked(idx))
        self.project_refresh.clicked.connect(lambda: self._load_projects(refresh=True))
        return frame

    def _build_tree(self) -> QWidget:
        frame, layout, self.tree_title = _pane("Datasets & tables")
        self.table_search = QLineEdit()
        self.table_search.setPlaceholderText("Search all datasets and tables, e.g.  orders   or   sales.*_2024")
        self.table_search.setClearButtonEnabled(True)
        layout.addWidget(self.table_search)

        self.tree_model = QStandardItemModel(self)
        self.tree_model.setHorizontalHeaderLabels(["Name", "Type"])
        self.tree_proxy = TreeFilter(self)
        self.tree_proxy.setSourceModel(self.tree_model)
        self.tree = QTreeView()
        self.tree.setModel(self.tree_proxy)
        self.tree.setUniformRowHeights(True)
        self.tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.Interactive)
        self.tree.header().setStretchLastSection(False)
        self.tree.setColumnWidth(1, 150)
        layout.addWidget(self.tree, 1)

        self.empty_hint = _hint("Pick a project on the left.")
        self.empty_hint.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.empty_hint)

        row = QHBoxLayout()
        self.tree_status = _hint("")
        self.tick_shown = _button("Tick all shown", "Tick every table currently shown (Ctrl+A)")
        self.untick_shown = _button("Untick shown")
        self.tree_refresh = _button("↻", "Reload datasets and tables of this project from BigQuery")
        self.tree_refresh.setFixedWidth(34)
        row.addWidget(self.tree_status, 1)
        row.addWidget(self.tick_shown)
        row.addWidget(self.untick_shown)
        row.addWidget(self.tree_refresh)
        layout.addLayout(row)

        self._table_timer = QTimer(self, singleShot=True, interval=SEARCH_DELAY_MS)
        self._table_timer.timeout.connect(self._apply_table_filter)
        self._expand_timer = QTimer(self, singleShot=True, interval=250)
        self._expand_timer.timeout.connect(self._expand_matches)
        self.table_search.textChanged.connect(lambda: self._table_timer.start())
        self.table_search.installEventFilter(self)
        self.tree.expanded.connect(self._expanded)
        self.tree.doubleClicked.connect(self._double_clicked)
        self.tree_model.itemChanged.connect(self._item_changed)
        self.tick_shown.clicked.connect(lambda: self._tick_shown(True))
        self.untick_shown.clicked.connect(lambda: self._tick_shown(False))
        self.tree_refresh.clicked.connect(lambda: self.open_project(self.project, refresh=True))
        QShortcut(QKeySequence.SelectAll, self.tree, activated=lambda: self._tick_shown(True),
                  context=Qt.WidgetShortcut)
        self._set_tree_enabled(False)
        return frame

    def _build_selected(self) -> QWidget:
        frame, layout, self.selected_title = _pane("Selected (0)")
        self.selected_list = QListWidget()
        self.selected_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.selected_list.setUniformItemSizes(True)
        layout.addWidget(self.selected_list, 1)
        self.selected_hint = _hint("Ticked tables from any project collect here.")
        layout.addWidget(self.selected_hint)
        row = QHBoxLayout()
        self.remove_sel = _button("Remove", "Remove the highlighted tables (Delete)")
        self.clear_sel = _button("Clear")
        row.addStretch(1)
        row.addWidget(self.remove_sel)
        row.addWidget(self.clear_sel)
        layout.addLayout(row)
        self.remove_sel.clicked.connect(self._remove_highlighted)
        self.clear_sel.clicked.connect(self._clear_selection)
        QShortcut(QKeySequence.Delete, self.selected_list, activated=self._remove_highlighted,
                  context=Qt.WidgetShortcut)
        return frame

    # -- keyboard -----------------------------------------------------------------------

    def eventFilter(self, obj, event):
        if event.type() == event.Type.KeyPress and event.key() == Qt.Key_Down:
            view = self.project_list if obj is self.project_search else self.tree
            if view.model().rowCount():
                self._focus(view)
                if not view.currentIndex().isValid():
                    view.setCurrentIndex(view.model().index(0, 0))
                return True
        return super().eventFilter(obj, event)

    def _focus(self, widget):
        widget.setFocus()
        if isinstance(widget, QLineEdit):
            widget.selectAll()

    def keyPressEvent(self, event):
        # Enter must never close the dialog by accident.
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            return
        super().keyPressEvent(event)

    # -- background helper --------------------------------------------------------------

    def _run(self, fn, done, failed):
        task = Task(fn)
        self._tasks.add(task)
        task.signals.done.connect(lambda value: (self._tasks.discard(task), done(value)))
        task.signals.failed.connect(lambda msg: (self._tasks.discard(task), failed(msg)))
        self.pool.start(task)

    # -- projects -----------------------------------------------------------------------

    def _load_projects(self, refresh=False):
        self.project_status.setText("Loading projects…")
        self.project_refresh.setEnabled(False)

        def done(result):
            projects, fetched = result
            self.project_refresh.setEnabled(True)
            self._fill_projects(projects)
            self.project_status.setText(f"{len(projects):,} projects · {age_text(fetched)}")

        def failed(msg):
            self.project_refresh.setEnabled(True)
            self._fill_projects([])
            self.project_status.setText("Could not list projects. Type a project id and press Enter.")
            self.project_status.setToolTip(msg)

        self._run(lambda: self.catalog.projects(refresh), done, failed)

    def _fill_projects(self, projects):
        self.project_model.clear()
        self._project_items = {}
        listed = set(projects)
        bold = QFont()
        bold.setBold(True)
        for p in self.recent + [p for p in projects if p not in self.recent]:
            item = QStandardItem(p)
            item.setData(p, NAME)
            if p in self.recent:
                item.setData(True, RECENT)
                item.setFont(bold)
                item.setToolTip("Recently used" + ("" if p in listed else " (not in your project list)"))
            self._project_items[p] = item
            self.project_model.appendRow(item)
        self._update_project_badges()
        self._filter_projects()
        self._select_project_row(self.project)

    def _filter_projects(self):
        self.project_proxy.set_text(self.project_search.text())
        typed = self.project_search.text().strip()
        if typed and not self.project_proxy.rowCount():
            self.project_status.setText(f"Press Enter to open “{typed}”")

    def _project_enter(self):
        typed = self.project_search.text().strip()
        if self.project_proxy.rowCount() == 1 or (self.project_proxy.rowCount() and not typed):
            self._project_clicked(self.project_proxy.index(0, 0))
        elif typed:
            exact = [self.project_proxy.index(r, 0) for r in range(self.project_proxy.rowCount())
                     if self.project_proxy.index(r, 0).data(NAME) == typed]
            if exact:
                self._project_clicked(exact[0])
            else:
                self.open_project(typed)
        self._focus(self.table_search)

    def _project_clicked(self, index, *_):
        if index is not None and index.isValid():
            self.open_project(index.data(NAME))

    def _select_project_row(self, project):
        for r in range(self.project_proxy.rowCount()):
            idx = self.project_proxy.index(r, 0)
            if idx.data(NAME) == project:
                self.project_list.selectionModel().blockSignals(True)
                self.project_list.setCurrentIndex(idx)
                self.project_list.selectionModel().blockSignals(False)
                return

    def _update_project_badges(self, projects=None):
        counts = Counter(t.project for t in self.selected)
        for p in (self._project_items if projects is None else projects):
            item = self._project_items.get(p)
            if item is not None:
                item.setText(f"{p}   ({counts[p]:,})" if counts[p] else p)

    # -- datasets & tables --------------------------------------------------------------

    def open_project(self, project: str, refresh: bool = False):
        project = project.strip()
        if not project or (project == self.project and not refresh):
            return
        if self._bulk:
            self._bulk.cancel.set()
            self._bulk = None
        self._fill_queue.clear()
        self._bulk_finished_pending = False
        self.project = project
        self._bulk_done.discard(project)
        self._loading.clear()
        self._select_when_loaded.clear()
        self._dataset_items.clear()
        self._table_items.clear()
        self.tree_model.removeRows(0, self.tree_model.rowCount())
        self.tree_title.setText(project)
        self.tree_status.setText("Loading datasets…")
        self.empty_hint.setText("Loading…")
        self.empty_hint.show()
        self._set_tree_enabled(False)
        self._select_project_row(project)
        if project not in self.recent:
            self.recent.insert(0, project)

        def done(result):
            if project != self.project:
                return
            datasets, fetched = result
            self._fill_datasets(datasets)
            self._fetched = fetched
            self._set_tree_enabled(True)
            self.empty_hint.setVisible(not datasets)
            self.empty_hint.setText("This project has no datasets you can see.")
            self._update_tree_status()
            self._apply_table_filter()

        def failed(msg):
            if project != self.project:
                return
            self.tree_status.setText("")
            self.empty_hint.setText(f"⚠ {msg}")
            self.empty_hint.show()

        self._run(lambda: self.catalog.datasets(project, refresh), done, failed)

    def _fill_datasets(self, datasets):
        self._syncing = True
        rows = []
        for ds in datasets:
            item = QStandardItem(ds)
            item.setData("dataset", KIND)
            item.setData(ds, NAME)
            item.setData(ds.lower(), KEY)
            item.setCheckable(True)
            item.setUserTristate(False)
            kind = QStandardItem("Dataset")
            kind.setData("dataset", KIND)
            kind.setForeground(QBrush(QColor(MUTED)))
            self._dataset_items[ds] = item
            cached = self.catalog.cached_tables(self.project, ds)
            if cached is None:
                ph = QStandardItem("Loading…")
                ph.setData("placeholder", KIND)
                ph.setForeground(QBrush(QColor(MUTED)))
                item.appendRow(ph)
            else:
                self._add_table_rows(item, ds, cached)
            self._refresh_dataset_check(ds)
            rows.append([item, kind])
        for row in rows:
            self.tree_model.appendRow(row)
        self._syncing = False

    def _add_table_rows(self, ds_item: QStandardItem, ds: str, tables):
        ds_item.removeRows(0, ds_item.rowCount())
        if not tables:
            empty = QStandardItem("(no tables)")
            empty.setData("placeholder", KIND)
            empty.setForeground(QBrush(QColor(MUTED)))
            empty.setEnabled(False)
            ds_item.appendRow(empty)
            return
        muted = QBrush(QColor(MUTED))
        for name, kind in tables:
            target = TableTarget(self.project, ds, name)
            item = QStandardItem(name)
            item.setData("table", KIND)
            item.setData(name, NAME)
            item.setData(ds, DATASET)
            item.setData(f"{ds}.{name}".lower(), KEY)
            item.setCheckable(True)
            item.setCheckState(Qt.Checked if target in self.selected else Qt.Unchecked)
            label = kind
            if target.ref in self.existing:
                label += " · in list"
                item.setToolTip("Already in your table list")
            type_item = QStandardItem(label)
            type_item.setData("table", KIND)
            type_item.setData(name, NAME)
            type_item.setData(ds, DATASET)
            type_item.setForeground(muted)
            self._table_items[(ds, name)] = item
            ds_item.appendRow([item, type_item])

    def _is_loaded(self, ds: str) -> bool:
        item = self._dataset_items.get(ds)
        return item is not None and not (item.rowCount() == 1 and item.child(0).data(KIND) == "placeholder"
                                         and item.child(0).text() == "Loading…")

    def _expanded(self, proxy_index: QModelIndex):
        index = self.tree_proxy.mapToSource(proxy_index)
        if index.data(KIND) == "dataset":
            self._load_dataset(index.data(NAME))

    def _load_dataset(self, ds: str):
        key = (self.project, ds)
        if self._is_loaded(ds) or key in self._loading:
            return
        self._loading.add(key)
        project = self.project
        self._update_tree_status()

        def done(tables):
            self._loading.discard(key)
            if project == self.project:
                self._dataset_loaded(ds, tables)

        def failed(msg):
            self._loading.discard(key)
            self._select_when_loaded.discard(key)
            if project == self.project and ds in self._dataset_items:
                item = self._dataset_items[ds]
                item.child(0).setText(f"⚠ {msg}")
                self._update_tree_status()

        self._run(lambda: self.catalog.tables(project, ds), done, failed)

    def _dataset_loaded(self, ds: str, tables, update_status: bool = True):
        item = self._dataset_items.get(ds)
        if item is None or self._is_loaded(ds):
            return
        self._syncing = True
        self._add_table_rows(item, ds, tables)
        self._syncing = False
        key = (self.project, ds)
        if key in self._select_when_loaded:
            self._select_when_loaded.discard(key)
            self._set_tables(ds, [t for t, _ in tables], True)
        self._refresh_dataset_check(ds)
        if update_status:
            self._update_tree_status()
        if self.tree_proxy.matcher:
            self._expand_timer.start()

    # -- search -------------------------------------------------------------------------

    def _apply_table_filter(self):
        text = self.table_search.text()
        self.tree_proxy.set_text(text)
        if text.strip() and self.project and self.project not in self._bulk_done:
            self._start_bulk_load()
        self._expand_timer.start()
        self._update_tree_status()

    def _start_bulk_load(self):
        if self._bulk:
            return
        pending = [ds for ds in self._dataset_items if not self._is_loaded(ds)]
        if not pending:
            self._bulk_done.add(self.project)
            return
        bulk = BulkLoad(self.catalog, self.project, pending)
        self._bulk = bulk
        self._bulk_progress = (0, len(pending))
        bulk.signals.loaded.connect(self._bulk_loaded)
        bulk.signals.finished.connect(self._bulk_finished)
        self.pool.start(bulk)
        self._update_tree_status()

    def _bulk_loaded(self, project, ds, tables, error):
        # Queue and insert in small time-boxed batches so the window keeps
        # repainting even when hundreds of datasets arrive at once.
        if project == self.project:
            self._fill_queue.append((project, ds, tables, error))
            if not self._fill_timer.isActive():
                self._fill_timer.start()

    def _drain_fill_queue(self):
        deadline = time.monotonic() + FRAME_BUDGET_S
        while self._fill_queue and time.monotonic() < deadline:
            project, ds, tables, error = self._fill_queue.pop(0)
            if project != self.project:
                continue
            done, total = self._bulk_progress
            self._bulk_progress = (done + 1, total)
            if tables is not None:
                self._dataset_loaded(ds, tables, update_status=False)
            elif ds in self._dataset_items and not self._is_loaded(ds):
                self._dataset_items[ds].child(0).setText(f"⚠ {error}")
        if not self._fill_queue:
            self._fill_timer.stop()
            if self._bulk_finished_pending:
                self._bulk_finished_pending = False
                self._bulk = None
                self._bulk_done.add(self.project)
                self._expand_timer.start()
        self._update_tree_status()

    def _bulk_finished(self, project):
        if project == self.project:
            self._bulk_finished_pending = True
            if not self._fill_timer.isActive():
                self._drain_fill_queue()

    def _expand_matches(self):
        if not self.tree_proxy.matcher:
            return
        rows = self.tree_proxy.rowCount()
        # Expanding thousands of datasets would be slow and unreadable.
        for r in range(min(rows, 200)):
            idx = self.tree_proxy.index(r, 0)
            if self.tree_proxy.rowCount(idx) and not self.tree.isExpanded(idx):
                self.tree.setExpanded(idx, True)

    def _update_tree_status(self):
        if not self.project:
            return
        n_ds = len(self._dataset_items)
        n_tables = len(self._table_items)
        parts = [f"{n_ds:,} datasets", f"{n_tables:,} tables loaded"]
        if self._bulk:
            done, total = self._bulk_progress
            parts = [f"Searching all datasets… {done:,} / {total:,}"]
        elif self._loading:
            parts.append("loading…")
        if self.tree_proxy.matcher:
            shown = sum(self.tree_proxy.rowCount(self.tree_proxy.index(r, 0))
                        for r in range(self.tree_proxy.rowCount()))
            parts.append(f"{shown:,} matching tables")
        elif getattr(self, "_fetched", None):
            parts.append(f"listed {age_text(self._fetched)}")
        self.tree_status.setText(" · ".join(parts))

    def _set_tree_enabled(self, enabled):
        for w in (self.table_search, self.tick_shown, self.untick_shown, self.tree_refresh):
            w.setEnabled(enabled)

    # -- ticking ------------------------------------------------------------------------

    def _item_changed(self, item: QStandardItem):
        if self._syncing or item.column() != 0:
            return
        kind = item.data(KIND)
        checked = item.checkState() == Qt.Checked
        if kind == "table":
            self._set_selected([TableTarget(self.project, item.data(DATASET), item.data(NAME))], checked)
        elif kind == "dataset":
            ds = item.data(NAME)
            if self._is_loaded(ds):
                self._set_tables(ds, [item.child(r).data(NAME) for r in range(item.rowCount())
                                      if item.child(r).data(KIND) == "table"], checked)
            elif checked:
                self._select_when_loaded.add((self.project, ds))
                self._load_dataset(ds)
                self._update_add_button()
            else:
                self._select_when_loaded.discard((self.project, ds))
                self._update_add_button()

    def _double_clicked(self, proxy_index):
        index = self.tree_proxy.mapToSource(proxy_index.siblingAtColumn(0))
        item = self.tree_model.itemFromIndex(index)
        if item is not None and item.data(KIND) == "table":
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)

    def _tick_shown(self, checked: bool):
        by_ds: dict[str, list[str]] = {}
        for r in range(self.tree_proxy.rowCount()):
            ds_idx = self.tree_proxy.index(r, 0)
            ds = ds_idx.data(NAME)
            src = self.tree_model.itemFromIndex(self.tree_proxy.mapToSource(ds_idx))
            if not self._is_loaded(ds):
                if not self.tree_proxy.matcher:
                    if checked:
                        self._select_when_loaded.add((self.project, ds))
                        self._load_dataset(ds)
                    else:
                        self._select_when_loaded.discard((self.project, ds))
                continue
            names = [self.tree_proxy.index(c, 0, ds_idx).data(NAME)
                     for c in range(self.tree_proxy.rowCount(ds_idx))
                     if self.tree_proxy.index(c, 0, ds_idx).data(KIND) == "table"]
            if src is not None and names:
                by_ds[ds] = names
        self._set_selected([TableTarget(self.project, ds, n) for ds, names in by_ds.items() for n in names],
                           checked)
        self._update_add_button()

    def _set_tables(self, ds: str, names: list[str], checked: bool):
        self._set_selected([TableTarget(self.project, ds, n) for n in names], checked)

    def _set_selected(self, targets: list[TableTarget], checked: bool):
        changed = []
        for t in targets:
            if checked and t not in self.selected:
                self.selected[t] = None
                self.per_dataset[(t.project, t.dataset)] += 1
                changed.append(t)
            elif not checked and t in self.selected:
                del self.selected[t]
                self.per_dataset[(t.project, t.dataset)] -= 1
                changed.append(t)
        if not changed:
            self._refresh_all_dataset_checks({t.dataset for t in targets if t.project == self.project})
            return
        self._sync_tree(changed)
        self._sync_selected_list(changed, checked)
        self._update_project_badges({t.project for t in changed})
        self._update_add_button()

    def _sync_tree(self, changed: list[TableTarget]):
        # Ticking never changes what matches the search, so skip the proxy's
        # re-filtering of every changed row (it makes bulk ticks slow).
        self.tree_proxy.setDynamicSortFilter(False)
        self._syncing = True
        datasets = set()
        for t in changed:
            if t.project != self.project:
                continue
            item = self._table_items.get((t.dataset, t.table))
            if item is not None:
                item.setCheckState(Qt.Checked if t in self.selected else Qt.Unchecked)
            datasets.add(t.dataset)
        self._syncing = False
        self._refresh_all_dataset_checks(datasets)
        self.tree_proxy.setDynamicSortFilter(True)

    def _refresh_all_dataset_checks(self, datasets):
        for ds in datasets:
            self._refresh_dataset_check(ds)

    def _refresh_dataset_check(self, ds: str):
        item = self._dataset_items.get(ds)
        if item is None:
            return
        n = self.per_dataset[(self.project, ds)]
        total = sum(1 for r in range(item.rowCount()) if item.child(r).data(KIND) == "table")
        if (self.project, ds) in self._select_when_loaded:
            state = Qt.Checked
        elif n and total and n >= total:
            state = Qt.Checked
        elif n:
            state = Qt.PartiallyChecked
        else:
            state = Qt.Unchecked
        was = self._syncing
        self._syncing = True
        item.setCheckState(state)
        self._syncing = was

    def _sync_selected_list(self, changed, checked):
        lst = self.selected_list
        lst.setUpdatesEnabled(False)
        if checked:
            for t in changed:
                it = QListWidgetItem(t.ref)
                it.setData(Qt.UserRole, t)
                it.setToolTip(t.ref)
                lst.addItem(it)
        else:
            gone = set(changed)
            for r in range(lst.count() - 1, -1, -1):
                if lst.item(r).data(Qt.UserRole) in gone:
                    lst.takeItem(r)
        lst.setUpdatesEnabled(True)
        if checked:
            lst.scrollToBottom()
        self.selected_title.setText(f"Selected ({len(self.selected):,})")

    def _remove_highlighted(self):
        targets = [it.data(Qt.UserRole) for it in self.selected_list.selectedItems()]
        if targets:
            self._set_selected(targets, False)

    def _clear_selection(self):
        self._select_when_loaded.clear()
        self._set_selected(list(self.selected), False)
        self._refresh_all_dataset_checks(self._dataset_items)
        self._update_add_button()

    def _update_add_button(self):
        n = len(self.selected)
        pending = len(self._select_when_loaded)
        if pending:
            self.add_btn.setText(f"Add {n:,} tables (loading {pending} dataset{'s' if pending > 1 else ''}…)")
            self.add_btn.setEnabled(False)
        else:
            self.add_btn.setText(f"Add {n:,} table{'s' if n != 1 else ''}" if n else "Add tables")
            self.add_btn.setEnabled(n > 0)
        self.selected_hint.setVisible(n == 0)

    # -- finish -------------------------------------------------------------------------

    def _accept(self):
        self.targets = list(self.selected)
        self.accept()

    def done(self, result):
        if self._bulk:
            self._bulk.cancel.set()
        super().done(result)
