@echo off
REM ===========================================================================
REM  Kela Bench - launcher body (all the real logic lives here)
REM
REM  This script lives in scripts\ and is CALLed by the bench root's
REM  "Start Bench Tools.bat" AFTER it has finished the bench-central update
REM  (scripts\updater.py). Keeping the update in the tiny outer wrapper (and the
REM  rest here) avoids the cmd.exe self-update trap: cmd reads a .bat
REM  line-by-line by byte offset from disk, so a script that updates itself
REM  resumes at a stale offset once the update rewrites it. This file is only
REM  opened by cmd AFTER the update is done, so it is safe to change freely.
REM
REM  Starts ALL bench configurators in THIS one window and opens the dashboard:
REM
REM    Magos Radar      -> http://127.0.0.1:8001   (adapter on 192.168.40.x)
REM    Magos APU        -> http://127.0.0.1:8002   (adapter on 192.168.40.x)
REM    Teltonika OTD500 -> http://127.0.0.1:8003   (adapter on 192.168.1.x)
REM    Teltonika RUTM08 -> http://127.0.0.1:8004   (adapter on 192.168.1.x)
REM    Raythink Camera  -> http://127.0.0.1:8005   (adapter on 192.168.1.x)
REM    ISR Speaker      -> http://127.0.0.1:8006   (DHCP - scans 192.168.1/2/88.x)
REM
REM  All the tools share ONE .venv at the bench root.
REM ===========================================================================
setlocal
REM This file sits in scripts\ — everything else is addressed from the bench
REM root (one level up), which also becomes the working directory.
cd /d "%~dp0.."
set "ROOT=%CD%"

REM ── Guard against a double-launch ───────────────────────────────────────────
REM If a bench is already running, a second set would just fail to bind all the
REM ports (errors scrolling past) while the dashboard shows the FIRST instance's
REM green dots. Detect a listening tool port and only (re)open the dashboard.
set "BENCH_ALREADY="
for %%p in (8001 8002 8003 8004 8005 8006) do (
  netstat -ano -p tcp 2>nul | findstr "LISTENING" | findstr /c:":%%p " >nul && set "BENCH_ALREADY=1"
)
if defined BENCH_ALREADY (
  echo A bench instance is already running ^(a tool port is in use^) - opening its dashboard.
  start "" "%ROOT%\launcher\index.html"
  exit /b 0
)

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
REM Fail-open, like the wrapper's git pull: an offline bench (the device subnets
REM have no internet) or a flaky pip index must not block a launch when the .venv
REM already has working packages. We only hard-abort above when .venv is missing.
".venv\Scripts\python.exe" -m pip install -r requirements.txt || echo [warn] dependency install failed ^(offline, or a flaky pip index^) - launching with the packages already in .venv.
echo.

REM ── Version + station identity (from the updater's stamp files) ─────────────
REM BENCH_VERSION comes from .bench-build.json — the bundle stamp bench-central
REM injects (git rev-parse is updater.py's fallback for engineer checkouts).
REM Passed to every tool via the environment so it can log/surface it; recorded
REM so 5+ stations can be told apart when debugging "works on my bench".
REM BENCH_STATION_ID / BENCH_CENTRAL_URL come from .bench-station.json (written
REM once by setup-station.ps1) so nobody has to remember to set env vars on a
REM fresh bench PC; values already in the environment win.
set "BENCH_VERSION=unknown"
for /f "delims=" %%v in ('.venv\Scripts\python.exe scripts\updater.py --print-version 2^>nul') do set "BENCH_VERSION=%%v"
if not defined BENCH_STATION_ID (
  for /f "delims=" %%v in ('.venv\Scripts\python.exe scripts\updater.py --print-station station_id 2^>nul') do set "BENCH_STATION_ID=%%v"
)
if not defined BENCH_CENTRAL_URL (
  for /f "delims=" %%v in ('.venv\Scripts\python.exe scripts\updater.py --print-station central_url 2^>nul') do set "BENCH_CENTRAL_URL=%%v"
)
echo Bench version: %BENCH_VERSION%

REM The tools never auto-open their own browser tab (that's opt-in via
REM BENCH_OPEN_BROWSER=1); this launcher opens the one dashboard below instead.

echo Starting all bench configurators in this window...
REM /b runs each tool in THIS console instead of spawning its own window;
REM /d sets the tool's working directory. Closing this window stops them all.
start "Magos Radar"      /d "%ROOT%\magos-config-ui"    /b "%ROOT%\.venv\Scripts\python.exe" app.py
start "Magos APU"        /d "%ROOT%\magos-config-ui"    /b "%ROOT%\.venv\Scripts\python.exe" apu_app.py
start "Teltonika OTD500" /d "%ROOT%\otd-config-ui"      /b "%ROOT%\.venv\Scripts\python.exe" otd_app.py
start "Teltonika RUTM08" /d "%ROOT%\rutm-config-ui"     /b "%ROOT%\.venv\Scripts\python.exe" rutm_app.py
start "Raythink Camera"  /d "%ROOT%\raythink-config-ui" /b "%ROOT%\.venv\Scripts\python.exe" raythink_app.py
start "ISR Speaker"      /d "%ROOT%\speaker-config-ui"  /b "%ROOT%\.venv\Scripts\python.exe" speaker_app.py

REM Open the dashboard immediately — it self-polls every few seconds, so any tool
REM still doing its cold start shows a grey dot that flips green on its own.
start "" "%ROOT%\launcher\index.html"

REM Optionally open the operator guide too (e.g. on the side screen). Off by
REM default; set BENCH_OPEN_GUIDE=1 before running to also open it.
if "%BENCH_OPEN_GUIDE%"=="1" start "" "%ROOT%\launcher\guide.html"

echo.
echo Dashboard opened: launcher\index.html  (guide: launcher\guide.html)
echo All bench tools are now running in THIS window (bench version: %BENCH_VERSION%).
echo Leave it open while you work; close it (or press Ctrl+C) to stop ALL tools.
echo.

REM Keep this console alive so the tools keep running and stay attached to it.
:bench_wait
timeout /t 3600 >nul
goto bench_wait
