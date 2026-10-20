@echo off
REM Builds dist\NullColumnIdentifier.exe (one self-contained file)
REM Double-click it, or run it from a Command Prompt in this folder.
setlocal EnableExtensions
cd /d "%~dp0"
title Null Column Identifier - build

echo ============================================================
echo  Null Column Identifier - Windows build
echo ============================================================
echo.

REM ---- 1. Python -------------------------------------------------------------
echo [1/5] Finding Python...
set "VENV_PY=.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto :have_venv

set "PY="
py -3 -c "import sys" >nul 2>&1 && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>&1 && set "PY=python"
if not defined PY (
    echo.
    echo  Python was not found.
    echo  Install Python 3.10 or newer from https://www.python.org/downloads/
    echo  and tick "Add python.exe to PATH" during setup. Then run this again.
    goto :failed
)
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo.
    echo  Python 3.10 or newer is needed. Found:
    %PY% --version
    goto :failed
)
echo       Creating a virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 goto :failed

:have_venv
"%VENV_PY%" --version
"%VENV_PY%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"
if errorlevel 1 (
    echo.
    echo  The .venv folder uses a Python older than 3.10.
    echo  Delete the .venv folder and run this again.
    goto :failed
)

REM ---- 2. Packages -------------------------------------------------------------
echo.
echo [2/5] Installing packages (first time takes a few minutes)...
"%VENV_PY%" -m pip install --disable-pip-version-check -q -r requirements-dev.txt
if errorlevel 1 (
    echo.
    echo  Installing packages failed. On a company network this is usually the
    echo  proxy or VPN: connect to the VPN, or ask IT for the pip proxy settings.
    goto :failed
)

REM ---- 3. Tests ----------------------------------------------------------------
echo.
echo [3/5] Running tests...
"%VENV_PY%" -m pytest -q tests
if errorlevel 1 (
    echo.
    echo  Some tests failed - see above. Please send a screenshot of this window.
    choice /C YN /M "Build the app anyway"
    if errorlevel 2 goto :failed
)

REM ---- 4. Build ----------------------------------------------------------------
echo.
echo [4/5] Building the app...
tasklist /FI "IMAGENAME eq NullColumnIdentifier.exe" 2>nul | find /I "NullColumnIdentifier.exe" >nul
if not errorlevel 1 (
    echo.
    echo  Null Column Identifier is running. Close it first, then press a key.
    pause >nul
)
"%VENV_PY%" -m PyInstaller --noconfirm --clean --log-level WARN NullColumnIdentifier.spec
if errorlevel 1 (
    echo.
    echo  The build failed - see above. If the error mentions "Access is denied",
    echo  close the app and any open dist folder, or ask IT whether antivirus
    echo  blocks PyInstaller.
    goto :failed
)

REM ---- 5. Check ----------------------------------------------------------------
echo.
echo [5/5] Checking the built app...
dist\NullColumnIdentifier.exe --selftest
if errorlevel 1 (
    echo.
    echo  The app was built but its self-test failed.
    goto :failed
)

echo.
echo ============================================================
echo  Done. The app is one file you can share as is:
echo    %CD%\dist\NullColumnIdentifier.exe
echo.
echo  To release a new version to everyone, run release.bat
echo ============================================================
echo.
pause
exit /b 0

:failed
echo.
echo ============================================================
echo  Build stopped. Scroll up for the reason.
echo ============================================================
echo.
pause
exit /b 1
