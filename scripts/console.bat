@echo off
rem Alexandria console: follows the server log written by start-server.bat.
rem Read-only -- closing this window does not stop the server.
rem Edit LOG if ALEXANDRIA_DATA_DIR points somewhere other than the default.
title Alexandria console
set "LOG=%LOCALAPPDATA%\Alexandria\data\server.log"
echo Following "%LOG%"
echo Press Ctrl+C to close. The server keeps running.
echo.
if not exist "%LOG%" echo No log yet -- waiting for the server's first start...
powershell -NoProfile -Command "while (-not (Test-Path -LiteralPath $env:LOG)) { Start-Sleep -Seconds 1 }; Get-Content -LiteralPath $env:LOG -Tail 200 -Wait"
