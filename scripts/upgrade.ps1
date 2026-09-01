<#
    Upgrades an Alexandria installation to a release tag: stops the server,
    backs up the library, checks out the tag, reinstalls, migrates, restarts.
    See INSTALL.md section 11.

        .\scripts\upgrade.ps1 v0.0.8        upgrade to that tag
        .\scripts\upgrade.ps1 v0.0.8 -Yes   the same, without the prompt
        .\scripts\upgrade.ps1               list the tags available

    The install to upgrade is the checkout this script is in, not a fixed path,
    so it acts on whichever tree it was run from. That tree is rewritten while
    the script runs, which is safe: PowerShell parses a script file in full
    before executing any of it, so the run continues with the version that was
    on disk when it started.
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $Tag,
    [switch] $Yes
)

$ErrorActionPreference = 'Continue'

# --- settings ---------------------------------------------------------------

# Edit $Port and $Task if the install does not use the default port or the task
# name from INSTALL.md section 7. The paths resolve themselves.
$Port = 8420
$Task = 'Alexandria'

if (-not $PSScriptRoot) {
    Write-Host 'ERROR: run this as a script file, not by pasting it into a prompt.' -ForegroundColor Red
    exit 1
}
$App = Split-Path -Parent $PSScriptRoot

# --- helpers ----------------------------------------------------------------

function Fail {
    param([string] $Message)
    Write-Host ''
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

function Step {
    param([int] $Number, [string] $Message)
    Write-Host ''
    Write-Host "[$Number/7] $Message" -ForegroundColor Cyan
}

# The library lives outside the checkout, at whatever ALEXANDRIA_DATA_DIR
# points to, so that an upgrade cannot disturb it. Read the same setting the
# server reads rather than assuming the default layout.
function Get-DataDir {
    param([string] $AppPath)
    $value = $env:ALEXANDRIA_DATA_DIR
    if (-not $value) {
        $envFile = Join-Path $AppPath '.env'
        if (Test-Path $envFile) {
            foreach ($line in (Get-Content $envFile)) {
                if ($line -match '^\s*ALEXANDRIA_DATA_DIR\s*=\s*(.+?)\s*$') {
                    $value = $matches[1].Trim().Trim('"').Trim("'")
                    break
                }
            }
        }
    }
    # Settings defaults data_dir to the relative path "data", which resolves
    # against the server's working directory, i.e. the checkout itself. The
    # sibling layout in INSTALL.md section 2 exists only because .env names it.
    if (-not $value) { $value = 'data' }
    if ([System.IO.Path]::IsPathRooted($value)) { return $value }
    return (Join-Path $AppPath $value)
}

# The scheduled task from INSTALL.md section 7 runs wscript.exe, which runs a
# batch file, which runs uvicorn. Ending the task kills the wrapper and leaves
# uvicorn holding the port, so the listener is found by port and killed
# directly. netstat is used rather than Get-NetTCPConnection because it needs
# no module and reports the owning PID in the same row.
function Get-ListenerPids {
    param([int] $Number)
    $ids = @()
    foreach ($row in (netstat -ano -p tcp)) {
        $fields = ($row.Trim() -split '\s+')
        if ($fields.Count -lt 5) { continue }
        if ($fields[3] -ne 'LISTENING') { continue }
        if ($fields[1] -notmatch ":$Number$") { continue }
        $ids += $fields[4]
    }
    return ($ids | Sort-Object -Unique)
}

function Stop-Listeners {
    param([int] $Number)
    foreach ($procId in (Get-ListenerPids $Number)) {
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Host "  stopping $($proc.ProcessName) (PID $procId)"
        } else {
            Write-Host "  stopping PID $procId"
        }
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 10; $i++) {
        if (-not (Get-ListenerPids $Number)) { return $true }
        Start-Sleep -Seconds 1
    }
    return (-not (Get-ListenerPids $Number))
}

function Test-TaskExists {
    param([string] $Name)
    $null = & schtasks.exe /Query /TN $Name 2>&1
    return ($LASTEXITCODE -eq 0)
}

# --- preflight --------------------------------------------------------------
#
# Everything that can be checked before the server is stopped is checked here,
# so that a failure costs nothing but a message.

$Data   = Get-DataDir $App
$Backup = "$Data-backup"
$python = Join-Path $App '.venv\Scripts\python.exe'

