# ============================================================================
#  Kela Bench - station installer (Windows)
# ----------------------------------------------------------------------------
#  One-shot setup of a fresh bench PC from bench-central. The PC must already
#  reach bench-central (tailnet or LAN). Run from a PowerShell window:
#
#    irm http://techops-automations-host:8100/setup.ps1 -OutFile setup.ps1
#    .\setup.ps1 -CentralUrl http://techops-automations-host:8100
#
#  Optional:
#    -StationId bench-3            (default: $env:BENCH_STATION_ID, else this
#                                   PC's hostname)
#    -InstallDir C:\kela-bench     (default: %USERPROFILE%\kela-bench)
#
#  What it does (idempotent - safe to re-run over an existing install):
#    1. finds/installs Python 3.11+ (winget)
#    2. downloads the release pinned on bench-central and extracts it
#    3. writes .bench-station.json (station id + central URL) and seeds each
#       tool's config from its committed *.example.* template
#    4. creates the shared .venv + installs requirements
#    5. puts a "Kela Bench Tools" shortcut on the desktop
#    6. verifies, checks the station in (it appears on the Fleet page), and
#       offers to launch
#
#  From then on every launch of "Start Bench Tools.bat" converges the station
#  to whatever release is pinned on bench-central (updater.py).
# ============================================================================
param(
    [string]$CentralUrl = $env:BENCH_CENTRAL_URL,
    # Same precedence the rest of the stack uses for the station identity:
    # explicit flag > BENCH_STATION_ID in the environment > the hostname.
    [string]$StationId = $(if ($env:BENCH_STATION_ID) { $env:BENCH_STATION_ID }
                           else { $env:COMPUTERNAME }),
    [string]$InstallDir = (Join-Path $env:USERPROFILE "kela-bench")
)

$ErrorActionPreference = "Stop"

function Fail($msg) { Write-Host "ERROR: $msg" -ForegroundColor Red; exit 1 }
function Step($n, $msg) { Write-Host "==> [$n/6] $msg" -ForegroundColor Cyan }

if (-not $CentralUrl) {
    $CentralUrl = Read-Host "bench-central URL (e.g. http://techops-automations-host:8100)"
}
if (-not $CentralUrl) { Fail "a bench-central URL is required" }
$CentralUrl = $CentralUrl.TrimEnd("/")

Write-Host "Kela Bench station installer"
Write-Host "  central:  $CentralUrl"
Write-Host "  station:  $StationId"
Write-Host "  into:     $InstallDir"
Write-Host ""

# ---- 1. bench-central reachable + a release pinned -------------------------
Step 1 "Checking bench-central"
try {
    $health = Invoke-RestMethod "$CentralUrl/api/v1/health" -TimeoutSec 10
} catch {
    Fail "bench-central is not reachable at $CentralUrl ($($_.Exception.Message)). Is this PC on the tailnet?"
}
$desired = Invoke-RestMethod "$CentralUrl/api/v1/fleet/desired" -TimeoutSec 10
if (-not $desired.version) {
    Fail "no release is pinned on bench-central yet - pin one on the dashboard ($CentralUrl) first."
}
$sha = $desired.version
Write-Host "    collector $($health.version), pinned release $($sha.Substring(0, 12))"

