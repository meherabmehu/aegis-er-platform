@echo off
chcp 65001 >nul
REM AEGIS-ER quick-start for Windows (double-click to run)
REM Automatically navigates to the folder containing this script

cd /d "%~dp0"
echo.
echo ============================================
echo   AEGIS-ER - Emergency Response Platform
echo ============================================
echo.
echo Working directory: %cd%
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org (check "Add Python to PATH") and try again.
    pause
    exit /b 1
)

echo [1/4] Setting up virtual environment...
if not exist ".venv" (
    python -m venv .venv
)
call .venv\Scripts\activate.bat

echo [2/4] Installing dependencies...
python -m pip install -q --upgrade pip
python -m pip install -q -e libs\aegis[server]
python -m pip install -q -r services\assignment-solver\requirements.txt

echo [3/4] Launching server...
set PYTHONPATH=libs/aegis
set AEGIS_SIMULATOR=false
set AEGIS_DASHBOARD_DIR=services/dashboard

echo.
echo [4/4] Opening browser in 5 seconds...
echo      Dashboard: http://localhost:8000/
echo      Press Ctrl+C to stop.
echo.
start "" cmd /c "timeout /t 5 /nobreak >nul && start http://localhost:8000/"
python services\assignment-solver\app.py

pause
