"""Dialog for adding tables by pasting their names."""

from __future__ import annotations

from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QVBoxLayout)

from ..core import TableTarget


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
