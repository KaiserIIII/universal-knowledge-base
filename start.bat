@echo off
setlocal enabledelayedexpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

echo.
echo   ================================================
echo     Generic Knowledge Base v2.1 - Embedded Edition
echo   ================================================
echo.

echo [1/4] Checking Python...
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Install Python 3.10+
    exit /b 1
)
python --version

echo.
echo [2/4] Setting up virtual environment...
if not exist "backend\venv\Scripts\python.exe" (
    echo        Creating venv...
    python -m venv backend\venv
)

echo        Installing packages...
call backend\venv\Scripts\python.exe -m pip install -q --upgrade pip --break-system-packages >nul 2>&1
call backend\venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt --break-system-packages >nul 2>&1

echo.
echo [3/4] Checking configuration...
if not exist "backend\.env" (
    copy "backend\.env.template" "backend\.env" >nul 2>&1
    echo        Default configuration created. Configure it in the Web UI.
)

echo.
echo [4/4] Starting Knowledge Base...

if not exist "backend\app\data" mkdir "backend\app\data" 2>nul

:: Start backend with output visible in a window that stays open
start "GenericKnowledgeBase Backend" /D "%SCRIPT_DIR%backend" cmd /c "venv\Scripts\python.exe run.py"

echo        Waiting for backend (polling /health)...
set /a TRIES=0
:waitloop
timeout /t 2 /nobreak >nul
set /a TRIES+=1
call backend\venv\Scripts\python.exe -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=2)" >nul 2>&1
if %errorlevel% equ 0 goto ready
if %TRIES% lss 90 goto waitloop
echo        [WARN] Backend not responding after 180s, opening UI anyway...

:ready
echo        Backend is ready.

echo        Opening frontend UI...
if exist "%SCRIPT_DIR%index.html" (
    start "" "http://localhost:8000"
) else (
    echo        [WARN] index.html not found, please open: %SCRIPT_DIR%index.html
)

echo        All settings are available in the Web UI.
echo        To stop    : run stop.bat
exit /b 0
