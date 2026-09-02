<#
    Runs every check in CLAUDE.md's "Verifying a change" table in one pass:
    ruff lint, ruff format check, mypy, pytest with coverage, and the frontend
    test + build. Not wired into CI yet (planning/TODO.md M-12) — this is the
    manual all-in-one a pre-push hook or a future workflow would call.

        .\scripts\check.ps1

    Every step runs even if an earlier one fails, so one pass reports
    everything that's broken instead of stopping at the first. Exits 1 if any
    step failed, 0 if all passed. Uses the repo's own .venv directly rather
    than relying on it being activated, so this also works from a shell that
    hasn't sourced it.
#>

$ErrorActionPreference = 'Continue'

if (-not $PSScriptRoot) {
    Write-Host 'ERROR: run this as a script file, not by pasting it into a prompt.' -ForegroundColor Red
    exit 1
}
$App    = Split-Path -Parent $PSScriptRoot
$python = Join-Path $App '.venv\Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host "ERROR: no virtual environment at `"$App\.venv`". See README.md." -ForegroundColor Red
    exit 1
}

# npm.cmd, not npm: shutil.which("npm")-style resolution can land on the
# extensionless POSIX shim, which CreateProcess can't launch directly.
$npm = 'npm.cmd'
if (-not (Get-Command $npm -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: npm not found on PATH." -ForegroundColor Red
    exit 1
}

$steps = @(
    @{ Name = 'ruff check';        Dir = $App;                    Cmd = { & $python -m ruff check pka tests scripts } }
    @{ Name = 'ruff format check'; Dir = $App;                    Cmd = { & $python -m ruff format --check pka tests scripts } }
    @{ Name = 'mypy';              Dir = $App;                    Cmd = { & $python -m mypy pka } }
    @{ Name = 'pytest --cov';      Dir = $App;                    Cmd = { & $python -m pytest --cov=pka --cov-report=term-missing } }
    @{ Name = 'npm run test';      Dir = (Join-Path $App 'frontend'); Cmd = { & $npm run test } }
    @{ Name = 'npm run build';     Dir = (Join-Path $App 'frontend'); Cmd = { & $npm run build } }
)

$results = @()
foreach ($step in $steps) {
    Write-Host ''
    Write-Host "-- $($step.Name) --" -ForegroundColor Cyan
    Push-Location $step.Dir
    try {
        & $step.Cmd
        $ok = ($LASTEXITCODE -eq 0)
    } finally {
        Pop-Location
    }
    $results += [pscustomobject]@{ Name = $step.Name; Ok = $ok }
}

Write-Host ''
Write-Host '-- Summary --' -ForegroundColor Cyan
$failed = 0
foreach ($r in $results) {
    if ($r.Ok) {
        Write-Host "  PASS  $($r.Name)" -ForegroundColor Green
    } else {
        Write-Host "  FAIL  $($r.Name)" -ForegroundColor Red
        $failed++
    }
}
Write-Host ''

if ($failed -gt 0) {
    Write-Host "$failed of $($results.Count) checks failed." -ForegroundColor Red
    exit 1
}
Write-Host "All $($results.Count) checks passed." -ForegroundColor Green
exit 0
