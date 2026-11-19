@echo off
rem Checks that every data source responds from this computer
cd /d "%~dp0"
call ZENITH.bat --noexec || exit /b 1
".venv\Scripts\python.exe" -m zenith.selftest
