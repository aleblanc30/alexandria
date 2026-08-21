@echo off
cd /d "%LOCALAPPDATA%\Alexandria\app"
call .venv\Scripts\activate.bat
uvicorn pka.api.main:app --host 127.0.0.1 --port 8420