if (-not (Test-Path (Join-Path $App '.git'))) { Fail "`"$App`" is not a git checkout." }
if (-not (Test-Path $python))                 { Fail "no virtual environment at `"$App\.venv`". See INSTALL.md section 3." }
if (-not (Test-Path $Data))                   { Fail "no library at `"$Data`". Check ALEXANDRIA_DATA_DIR in `"$App\.env`"." }

Push-Location $App
try {

    Write-Host 'Fetching tags...'
    & git fetch --tags --quiet
    if ($LASTEXITCODE -ne 0) { Fail 'git fetch --tags failed. Check the network and the remote.' }

    if (-not $Tag) {
        Write-Host ''
        Write-Host 'Usage: .\scripts\upgrade.ps1 <tag> [-Yes]'
        Write-Host ''
        Write-Host 'Tags available:'
        & git tag --list --sort=-v:refname
        Write-Host ''
        Write-Host -NoNewline 'Currently checked out: '
        & git describe --tags --always
        exit 1
    }

    $null = & git rev-parse --verify --quiet "refs/tags/$Tag"
    if ($LASTEXITCODE -ne 0) {
        Fail "no tag `"$Tag`". Run this script with no arguments to list them."
    }

    # A modified working tree makes the checkout fail halfway, after the server
    # has already been stopped. Refuse before touching anything.
    & git diff --quiet HEAD
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host "The working tree at `"$App`" has local modifications:"
        & git status --short --untracked-files=no
        Fail 'discard or commit them, then run again. Nothing has been changed.'
    }

    Write-Host ''
    Write-Host "  tag       $Tag"
    Write-Host "  app       $App"
    Write-Host "  library   $Data"
    Write-Host "  backup    $Backup   (mirrored: contents replaced)"
    Write-Host ''
    if (-not $Yes) {
        $answer = Read-Host 'Proceed? [y/N]'
        if ($answer -ne 'y') { Fail 'aborted. Nothing has been changed.' }
    }

    # --- 1. stop the server -------------------------------------------------

    Step 1 'Stopping the server...'
    if (Test-TaskExists $Task) {
        Write-Host "  ending scheduled task `"$Task`""
        $null = & schtasks.exe /End /TN $Task 2>&1
    }
    if (-not (Get-ListenerPids $Port)) {
        Write-Host "  nothing is listening on port $Port"
    } elseif (-not (Stop-Listeners $Port)) {
        Fail ("port $Port is still held after the kill. Backing up the library " +
              'while it is being written gives an inconsistent copy, so the ' +
              'upgrade stopped here. Nothing has been changed.')
    }

    # --- 2. back up the library ---------------------------------------------

    Step 2 "Backing up the library to `"$Backup`"..."
    & robocopy.exe $Data $Backup /MIR /NFL /NDL /NJH /NJS /NP /R:2 /W:2
    if ($LASTEXITCODE -ge 8) {
        Fail ("robocopy failed (exit $LASTEXITCODE). Nothing else has been changed. " +
              "The server is stopped; restart it with: schtasks /Run /TN $Task")
    }

    # --- 3. check out the tag -----------------------------------------------

    Step 3 "Checking out $Tag..."
    & git checkout --quiet $Tag
    if ($LASTEXITCODE -ne 0) {
        Fail "git checkout $Tag failed. The install is unchanged apart from being stopped, and the backup is current."
    }

    # --- 4. reinstall the package -------------------------------------------

    Step 4 'Installing the package...'
    & $python -m pip install --disable-pip-version-check .
    if ($LASTEXITCODE -ne 0) {
        Fail 'pip install . failed. The tag is checked out but not installed; fix the error and run this script again with the same tag.'
    }

    # --- 5. rebuild the frontend --------------------------------------------

    Step 5 'Building the frontend...'
    Push-Location (Join-Path $App 'frontend')
    try {
        & npm.cmd ci
        if ($LASTEXITCODE -ne 0) { Fail 'npm ci failed. The backend is on the new tag but the interface is the previous release. Fix the error and run again.' }
        & npm.cmd run build
        if ($LASTEXITCODE -ne 0) { Fail 'npm run build failed. The backend is on the new tag but the interface is the previous release. Fix the error and run again.' }
    } finally {
        Pop-Location
    }
    if (-not (Test-Path (Join-Path $App 'frontend\dist\index.html'))) {
        Fail 'the build left no frontend\dist\index.html. See INSTALL.md section 9.'
    }

    # --- 6. migrate the database --------------------------------------------

    Step 6 'Migrating the database...'
    & (Join-Path $App '.venv\Scripts\alexandria.exe') init
    if ($LASTEXITCODE -ne 0) {
        Fail 'alexandria init failed. The database was not migrated; do not start the server until this is resolved. See INSTALL.md section 11.'
    }

    # --- 7. start the server ------------------------------------------------

    Step 7 'Starting the server...'
    if (Test-TaskExists $Task) {
        & schtasks.exe /Run /TN $Task
        if ($LASTEXITCODE -ne 0) { Fail "schtasks /Run /TN $Task failed." }
    } else {
        $vbs = Join-Path $App 'start-hidden.vbs'
        if (-not (Test-Path $vbs)) { Fail "no scheduled task `"$Task`" and no `"$vbs`". See INSTALL.md section 6." }
        Write-Host "  no scheduled task `"$Task`"; starting directly"
        Start-Process -FilePath "$env:SystemRoot\System32\wscript.exe" -ArgumentList "`"$vbs`""
    }

    Write-Host "  waiting for http://localhost:$Port ..."
    $deadline = (Get-Date).AddSeconds(180)
    $up = $false
    while ((Get-Date) -lt $deadline) {
        try {
            $null = Invoke-WebRequest -Uri "http://localhost:$Port/docs" -UseBasicParsing -TimeoutSec 5
            $up = $true
            break
        } catch {
            Start-Sleep -Seconds 3
        }
    }
    if (-not $up) {
        Fail "the server did not answer on port $Port within 180 seconds. Check the log: `"$Data\server.log`""
    }

    Write-Host ''
    Write-Host -NoNewline 'Now running: '
    & git describe --tags --always
    Write-Host "Upgrade complete. Open http://localhost:$Port" -ForegroundColor Green
    Write-Host "The previous library is at `"$Backup`"."

} finally {
    Pop-Location
}
