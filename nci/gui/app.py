"""Start the desktop app."""

import sys

from PySide6.QtWidgets import QApplication, QStyleFactory

from .. import APP_NAME, __version__
from .main_window import MainWindow
from .style import STYLESHEET, app_font, app_icon


def selftest() -> int:
    """Check the libraries a scan needs are importable (used to verify builds)."""
    import fpdf  # noqa: F401
    import openpyxl  # noqa: F401
    from google.cloud import bigquery  # noqa: F401
    from google.oauth2 import service_account  # noqa: F401

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
