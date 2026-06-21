@echo off
REM ===========================================================================
REM  Raythink Camera Configurator - Windows launcher
REM
REM  Double-click this file. On the first run it creates a local virtual
REM  environment and installs the dependencies (needs internet that one time);
REM  every run after that just starts the app and opens your browser.
REM
REM  Requires Python 3.11+ on PATH:
REM    https://www.python.org/downloads/  (tick "Add python.exe to PATH")
REM
REM  NOTE: the PC's network adapter must be on the camera's factory subnet
REM        (192.168.1.x) to reach it at 192.168.1.123. After the run the camera
REM        moves to 192.168.88.XX - be able to reach that subnet to confirm.
REM
REM  NOTE: this folder must sit next to bench-core (the shared package
REM        with the bench-UI base that it installs).
REM
REM  Runs on port 8005, independently of the radar (8001) / APU (8002) /
REM  OTD (8003) / RUTM (8004) tools, so you can run them all at once.
REM
REM  Before first use: copy raythink.config.example.json -> raythink.config.json
REM  and drop your real profiles\lan.json + profiles\cellular.json in place.
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
echo Starting Raythink Camera Configurator - a browser window will open at http://127.0.0.1:8005
echo Close this window (or press Ctrl+C) to stop the server.
echo.
".venv\Scripts\python.exe" raythink_app.py

pause
