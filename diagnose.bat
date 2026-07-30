@echo off
cd /d "%~dp0"
echo === Python Check ===
backend\venv\Scripts\python.exe --version 2>&1
echo.
echo === Import Check ===
cd backend
venv\Scripts\python.exe -c "from app.config import get_settings; s=get_settings(); print('config OK, db:', s.database_url)" 2>&1
venv\Scripts\python.exe -c "from app.database import engine; print('database OK')" 2>&1
venv\Scripts\python.exe -c "from app.models import Base; print('models OK')" 2>&1
venv\Scripts\python.exe -c "from app.schemas import WorkspaceCreate; print('schemas OK')" 2>&1
venv\Scripts\python.exe -c "from app.rag_engine import get_rag_engine; print('rag_engine OK')" 2>&1
echo.
echo === Data Dir Check ===
cd /d "%~dp0"
if exist "backend\app\data" (echo backend\app\data EXISTS) else (echo backend\app\data MISSING - creating... && mkdir "backend\app\data")
echo.
echo === Try Starting ===
cd backend
venv\Scripts\python.exe run.py 2>&1
pause
