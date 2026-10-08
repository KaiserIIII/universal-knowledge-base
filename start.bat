@echo off
cd /d "%~dp0"
python start.py
exit /b %errorlevel%
