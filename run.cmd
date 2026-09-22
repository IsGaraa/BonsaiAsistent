@echo off
setlocal
netstat -ano | findstr /C:":8080" | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo Bonsai is already running on http://127.0.0.1:8080
    start "" "http://127.0.0.1:8080"
    goto :eof
)
echo Starting Bonsai 2...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-server.ps1"