@echo off
REM ===========================================================================
REM  OTD500 Configurator - Windows launcher
REM
REM  Double-click this file. On the first run it creates a local virtual
REM  environment and installs the dependencies (needs internet that one time);
REM  every run after that just starts the app and opens your browser.
REM
REM  Requires Python 3.11+ on PATH:
REM    https://www.python.org/downloads/  (tick "Add python.exe to PATH")
REM
REM  NOTE: the PC's network adapter must be on the OTD500's factory subnet
REM        (192.168.1.x) to reach the device at 192.168.1.1.
REM
REM  Runs on port 8003, independently of the radar (8001) / APU (8002) tools,
REM  so you can run them all at once.
REM
REM  Before first use: copy site.config.example.json -> site.config.json and
REM  manifest.example.csv -> manifest.csv, then fill in real values.
REM ===========================================================================
setlocal
cd /d "%~dp0"

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

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ^(first run only^)...
    %PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
    echo Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)
)

echo.
echo Starting OTD500 Configurator - a browser window will open at http://127.0.0.1:8003
echo Close this window (or press Ctrl+C) to stop the server.
echo.
".venv\Scripts\python.exe" otd_app.py

pause
