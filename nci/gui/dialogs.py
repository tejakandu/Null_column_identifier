"""Dialogs for adding tables: paste names, or browse BigQuery."""

from __future__ import annotations

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPlainTextEdit, QPushButton, QTreeWidget,
                               QTreeWidgetItem, QTreeWidgetItemIterator, QVBoxLayout)

from ..core import TableTarget
from .workers import Task

LOADING = "__loading__"


class AddTablesDialog(QDialog):
    """Paste one table per line; short names use the default project/dataset."""

    def __init__(self, parent=None, project: str = "", dataset: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Add tables")
        self.resize(560, 420)
        self.targets: list[TableTarget] = []

        layout = QVBoxLayout(self)
        intro = QLabel("Paste or type table names, one per line (commas also work).\n"
                       "Use project.dataset.table, or just the table name when the defaults below are set.")
        intro.setObjectName("hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.project = QLineEdit(project)
        self.project.setPlaceholderText("e.g. my-gcp-project")
        self.dataset = QLineEdit(dataset)
        self.dataset.setPlaceholderText("e.g. sales")
        form.addRow("Default project", self.project)
        form.addRow("Default dataset", self.dataset)
        layout.addLayout(form)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("my-gcp-project.sales.orders\nmy-gcp-project.sales.customers\nreturns")
        layout.addWidget(self.text, 1)
        self.feedback = QLabel()
        self.feedback.setObjectName("hint")
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText("Add")
        self.ok.setObjectName("primary")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for w in (self.project, self.dataset):
            w.textChanged.connect(self._parse)
        self.text.textChanged.connect(self._parse)
        self._parse()
        self.text.setFocus()

    def _parse(self):
        targets, bad = [], []
        raw = self.text.toPlainText().replace(",", "\n").replace(";", "\n").replace("\t", "\n")
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                targets.append(TableTarget.parse(line, default_project=self.project.text().strip(),
                                                 default_dataset=self.dataset.text().strip()))
            except ValueError:
                bad.append(line.strip())
        self.targets = list(dict.fromkeys(targets))
        msg = f"{len(self.targets)} table(s) ready to add." if self.targets else ""
        if bad:
            msg += f"  Not understood: {', '.join(bad[:5])}{'…' if len(bad) > 5 else ''}"
        self.feedback.setText(msg.strip())
        self.ok.setEnabled(bool(self.targets))


class BrowseDialog(QDialog):
    """Pick datasets / tables from a BigQuery project with checkboxes."""

    def __init__(self, client_for_project, parent=None, project: str = "", recent_projects=()):
        super().__init__(parent)
        self.setWindowTitle("Browse BigQuery")
        self.resize(620, 620)
        self.client_for_project = client_for_project
        self.pool = QThreadPool.globalInstance()
        self.targets: list[TableTarget] = []
        self._tasks: set[Task] = set()
        self._project_loaded = ""
        self._waiters: dict[int, list] = {}

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.setInsertPolicy(QComboBox.NoInsert)
        self.project.lineEdit().setPlaceholderText("GCP project id, e.g. my-gcp-project")
        for p in dict.fromkeys([project, *recent_projects]):
            if p:
                self.project.addItem(p)
        self.project.setCurrentText(project)
        self.load_btn = QPushButton("Load datasets")
        self.find_btn = QPushButton("Find my projects")
        self.find_btn.setToolTip("List the projects your account can see")
        top.addWidget(QLabel("Project"))
        top.addWidget(self.project, 1)
        top.addWidget(self.load_btn)
        top.addWidget(self.find_btn)
        layout.addLayout(top)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter datasets and tables…")
        self.search.setClearButtonEnabled(True)
        layout.addWidget(self.search)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type"])
        self.tree.setColumnWidth(0, 420)
        self.tree.setUniformRowHeights(True)
        layout.addWidget(self.tree, 1)

        self.status = QLabel("Enter a project and click “Load datasets”.")
        self.status.setObjectName("hint")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok = buttons.button(QDialogButtonBox.Ok)
        self.ok.setText("Add selected")
        self.ok.setObjectName("primary")
        self.ok.setEnabled(False)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.load_btn.clicked.connect(self.load_datasets)
        self.project.lineEdit().returnPressed.connect(self.load_datasets)
        self.find_btn.clicked.connect(self.find_projects)
        self.tree.itemExpanded.connect(self._expanded)
        self.tree.itemChanged.connect(self._item_changed)
        self.search.textChanged.connect(self._filter)

        if project:
            QTimer.singleShot(0, self.load_datasets)

    # -- background helpers ---------------------------------------------------

    def _run(self, fn, done, failed=None):
        task = Task(fn)
        self._tasks.add(task)

        def finish(handler):
            def inner(value):
                self._tasks.discard(task)
                if handler:
                    handler(value)
            return inner

        task.signals.done.connect(finish(done))
        task.signals.failed.connect(finish(failed or self._show_error))
        self.pool.start(task)

    def _show_error(self, msg):
        self.status.setText(f"⚠ {msg}")
        self.load_btn.setEnabled(True)
        self.find_btn.setEnabled(True)

    # -- projects / datasets / tables -------------------------------------------

    def find_projects(self):
        self.find_btn.setEnabled(False)
        self.status.setText("Looking up projects…")
        current = self.project.currentText().strip()

        def fetch():
            client = self.client_for_project(current)
            return sorted(p.project_id for p in client.list_projects())

        def done(projects):
            self.find_btn.setEnabled(True)
            text = self.project.currentText()
            self.project.clear()
            self.project.addItems(projects)
            self.project.setCurrentText(text or (projects[0] if projects else ""))
            self.status.setText(f"Found {len(projects)} project(s). Pick one and click “Load datasets”."
                                if projects else "No projects found for this account.")

        self._run(fetch, done)

    def load_datasets(self):
        project = self.project.currentText().strip()
        if not project:
            self.status.setText("Enter a project id first.")
            return
        self.load_btn.setEnabled(False)
        self.tree.clear()
        self.status.setText(f"Loading datasets in {project}…")

        def fetch():
            client = self.client_for_project(project)
            return sorted(d.dataset_id for d in client.list_datasets(project))

        def done(datasets):
            self.load_btn.setEnabled(True)
            self._project_loaded = project
            self.tree.blockSignals(True)
            for ds in datasets:
                item = QTreeWidgetItem([ds, "Dataset"])
                # Auto-tristate is switched on once the tables are loaded; until then
                # the placeholder child would force the dataset back to unchecked.
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                placeholder = QTreeWidgetItem(["Loading…"])
                placeholder.setData(0, Qt.UserRole, LOADING)
                item.addChild(placeholder)
                self.tree.addTopLevelItem(item)
            self.tree.blockSignals(False)
            self.status.setText(f"{len(datasets)} dataset(s). Expand a dataset to see its tables, "
                                "or tick a whole dataset." if datasets else "This project has no datasets you can see.")
            self._filter(self.search.text())

        self._run(fetch, done)

    def _expanded(self, item):
        if item.parent() is None:
            self._load_tables(item)

    def _needs_load(self, ds_item) -> bool:
        return ds_item.childCount() == 1 and ds_item.child(0).data(0, Qt.UserRole) == LOADING

    def _load_tables(self, ds_item, then=None):
        if not self._needs_load(ds_item):
            if then:
                then()
            return
        waiters = self._waiters.setdefault(id(ds_item), [])
        if then:
            waiters.append(then)
        if ds_item.data(1, Qt.UserRole) == "loading":
            return
        ds_item.setData(1, Qt.UserRole, "loading")
        project, dataset = self._project_loaded, ds_item.text(0)

        def fetch():
            client = self.client_for_project(project)
            return sorted(((t.table_id, (t.table_type or "TABLE").title().replace("_", " "))
                           for t in client.list_tables(f"{project}.{dataset}")))

        def done(tables):
            self.tree.blockSignals(True)
            state = ds_item.checkState(0)
            ds_item.setData(1, Qt.UserRole, "loaded")
            ds_item.takeChildren()
            for name, kind in tables:
                child = QTreeWidgetItem([name, kind])
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Checked if state == Qt.Checked else Qt.Unchecked)
                ds_item.addChild(child)
            if not tables:
                empty = QTreeWidgetItem(["(no tables)"])
                empty.setFlags(Qt.NoItemFlags)
                ds_item.addChild(empty)
            else:
                ds_item.setFlags(ds_item.flags() | Qt.ItemIsAutoTristate)
            self.tree.blockSignals(False)
            self._filter(self.search.text())
            self._update_count()
            for callback in self._waiters.pop(id(ds_item), []):
                callback()

        def failed(msg):
            ds_item.setData(1, Qt.UserRole, None)
            self._waiters.pop(id(ds_item), None)
            self._show_error(msg)
            self._update_count()

        self._run(fetch, done, failed)

    def _item_changed(self, item, column):
        if column == 0:
            self._update_count()

    def _checked(self) -> tuple[list[TableTarget], list[QTreeWidgetItem]]:
        targets, pending = [], []
        for i in range(self.tree.topLevelItemCount()):
            ds = self.tree.topLevelItem(i)
            if self._needs_load(ds):
                if ds.checkState(0) == Qt.Checked:
                    pending.append(ds)
                continue
            for j in range(ds.childCount()):
                child = ds.child(j)
                if child.flags() & Qt.ItemIsUserCheckable and child.checkState(0) == Qt.Checked:
                    targets.append(TableTarget(self._project_loaded, ds.text(0), child.text(0)))
        return targets, pending

    def _update_count(self):
        targets, pending = self._checked()
        parts = []
        if targets:
            parts.append(f"{len(targets)} table(s)")
        if pending:
            parts.append(f"{len(pending)} whole dataset(s)")
        self.ok.setEnabled(bool(parts))
        if parts:
            self.status.setText("Selected: " + " + ".join(parts))

    def _filter(self, text):
        text = text.strip().lower()
        it = QTreeWidgetItemIterator(self.tree)
        while it.value():
            item = it.value()
            if item.parent() is None:
                ds_match = text in item.text(0).lower()
                any_child = False
                for j in range(item.childCount()):
                    child = item.child(j)
                    show = not text or ds_match or text in child.text(0).lower()
                    child.setHidden(not show)
                    any_child = any_child or (show and bool(child.flags() & Qt.ItemIsUserCheckable))
                item.setHidden(bool(text) and not ds_match and not any_child)
                if text and any_child and not ds_match:
                    item.setExpanded(True)
            it += 1

    def _accept(self):
        targets, pending = self._checked()
        if not pending:
            self.targets = targets
            self.accept()
            return
        self.ok.setEnabled(False)
        self.status.setText(f"Listing tables in {len(pending)} dataset(s)…")
        remaining = {id(ds) for ds in pending}

        def one_done(ds):
            def inner():
                remaining.discard(id(ds))
                if not remaining:
                    self.targets = self._checked()[0]
                    self.accept()
            return inner

        for ds in pending:
            self._load_tables(ds, then=one_done(ds))
