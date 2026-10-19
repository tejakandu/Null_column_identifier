@echo off
REM Builds dist\NullColumnIdentifier\NullColumnIdentifier.exe
REM Run from this folder in a normal Command Prompt. Needs Python 3.10+ on PATH.
setlocal
cd /d "%~dp0"

if not exist .venv (
    python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip >nul
python -m pip install -r requirements-dev.txt || goto :error

python -m pytest -q tests || goto :error

pyinstaller --noconfirm --clean NullColumnIdentifier.spec || goto :error
dist\NullColumnIdentifier\NullColumnIdentifier.exe --selftest || goto :error

echo.
echo Done. The app is in dist\NullColumnIdentifier\NullColumnIdentifier.exe
echo Zip the whole dist\NullColumnIdentifier folder to share it.
exit /b 0

:error
echo.
echo Build failed. See the messages above.
exit /b 1
