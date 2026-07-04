@echo off
REM ===========================================================================
REM  Kela Bench - master launcher  (STABLE WRAPPER - keep this file byte-stable)
REM
REM  Double-click this to start ALL bench configurators and open the dashboard.
REM
REM  WHY THIS FILE IS TINY (and must STAY tiny):
REM  cmd.exe reads a .bat line-by-line by byte OFFSET from disk. A script that
REM  `git pull`s ITSELF gets corrupted the instant the pull rewrites it -
REM  execution resumes at a stale offset inside the new bytes - and that happens
REM  on exactly the runs that carry an update (the ones you most need to work
REM  when rolling changes to N stations). So this entry point only does the pull,
REM  then hands off to bench-launch.bat, which is not open in cmd until AFTER the
REM  pull finishes and is therefore safe to change.
REM
REM  DO NOT add launch logic here. Put every change in bench-launch.bat, and do
REM  not let commits modify this file - if its byte length changes across a pull
REM  it can corrupt its own run. Set BENCH_NO_PULL=1 to freeze the on-disk version.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM ── Pull the latest tools before handing off (best-effort) ─────────────────
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

REM Hand off to the real launcher (freshly re-read from disk after the pull).
REM Guard a broken checkout: without this the window just flash-closes with no
REM readable error if the inner script is missing.
if not exist "%~dp0bench-launch.bat" (
  echo bench-launch.bat is missing - the checkout looks broken. Re-clone or run git pull, then try again.
  pause
  exit /b 1
)
call "%~dp0bench-launch.bat"