# ---- 2. Python 3.11+ --------------------------------------------------------
Step 2 "Locating Python 3.11+"
# Returns @{Exe=...; Args=@(...)} for a Python >= 3.10, or $null. An empty
# Args array flattens to nothing when calling a native exe, so callers can
# always write: & $py.Exe $py.Args <more args>.
function Find-Python {
    foreach ($candidate in @(@{Exe = "py"; Args = @("-3")}, @{Exe = "python"; Args = @()})) {
        if (-not (Get-Command $candidate.Exe -ErrorAction SilentlyContinue)) { continue }
        & $candidate.Exe $candidate.Args -c "import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)" 2>$null | Out-Null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    # Why the automatic install didn't produce a usable Python. Without it the
    # only symptom of a blocked/absent winget source, a "no applicable
    # installer" HRESULT or an interrupted download is the generic "install it
    # by hand" message below, which sends the operator looking in the wrong place.
    $reason = "winget is not available on this PC"
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Host "    Python not found - installing via winget (this can take a few minutes)..."
        $reason = $null
        try {
            winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -ne 0) {
                $reason = "winget exited with 0x$('{0:X8}' -f $LASTEXITCODE)"
            }
        } catch {
            $reason = "winget failed: $($_.Exception.Message)"
        }
        if (-not $reason) {
            # winget updates PATH for NEW shells; pick up the install for this one.
            $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                        [Environment]::GetEnvironmentVariable("Path", "User")
            $py = Find-Python
            if (-not $py) { $reason = "winget reported success but no Python is on PATH" }
        }
    }
    if (-not $py) {
        Fail "Python 3.11+ is required and could not be installed automatically ($reason). Install it from https://www.python.org/downloads/ (tick `"Add python.exe to PATH`") and re-run this script."
    }
}
Write-Host "    using: $($py.Exe) $($py.Args -join ' ')"

# ---- 3. Download + extract the pinned bundle --------------------------------
Step 3 "Downloading the pinned bench release"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$zip = Join-Path $env:TEMP "bench-$($sha.Substring(0, 12)).zip"
Invoke-WebRequest "$CentralUrl/api/v1/fleet/bundle/$sha.zip" -OutFile $zip -TimeoutSec 300
Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
Remove-Item $zip -ErrorAction SilentlyContinue
Write-Host "    extracted to $InstallDir"

# ---- 4. Station identity + config seeding -----------------------------------
Step 4 "Writing station identity + seeding configs"
# BOM-less UTF-8 on purpose: Windows PowerShell 5.1's `Set-Content -Encoding
# UTF8` prepends a BOM, which json.loads() in updater.py rejects — the station
# file would exist but read as empty, silently disabling updates/check-ins.
$stationJson = @{ station_id = $StationId; central_url = $CentralUrl } | ConvertTo-Json
[System.IO.File]::WriteAllText((Join-Path $InstallDir ".bench-station.json"),
                               $stationJson, [System.Text.UTF8Encoding]::new($false))
Push-Location $InstallDir
try {
    & $py.Exe $py.Args scripts\updater.py --seed-configs
} finally { Pop-Location }

# ---- 5. Shared venv + dependencies + desktop shortcut ------------------------
Step 5 "Creating the shared .venv + desktop shortcut"
Push-Location $InstallDir
try {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        & $py.Exe $py.Args -m venv .venv
        if ($LASTEXITCODE -ne 0) { Fail "could not create the virtual environment" }
        & ".venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
    }
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-Host "    [warn] dependency install failed - the launcher retries on every run." -ForegroundColor Yellow
    }
} finally { Pop-Location }

$shortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "Kela Bench Tools.lnk"
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut($shortcut)
$link.TargetPath = Join-Path $InstallDir "Start Bench Tools.bat"
$link.WorkingDirectory = $InstallDir
$link.Description = "Start all Kela bench provisioning tools"
$link.Save()
Write-Host "    shortcut: $shortcut"

# ---- 6. Verify + first check-in ----------------------------------------------
Step 6 "Verifying"
$failures = 0
function Check($desc, $ok) {
    if ($ok) { Write-Host "PASS  $desc" }
    else { Write-Host "FAIL  $desc" -ForegroundColor Red; $script:failures++ }
}
$stamp = Join-Path $InstallDir ".bench-build.json"
Check "bundle stamp present (.bench-build.json)" (Test-Path $stamp)
if (Test-Path $stamp) {
    Check "installed version matches the pin" ((Get-Content $stamp -Raw | ConvertFrom-Json).version -eq $sha)
}
# Read the station file back through updater.py — the exact path the
# launchers use — so "written but unreadable" fails here, not silently later.
Push-Location $InstallDir
try {
    $pinnedUrl = (& $py.Exe $py.Args scripts\updater.py --print-station central_url 2>$null | Out-String).Trim()
} finally { Pop-Location }
Check "station file readable (central_url pinned)" ($pinnedUrl -eq $CentralUrl)
Check "launcher present (Start Bench Tools.bat)" (Test-Path (Join-Path $InstallDir "Start Bench Tools.bat"))
Check "shared .venv usable" (Test-Path (Join-Path $InstallDir ".venv\Scripts\python.exe"))
try {
    $checkin = Invoke-RestMethod -Method Post -ContentType "application/json" -TimeoutSec 10 `
        -Uri "$CentralUrl/api/v1/fleet/checkin" `
        -Body (@{ station_id = $StationId; hostname = $env:COMPUTERNAME
                  platform = "Windows"; version = $sha } | ConvertTo-Json)
    Check "checked in with bench-central (see the Fleet page)" ($checkin.ok -eq $true)
} catch {
    Check "checked in with bench-central (see the Fleet page)" $false
}
if ($failures -gt 0) { Fail "$failures check(s) failed - fix the above and re-run this script." }

Write-Host ""
Write-Host "Station '$StationId' is installed." -ForegroundColor Green
Write-Host "Launch anytime via the desktop shortcut 'Kela Bench Tools'."
$answer = Read-Host "Launch the bench tools now? [Y/n]"
if ($answer -eq "" -or $answer -match "^[Yy]") {
    Start-Process -FilePath (Join-Path $InstallDir "Start Bench Tools.bat") -WorkingDirectory $InstallDir
}
