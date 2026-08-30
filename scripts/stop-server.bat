@echo off
rem Stops Alexandria however it was started: ends the scheduled task from
rem INSTALL.md section 7 if it exists, then stops whatever still listens on the
rem API port. Edit PORT if the server was moved off 8420.
title Stop Alexandria
set "PORT=8420"

schtasks /Query /TN Alexandria >nul 2>&1
if not errorlevel 1 (
    echo Ending scheduled task "Alexandria"...
    schtasks /End /TN Alexandria >nul 2>&1
)

powershell -NoProfile -Command "$ids = @(Get-NetTCPConnection -LocalPort $env:PORT -State Listen -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique); if (-not $ids) { Write-Host ('Nothing is listening on port ' + $env:PORT + '.') } else { foreach ($id in $ids) { $p = Get-Process -Id $id -ErrorAction SilentlyContinue; Write-Host ('Stopping ' + $(if ($p) { $p.ProcessName } else { 'process' }) + ' (PID ' + $id + ')'); Stop-Process -Id $id -Force -ErrorAction SilentlyContinue } }; Start-Sleep -Seconds 3"
