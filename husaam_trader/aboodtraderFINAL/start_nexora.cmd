@echo off
cd /d "%~dp0"

set "UVICORN_NO_PREBIND=1"

echo.
echo ===== NEXORA =====
echo Folder: %CD%
echo Stop with Ctrl+C. Do not close while running.
echo.

py bot.py
echo.
echo Exit code: %ERRORLEVEL%
pause
