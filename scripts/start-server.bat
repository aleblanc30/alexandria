@echo off
cd /d "%LOCALAPPDATA%\Alexandria\app"
call .venv\Scripts\activate.bat
echo [%date% %time%] starting cwd=%CD% >> "%LOCALAPPDATA%\Alexandria\data\server.log"
uvicorn pka.api.main:app --host 127.0.0.1 --port 8420 >> "%LOCALAPPDATA%\Alexandria\data\server.log" 2>&1