@echo off
REM ===========================================================================
REM  Kela Bench - master launcher
REM
REM  Double-click this to start ALL bench configurators at once and open the
REM  dashboard in one browser tab. Each tool runs in its own console window:
REM
REM    Magos Radar      -> http://127.0.0.1:8001   (adapter on 192.168.40.x)
REM    Magos APU        -> http://127.0.0.1:8002   (adapter on 192.168.40.x)
REM    Teltonika OTD500 -> http://127.0.0.1:8003   (adapter on 192.168.1.x)
REM    Teltonika RUTM08 -> http://127.0.0.1:8004   (adapter on 192.168.1.x)
REM    Raythink Camera  -> http://127.0.0.1:8005   (adapter on 192.168.1.x)
REM
REM  The first run of each tool creates its own .venv and installs deps (needs
REM  internet that once). To run just one tool, open its folder and double-click
REM  its own run_*.bat instead.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM ── Pull the latest tools before launching (best-effort) ───────────────────
REM The repo root is one level up from this bench\ folder. If git is missing or
REM the pull fails (offline, or local edits block a fast-forward), we warn and
REM start whatever is already on disk rather than blocking the bench.
where git >nul 2>nul
if errorlevel 1 (
  echo [skip] git not found on PATH - launching the version already on disk.
) else (
  echo Updating to the latest bench tools ^(git pull^)...
  git -C "%~dp0.." pull --ff-only
  if errorlevel 1 echo [warn] git pull failed ^(offline, or local changes^) - launching what's on disk.
)
echo.

REM Tell each tool not to open its own browser tab - this launcher opens the
REM dashboard (which links to all four) instead.
set "BENCH_NO_BROWSER=1"

echo Starting all bench configurators (one console window each)...
start "Magos Radar"      cmd /c "magos-config-ui\run_radar.bat"
start "Magos APU"        cmd /c "magos-config-ui\run_apu.bat"
start "Teltonika OTD500" cmd /c "otd-config-ui\run_otd.bat"
start "Teltonika RUTM08" cmd /c "rutm-config-ui\run_rutm.bat"
start "Raythink Camera"  cmd /c "raythink-config-ui\run_raythink.bat"

REM Give the servers a moment to come up, then open the dashboard once.
timeout /t 3 >nul
start "" "%~dp0launcher\index.html"

echo.
echo Dashboard opened: launcher\index.html
echo Five tool windows are starting. You can close THIS window.
echo (Close a tool's own window to stop that tool.)
