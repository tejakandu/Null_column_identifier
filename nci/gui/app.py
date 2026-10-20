"""Start the desktop app."""

import sys

from PySide6.QtWidgets import QApplication, QStyleFactory

from .. import APP_NAME, __version__
from ..core import hide_console_windows
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
    from PySide6.QtWidgets import QApplication

    from ..config import load_config
    from ..identity import local_identity
    from .pdflog import write_pdf_log

    _app = QApplication.instance() or QApplication(sys.argv[:1])  # needed for fonts in the PDF
    results = [TableResult(target, "ok", 1, [ColumnResult(spec, 0)])]
    info = RunInfo(run_id="selftest", identity=local_identity("selftest"))
    with tempfile.TemporaryDirectory() as tmp:
        write_report(f"{tmp}/r.xlsx", results, info)
        save_targets(f"{tmp}/l.xlsx", [target])
        assert load_targets(f"{tmp}/l.xlsx")[0] == [target]
        write_pdf_log(f"{tmp}/l.pdf", info, results, ["selftest"])
        with open(f"{tmp}/l.pdf", "rb") as fh:
            assert fh.read(4) == b"%PDF"
    cfg = load_config()
    message = (f"{APP_NAME} {__version__}: OK (bigquery {bigquery.__version__}; config {cfg.source}; "
               f"tracking {'on' if cfg.tracking_enabled else 'off'}, "
               f"updates {'on' if cfg.updates_enabled else 'off'})")
    print(message)
    report = _arg_value("--report")
    if report:
        # The windowed .exe has no console to print to, so the release tool
        # reads the result from this file.
        import json

        with open(report, "w", encoding="utf-8") as fh:
            json.dump({"ok": True, "version": __version__, "message": message,
                       "tracking": cfg.tracking_enabled, "updates": cfg.updates_enabled}, fh)
    return 0


def _arg_value(name: str) -> str:
    args = sys.argv[1:]
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else ""


def main() -> int:
    hide_console_windows()
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
