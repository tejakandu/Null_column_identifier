"""Start the desktop app."""

import sys

from PySide6.QtWidgets import QApplication, QStyleFactory

from .. import APP_NAME, __version__
from .main_window import MainWindow
from .style import STYLESHEET, app_font, app_icon


def selftest() -> int:
    """Exercise what a scan needs, without network (used to verify builds)."""
    import tempfile

    import google.auth.transport.requests  # noqa: F401
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery
    from google.oauth2 import service_account  # noqa: F401

    from ..core import ColumnResult, ColumnSpec, TableResult, TableTarget, build_query
    from ..inputs import load_targets, save_targets
    from ..report import RunInfo, write_report

    bigquery.Client(project="selftest", credentials=AnonymousCredentials())
    bigquery.QueryJobConfig(maximum_bytes_billed=1, labels={"app": "selftest"})
    target = TableTarget("p", "d", "t")
    spec = ColumnSpec("c", "STRING", "NULLABLE", "t.`c` IS NOT NULL")
    build_query(target, [spec])
    with tempfile.TemporaryDirectory() as tmp:
        write_report(f"{tmp}/r.xlsx", [TableResult(target, "ok", 1, [ColumnResult(spec, 0)])], RunInfo())
        save_targets(f"{tmp}/l.xlsx", [target])
        assert load_targets(f"{tmp}/l.xlsx")[0] == [target]
    print(f"{APP_NAME} {__version__}: OK (bigquery {bigquery.__version__})")
    return 0


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if sys.platform == "win32":
        # Own taskbar icon/group instead of python.exe's.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NullColumnIdentifier.App")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName("NullColumnIdentifier")
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setFont(app_font())
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app_icon())
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
