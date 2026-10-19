# PyInstaller build file: pyinstaller --noconfirm --clean NullColumnIdentifier.spec
from PyInstaller.utils.hooks import copy_metadata

datas = []
for dist in ("google-cloud-bigquery", "google-api-core", "google-auth", "google-cloud-core",
             "grpcio-status", "proto-plus", "protobuf"):
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

a = Analysis(
    ["app.py"],
    datas=datas,
    hiddenimports=["google.cloud.bigquery", "google.oauth2.service_account"],
    excludes=["tkinter", "matplotlib", "IPython", "PySide6.QtWebEngineCore",
              "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
              "PySide6.QtMultimedia", "PySide6.QtPdf"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="NullColumnIdentifier",
    icon="assets/app.ico",
    console=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="NullColumnIdentifier")
