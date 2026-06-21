@echo off
REM ===========================================================================
REM  Magos APU Configurator - Windows launcher
REM
REM  Double-click this file. On the first run it creates a local virtual
REM  environment and installs the dependencies (needs internet that one time);
REM  every run after that just starts the app and opens your browser.
REM
REM  Requires Python 3.11+ on PATH:
REM    https://www.python.org/downloads/  (tick "Add python.exe to PATH")
REM
REM  NOTE: the PC's network adapter must be on the APU's factory subnet
REM        (192.168.40.x) to reach an APU at 192.168.40.60.
REM
REM  Runs independently of run_radar.bat (the radar tool) on its own port 8002,
REM  so you can run both at the same time.
REM
REM  NOTE: this folder must sit next to bench-core (the shared package it
REM        installs). Keeping the whole bench/ folder together guarantees this.
REM ===========================================================================
setlocal

REM Always run from the folder this script lives in.
cd /d "%~dp0"

REM Find a Python launcher (prefer the 'py' launcher, fall back to 'python').
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo.
    echo   Python was not found on PATH.
    echo   Install Python 3.11+ from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup, then re-run this file.
    echo.
    pause
    exit /b 1
)

REM First-run setup: create the venv and install dependencies.
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ^(first run only^)...
    %PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)
)

echo.
echo Starting Magos APU Configurator - a browser window will open at http://127.0.0.1:8002
echo Close this window (or press Ctrl+C) to stop the server.
echo.
".venv\Scripts\python.exe" apu_app.py

REM Keep the window open if the server exits/crashes so the error stays visible.
pause
