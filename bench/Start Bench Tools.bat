@echo off
REM ===========================================================================
REM  Kela Bench - master launcher  (STABLE WRAPPER - keep this file byte-stable)
REM
REM  Double-click this to start ALL bench configurators and open the dashboard.
REM
REM  WHY THIS FILE IS TINY (and must STAY tiny):
REM  cmd.exe reads a .bat line-by-line by byte OFFSET from disk. A script that
REM  updates ITSELF gets corrupted the instant the update rewrites it -
REM  execution resumes at a stale offset inside the new bytes - and that happens
REM  on exactly the runs that carry an update (the ones you most need to work
REM  when rolling changes to N stations). So this entry point only runs the
REM  updater, then hands off to scripts\bench-launch.bat, which is not open in
REM  cmd until AFTER the update finishes and is therefore safe to change.
REM
REM  DO NOT add launch logic here. Put every change in scripts\bench-launch.bat,
REM  and do not let commits modify this file - if its byte length changes across
REM  an update it can corrupt its own run. Set BENCH_NO_PULL=1 to freeze the
REM  on-disk version (scripts\updater.py honors it too).
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM ── Converge to the pinned bench-central release before handing off ────────
REM updater.py is fail-open by design: offline, no station file, a git (dev)
REM checkout, or BENCH_NO_PULL=1 all just print why and launch what's on disk.
set "BENCH_UPDATE_PY="
where py >nul 2>nul && set "BENCH_UPDATE_PY=py -3"
if not defined BENCH_UPDATE_PY (
  where python >nul 2>nul && set "BENCH_UPDATE_PY=python"
)
if not defined BENCH_UPDATE_PY (
  echo [skip] Python not found on PATH - launching the version already on disk.
) else (
  if exist "%~dp0scripts\updater.py" (
    echo Checking bench-central for the pinned bench release...
    %BENCH_UPDATE_PY% "%~dp0scripts\updater.py"
  ) else (
    echo [skip] scripts\updater.py is missing - launching the version already on disk.
  )
)
echo.

REM Hand off to the real launcher (freshly re-read from disk after the update).
REM Guard a broken copy: without this the window just flash-closes with no
REM readable error if the inner script is missing.
if not exist "%~dp0scripts\bench-launch.bat" (
  echo scripts\bench-launch.bat is missing - this copy looks broken. Re-run the installer, then try again.
  pause
  exit /b 1
)
call "%~dp0scripts\bench-launch.bat"
