@echo off
rem Runs ZENITH with a console window - shows every message and error
cd /d "%~dp0"
call ZENITH.bat --noexec || exit /b 1
".venv\Scripts\python.exe" main.py
echo.
echo ===== ZENITH exited (code %errorlevel%) =====
pause
