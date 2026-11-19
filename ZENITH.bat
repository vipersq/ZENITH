@echo off
rem ZENITH // Anomaly Watch - launcher (first run installs the libraries)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo [ZENITH] First run - creating the Python environment...
    where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
    if not exist ".venv\Scripts\python.exe" (
        echo [ZENITH] Python 3 was not found. Install it from python.org and tick "Add to PATH".
        pause
        exit /b 1
    )
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
)
rem install / update libraries when the requirement list changes (version marker)
if not exist ".venv\req_v3.ok" (
    echo [ZENITH] Installing / updating libraries...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || (pause & exit /b 1)
    echo ok> ".venv\req_v3.ok"
)
if /i "%~1"=="--noexec" exit /b 0
start "" ".venv\Scripts\pythonw.exe" main.py
