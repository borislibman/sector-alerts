@echo off
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python sector_alert.py >> logs\sector_alert.log 2>&1
exit /b %errorlevel%