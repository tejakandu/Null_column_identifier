@echo off
REM Release a new version to everyone: asks 3 questions, then tests, builds,
REM checks, uploads to Cloud Storage and publishes it.
REM   release.bat              normal release
REM   release.bat --dry-run    build and check only, publish nothing
setlocal EnableExtensions
cd /d "%~dp0"
title Null Column Identifier - release

set "VENV_PY=.venv\Scripts\python.exe"
if not exist "%VENV_PY%" (
    echo  First run build_windows.bat once - it sets up Python for this folder.
    goto :done
)

echo Updating packages...
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt
if errorlevel 1 (
    echo  Installing packages failed - check the VPN or proxy and try again.
    goto :done
)

"%VENV_PY%" -m nci.release %*

:done
echo.
pause
