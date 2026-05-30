@echo off
chcp 65001 >nul 2>&1
title YouTube Downloader
echo.
echo  ================================
echo   YouTube Downloader
echo  ================================
echo.

cd /d "%~dp0"

:: Check dependencies
pip show flask >nul 2>&1 || pip install -r requirements.txt
pip show PySide6 >nul 2>&1 || pip install -r requirements.txt

:: Kill existing process on port 5002
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5002" ^| findstr "LISTEN"') do (
    taskkill /pid %%a /f >nul 2>&1
)

python app.py
