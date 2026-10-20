"""Update checks, the forced-update prompt and the About window."""

from __future__ import annotations

import html

from PySide6.QtCore import QObject, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout)

from .. import APP_NAME, AUTHOR, DESIGNED_BY, __version__
from ..config import AppConfig
from ..releases import UpdateDecision, check_for_update
from .style import app_icon
from .workers import Task


class UpdateChecker(QObject):
    """Checks the releases table now and then, off the UI thread.

    ``client_factory()`` is called on the UI thread (it may read widgets) and
    returns a function project -> BigQuery client, which runs on a worker
    thread. Failures are reported via ``failed`` and otherwise ignored, so
    the app keeps working (fail open).
    """

    decided = Signal(object)  # UpdateDecision
    failed = Signal(str)

    def __init__(self, config: AppConfig, client_factory, parent=None):
        super().__init__(parent)
        self.config = config
        self.client_factory = client_factory
        self.last: UpdateDecision | None = None
        self._task: Task | None = None
        self._timer = QTimer(self, interval=config.update_check_minutes * 60_000)
        self._timer.timeout.connect(self.check)

    @property
    def enabled(self) -> bool:
        return self.config.updates_enabled

    def start(self, delay_ms: int = 1500):
        if self.enabled:
            QTimer.singleShot(delay_ms, self.check)
            self._timer.start()

    def check(self):
        if not self.enabled or self._task is not None:
            return
        table = self.config.releases_table
        client_for_project = self.client_factory()
        task = Task(lambda: check_for_update(client_for_project(table.split(".")[0]), table))
        self._task = task
        task.signals.done.connect(self._done)
        task.signals.failed.connect(self._failed)
        QThreadPool.globalInstance().start(task)

    def _done(self, decision: UpdateDecision):
        self._task = None
        self.last = decision
        self.decided.emit(decision)

    def _failed(self, message: str):
        self._task = None
        self.failed.emit(message)


def _download_url(decision: UpdateDecision, config: AppConfig) -> str:
    for release in (decision.required, decision.latest):
        if release is not None and release.download_url:
            return release.download_url
    return config.download_url


def _notes_text(decision: UpdateDecision) -> str:
    return "\n\n".join(decision.notes) if decision.notes else ""


class ForcedUpdateDialog(QDialog):
    """Blocks the app: the only ways out are Download (then quit) or Quit."""

    def __init__(self, decision: UpdateDecision, config: AppConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} – update required")
        self.setWindowIcon(app_icon())
        self.setModal(True)
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint | Qt.WindowTitleHint)  # no ✕
        self.setMinimumWidth(500)
        self.url = _download_url(decision, config)
        self.downloaded = False
        latest = decision.latest

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)
        title = QLabel(f"Version {html.escape(latest.version)} is required")
        title.setObjectName("title")
        layout.addWidget(title)
        body = QLabel(
            f"You are using version <b>{__version__}</b>, which is no longer supported. "
            f"Please download version <b>{html.escape(latest.version)}</b> to keep using "
            f"{APP_NAME}.<br/><br/>This window will close the app.")
        body.setWordWrap(True)
        layout.addWidget(body)
        notes = _notes_text(decision)
        if notes:
            layout.addWidget(QLabel("What's new"))
            box = QPlainTextEdit(notes)
            box.setReadOnly(True)
            box.setMaximumHeight(140)
            layout.addWidget(box)
        if not self.url:
            missing = QLabel("Ask your administrator for the download link.")
            missing.setObjectName("hint")
            layout.addWidget(missing)

        row = QHBoxLayout()
        row.addStretch(1)
        quit_btn = QPushButton("Quit")
        quit_btn.setAutoDefault(False)
        self.download_btn = QPushButton(f"Download {latest.version}")
        self.download_btn.setObjectName("primary")
        self.download_btn.setEnabled(bool(self.url))
        row.addWidget(quit_btn)
        row.addWidget(self.download_btn)
        layout.addLayout(row)
        quit_btn.clicked.connect(self.accept)
        self.download_btn.clicked.connect(self._download)
        if self.url:
            self.download_btn.setFocus()

    def _download(self):
        QDesktopServices.openUrl(QUrl(self.url))
        self.downloaded = True
        self.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:  # Esc must not slip past the prompt
            return
        super().keyPressEvent(event)

    def reject(self):  # Alt+F4 / Esc: treat as Quit
        self.accept()


class AboutDialog(QDialog):
    def __init__(self, config: AppConfig, checker: UpdateChecker | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.setMinimumWidth(440)
        self.config = config
        self.checker = checker

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(6)
        top = QHBoxLayout()
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(56, 56))
        top.addWidget(icon)
        names = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        version = QLabel(f"Version {__version__}")
        version.setObjectName("subtitle")
        names.addWidget(title)
        names.addWidget(version)
        top.addLayout(names, 1)
        layout.addLayout(top)
        layout.addSpacing(8)

        credits = QLabel(f"<table cellpadding='2'>"
                         f"<tr><td style='color:#6B7280'>Designed by</td><td><b>{DESIGNED_BY}</b></td></tr>"
                         f"<tr><td style='color:#6B7280'>Author</td><td><b>{AUTHOR}</b></td></tr></table>")
        layout.addWidget(credits)
        layout.addSpacing(8)

        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("hint")
        layout.addWidget(self.status)

        row = QHBoxLayout()
        self.check_btn = QPushButton("Check for updates")
        self.download_btn = QPushButton("Download")
        self.download_btn.setObjectName("primary")
        self.download_btn.hide()
        close = QPushButton("Close")
        row.addWidget(self.check_btn)
        row.addStretch(1)
        row.addWidget(self.download_btn)
        row.addWidget(close)
        layout.addSpacing(6)
        layout.addLayout(row)
        close.clicked.connect(self.accept)
        self.check_btn.clicked.connect(self._check)
        self.download_btn.clicked.connect(self._download)

        self.check_btn.setVisible(bool(checker and checker.enabled))
        if checker is not None:
            checker.decided.connect(self._show)
            checker.failed.connect(self._failed)
        self._show(checker.last if checker else None)

    def _check(self):
        self.status.setText("Checking…")
        self.check_btn.setEnabled(False)
        self.checker.check()

    def _show(self, decision: UpdateDecision | None):
        self.check_btn.setEnabled(True)
        self._decision = decision
        if not (self.checker and self.checker.enabled):
            self.status.setText("Update checks are not set up for this copy.")
        elif decision is None:
            self.status.setText("Not checked yet.")
        elif decision.kind == "none":
            self.status.setText("You have the latest version.")
        else:
            self.status.setText(f"Version {decision.latest.version} is available"
                                + (" (required)." if decision.kind == "forced" else "."))
        url = _download_url(decision, self.config) if decision and decision.kind != "none" else ""
        self.download_btn.setVisible(bool(url))
        self._url = url

    def _failed(self, message: str):
        self.check_btn.setEnabled(True)
        self.status.setText(f"Could not check for updates: {message}")

    def _download(self):
        if self._url:
            QDesktopServices.openUrl(QUrl(self._url))
