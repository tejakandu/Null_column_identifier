"""The list of tables to scan, shown in the main window."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter
from PySide6.QtWidgets import QAbstractItemView, QHeaderView, QTableView

from ..core import TableResult, TableTarget
from .style import MUTED, STATUS_COLORS

COLUMNS = ["Project", "Dataset", "Table", "Key file", "Status", "Null columns", "Coverage"]
COL_PROJECT, COL_DATASET, COL_TABLE, COL_KEY, COL_STATUS, COL_NULLS, COL_COVERAGE = range(7)
EDITABLE = {COL_PROJECT: "project", COL_DATASET: "dataset", COL_TABLE: "table", COL_KEY: "key_file"}


@dataclass
class Row:
    target: TableTarget
    state: str = "ready"   # ready / queued / running / ok / nulls / error / cancelled
    text: str = "Ready"
    result: TableResult | None = None


class TargetModel(QAbstractTableModel):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[Row] = []
        self.locked = False

    # -- Qt API ---------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def flags(self, index):
        f = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() in EDITABLE and not self.locked:
            f |= Qt.ItemIsEditable
        return f

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        col = index.column()
        res = row.result
        if role in (Qt.DisplayRole, Qt.EditRole):
            if col in EDITABLE:
                value = getattr(row.target, EDITABLE[col])
                if col == COL_KEY and role == Qt.DisplayRole:
                    return os.path.basename(value) if value else "(default)"
                return value
            if col == COL_STATUS:
                return row.text
            if col == COL_NULLS and res and res.status == "ok":
                return f"{len(res.null_columns)} of {len(res.columns)}"
            if col == COL_COVERAGE and res and res.coverage is not None:
                return f"{res.coverage:.1%}"
            return ""
        if role == Qt.ForegroundRole:
            if col == COL_STATUS:
                return QBrush(QColor(STATUS_COLORS.get(row.state, MUTED)))
            if col == COL_KEY and not row.target.key_file:
                return QBrush(QColor(MUTED))
            if col == COL_NULLS and res and res.null_columns:
                return QBrush(QColor(STATUS_COLORS["nulls"]))
        if role == Qt.ToolTipRole:
            if col == COL_KEY:
                return row.target.key_file or "Uses the connection chosen on the right"
            if col == COL_STATUS and res and res.error:
                return res.error
            if col == COL_NULLS and res and res.null_columns:
                return "\n".join(c.name for c in res.null_columns[:40]) + (
                    "\n…" if len(res.null_columns) > 40 else "")
        if role == Qt.TextAlignmentRole and col in (COL_NULLS, COL_COVERAGE):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.EditRole or index.column() not in EDITABLE or self.locked:
            return False
        value = str(value).strip().strip("`")
        field = EDITABLE[index.column()]
        if field != "key_file" and not value:
            return False
        row = self.rows[index.row()]
        new_target = dataclasses.replace(row.target, **{field: value})
        if new_target in self.targets() and new_target != row.target:
            return False
        row.target = new_target
        row.state, row.text, row.result = "ready", "Ready", None
        self.dataChanged.emit(self.index(index.row(), 0), self.index(index.row(), len(COLUMNS) - 1))
        self.changed.emit()
        return True

    # -- helpers --------------------------------------------------------------

    def targets(self) -> list[TableTarget]:
        return [r.target for r in self.rows]

    def add(self, targets) -> int:
        existing = set(self.targets())
        new = [t for t in dict.fromkeys(targets) if t not in existing]
        if not new:
            return 0
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
        self.rows.extend(Row(t) for t in new)
        self.endInsertRows()
        self.changed.emit()
        return len(new)

    def remove(self, rows: list[int]):
        for r in sorted(set(rows), reverse=True):
            self.beginRemoveRows(QModelIndex(), r, r)
            del self.rows[r]
            self.endRemoveRows()
        self.changed.emit()

    def clear(self):
        self.beginResetModel()
        self.rows.clear()
        self.endResetModel()
        self.changed.emit()

    def set_key_file(self, rows: list[int], path: str):
        for r in rows:
            self.rows[r].target = dataclasses.replace(self.rows[r].target, key_file=path)
        self._refresh_all()
        self.changed.emit()

    def set_state(self, row: int, state: str, text: str, result: TableResult | None = None):
        r = self.rows[row]
        r.state, r.text = state, text
        if result is not None:
            r.result = result
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(COLUMNS) - 1))

    def reset_states(self, state="ready", text="Ready"):
        for r in self.rows:
            r.state, r.text, r.result = state, text, None
        self._refresh_all()

    def _refresh_all(self):
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(COLUMNS) - 1))


class TargetView(QTableView):
    """Table view with an empty-state message and drag & drop of list files."""

    filesDropped = Signal(list)
    DROP_EXT = (".xlsx", ".xlsm", ".xls", ".csv", ".txt")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(30)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self._drag_active = False

    def setModel(self, model):
        super().setModel(model)
        h = self.horizontalHeader()
        h.setHighlightSections(False)
        h.setStretchLastSection(False)
        for col, mode in ((COL_PROJECT, QHeaderView.Interactive), (COL_DATASET, QHeaderView.Interactive),
                          (COL_TABLE, QHeaderView.Stretch), (COL_KEY, QHeaderView.Interactive),
                          (COL_STATUS, QHeaderView.Interactive), (COL_NULLS, QHeaderView.Interactive),
                          (COL_COVERAGE, QHeaderView.Interactive)):
            h.setSectionResizeMode(col, mode)
        for col, width in ((COL_PROJECT, 150), (COL_DATASET, 140), (COL_KEY, 110),
                           (COL_STATUS, 150), (COL_NULLS, 100), (COL_COVERAGE, 80)):
            self.setColumnWidth(col, width)
        model.modelReset.connect(self.viewport().update)
        model.rowsInserted.connect(self.viewport().update)
        model.rowsRemoved.connect(self.viewport().update)

    # -- drag & drop ------------------------------------------------------------

    def _paths(self, event):
        if not event.mimeData().hasUrls():
            return []
        return [u.toLocalFile() for u in event.mimeData().urls()
                if u.toLocalFile().lower().endswith(self.DROP_EXT)]

    def dragEnterEvent(self, event):
        if self._paths(event):
            self._drag_active = True
            self.viewport().update()
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if self._paths(event):
            event.acceptProposedAction()
        else:
            super().dragMoveEvent(event)

    def dragLeaveEvent(self, event):
        self._drag_active = False
        self.viewport().update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        paths = self._paths(event)
        self._drag_active = False
        self.viewport().update()
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
        else:
            super().dropEvent(event)

    # -- empty state --------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        model = self.model()
        if model is not None and model.rowCount() and not self._drag_active:
            return
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.viewport().rect().adjusted(24, 24, -24, -24)
        if self._drag_active:
            p.setPen(QColor("#1F6FEB"))
            p.setBrush(QColor(31, 111, 235, 18))
            p.drawRoundedRect(rect, 10, 10)
            p.drawText(rect, Qt.AlignCenter, "Drop to import the table list")
        else:
            p.setPen(QColor(MUTED))
            f = p.font()
            f.setPointSize(f.pointSize() + 2)
            p.setFont(f)
            top = QRect(rect.left(), rect.center().y() - 40, rect.width(), 30)
            p.drawText(top, Qt.AlignCenter, "No tables yet")
            f.setPointSize(f.pointSize() - 2)
            p.setFont(f)
            p.drawText(QRect(rect.left(), top.bottom() + 4, rect.width(), 60), Qt.AlignHCenter | Qt.TextWordWrap,
                       "Click “Browse BigQuery” to pick tables, “Add tables” to paste names,\n"
                       "or drag an Excel / CSV list onto this area.")
        p.end()
