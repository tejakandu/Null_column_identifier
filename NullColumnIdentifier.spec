# PyInstaller build file: pyinstaller --noconfirm --clean NullColumnIdentifier.spec
# Kept small on purpose: only the Qt pieces the app uses, and none of the
# optional heavy libraries google-cloud-bigquery can use (pandas, numpy, grpc).
import re

from PyInstaller.utils.hooks import copy_metadata

datas = [("nci/nci_config.json", "nci")]  # admin settings (tables, download link)
for dist in ("google-cloud-bigquery", "google-api-core", "google-auth", "google-cloud-core",
             "proto-plus", "protobuf"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

EXCLUDES = [
    # Heavy optional libraries; the app never imports them.
    "pandas", "numpy", "pyarrow", "grpc", "grpc_status", "google.cloud.bigquery_storage",
    "PIL", "lxml", "fpdf", "fontTools", "matplotlib", "IPython", "tkinter",
    # Qt modules the app does not use.
    "PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtPdf",
    "PySide6.QtPdfWidgets", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtWebChannel",
    "PySide6.QtMultimedia", "PySide6.QtOpenGL", "PySide6.QtOpenGLWidgets", "PySide6.QtSvg",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtXml", "PySide6.QtDBus", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtUiTools", "PySide6.QtConcurrent", "PySide6.QtPrintSupport",
]

# Files that the Qt hooks pull in but this app never loads.
DROP = re.compile(
    r"(opengl32sw\.dll|d3dcompiler_\d+\.dll|Qt6(Pdf|Qml|Quick|Network|WebEngine|WebChannel|Svg|OpenGL|"
    r"VirtualKeyboard|Multimedia|Positioning|Sql|Test|Xml)\w*\.(dll|so[.\d]*)|"
    r"[\\/]translations[\\/]|[\\/]plugins[\\/](tls|networkinformation|generic|iconengines|"
    r"platforminputcontexts|qmltooling|egldeviceintegrations|wayland\w*|xcbglintegrations)[\\/]|"
    r"[\\/]imageformats[\\/](?!q(ico|png))\w+)",
    re.IGNORECASE)

a = Analysis(
    ["app.py"],
    datas=datas,
    hiddenimports=["google.cloud.bigquery", "google.oauth2.service_account"],
    excludes=EXCLUDES,
)
a.binaries = [b for b in a.binaries if not DROP.search(b[0]) and not DROP.search(b[1])]
a.datas = [d for d in a.datas if not DROP.search(d[0])]

pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NullColumnIdentifier",
    icon="assets/app.ico",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NullColumnIdentifier")
