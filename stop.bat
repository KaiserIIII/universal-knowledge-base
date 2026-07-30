@echo off
echo Stopping Generic Knowledge Base...
taskkill /FI "WINDOWTITLE eq GenericKnowledgeBase*" /F 2>nul
echo [OK] Stopped
timeout /t 2 /nobreak >nul
