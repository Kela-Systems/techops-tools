@echo off
REM ===========================================================================
REM  Kela Bench - master launcher
REM
REM  Double-click this to start ALL bench configurators at once and open the
REM  dashboard in one browser tab. All tools run inside THIS one window:
REM
REM    Magos Radar      -> http://127.0.0.1:8001   (adapter on 192.168.40.x)
REM    Magos APU        -> http://127.0.0.1:8002   (adapter on 192.168.40.x)
REM    Teltonika OTD500 -> http://127.0.0.1:8003   (adapter on 192.168.1.x)
REM    Teltonika RUTM08 -> http://127.0.0.1:8004   (adapter on 192.168.1.x)
REM    Raythink Camera  -> http://127.0.0.1:8005   (adapter on 192.168.1.x)
REM
REM  All five tools share ONE .venv in this folder. Every launch pulls the
REM  latest tools (git) and installs requirements (needs internet that once,
REM  then it's a near-instant no-op). Set BENCH_NO_PULL=1 to freeze the on-disk
REM  version.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM ── Pull the latest tools before launching (best-effort) ───────────────────
if "%BENCH_NO_PULL%"=="1" (
  echo [skip] BENCH_NO_PULL=1 - launching the version already on disk.
) else (
  where git >nul 2>nul
  if errorlevel 1 (
    echo [skip] git not found on PATH - launching the version already on disk.
  ) else (
    echo Updating to the latest bench tools ^(git pull^)...
    git pull --ff-only
    if errorlevel 1 echo [warn] git pull failed ^(offline, or local changes^) - launching what's on disk.
  )
)
echo.

REM ── Locate a Python to build the shared venv with (first run only) ──────────
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
  if not defined PY (
    echo.
    echo   Python was not found on PATH.
    echo   Install Python 3.11+ from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" during setup, then re-run this file.
    echo.
    pause
    exit /b 1
  )
  echo Creating the shared virtual environment ^(first run only^)...
  %PY% -m venv .venv || (echo Could not create the virtual environment. & pause & exit /b 1)
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
)

echo Installing/updating dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)
echo.

REM The tools never auto-open their own browser tab (that's opt-in via
REM BENCH_OPEN_BROWSER=1); this launcher opens the one dashboard below instead.

echo Starting all bench configurators in this window...
REM /b runs each tool in THIS console instead of spawning its own window;
REM /d sets the tool's working directory. Closing this window stops them all.
start "Magos Radar"      /d "%~dp0magos-config-ui"    /b "%~dp0.venv\Scripts\python.exe" app.py
start "Magos APU"        /d "%~dp0magos-config-ui"    /b "%~dp0.venv\Scripts\python.exe" apu_app.py
start "Teltonika OTD500" /d "%~dp0otd-config-ui"      /b "%~dp0.venv\Scripts\python.exe" otd_app.py
start "Teltonika RUTM08" /d "%~dp0rutm-config-ui"     /b "%~dp0.venv\Scripts\python.exe" rutm_app.py
start "Raythink Camera"  /d "%~dp0raythink-config-ui" /b "%~dp0.venv\Scripts\python.exe" raythink_app.py

REM Give the servers a moment to come up, then open the dashboard once.
timeout /t 6 >nul
start "" "%~dp0launcher\index.html"

REM Optionally open the operator guide too (e.g. on the side screen). Off by
REM default; set BENCH_OPEN_GUIDE=1 before running to also open it.
if "%BENCH_OPEN_GUIDE%"=="1" start "" "%~dp0launcher\guide.html"

echo.
echo Dashboard opened: launcher\index.html  (guide: launcher\guide.html)
echo All five tools are now running in THIS window.
echo Leave it open while you work; close it (or press Ctrl+C) to stop ALL tools.
echo.

REM Keep this console alive so the tools keep running and stay attached to it.
:bench_wait
timeout /t 3600 >nul
goto bench_wait
