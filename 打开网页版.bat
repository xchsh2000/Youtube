@echo off
chcp 65001 >nul 2>&1
cd /d "%~dp0"

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5002" ^| findstr "LISTEN"') do (
    taskkill /pid %%a /f >nul 2>&1
)

C:\Python314\python.exe open_web.py > downloads\open_web.log 2>&1
