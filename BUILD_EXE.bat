@echo off
rem Builds a standalone ZENITH.exe (folder dist\ZENITH)
cd /d "%~dp0"
call ZENITH.bat --noexec || exit /b 1
".venv\Scripts\python.exe" -m pip install pyinstaller
".venv\Scripts\pyinstaller.exe" --noconfirm --windowed --name ZENITH --icon zenith\data\zenith.ico ^
    --add-data "zenith\data;zenith\data" ^
    --exclude-module PySide6.QtWebEngineCore --exclude-module PySide6.QtWebEngineWidgets ^
    --exclude-module PySide6.QtQuick --exclude-module PySide6.QtQml --exclude-module PySide6.Qt3DCore ^
    --exclude-module curl_cffi ^
    main.py
echo.
echo Done: dist\ZENITH\ZENITH.exe
pause
