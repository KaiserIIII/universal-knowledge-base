@echo off
setlocal

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%\backend"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
    echo Creating venv...
    python -m venv venv
)

echo.
echo Step 1: Downloading BGE model so it is cached...
call venv\Scripts\python.exe -c "from sentence_transformers import SentenceTransformer; m=SentenceTransformer('BAAI/bge-small-zh-v1.5'); print('Model OK. Dim:', m.get_sentence_embedding_dimension())" --break-system-packages

echo.
echo Step 2: Installing pyinstaller...
call venv\Scripts\python.exe -m pip install -q pyinstaller --break-system-packages >nul 2>&1

echo.
echo Step 3: Building EXE (5-10 minutes, ~2-3GB output)...
echo Output: backend\dist\GenericKnowledgeBase.exe
echo.

call venv\Scripts\python.exe -m PyInstaller build.spec --clean --noconfirm

if %errorlevel% equ 0 (
    echo.
    echo [OK] Build successful!
    echo File: %SCRIPT_DIR%backend\dist\GenericKnowledgeBase.exe
    explorer /select,"%SCRIPT_DIR%backend\dist\GenericKnowledgeBase.exe"
) else (
    echo.
    echo [FAIL] Build failed. Check errors above.
)

pause
