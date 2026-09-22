@echo off
cd /d "%~dp0"

echo Starting BONSAI - PC Assistant (Bonsai 2 27B + vision)...
echo The Bonsai 2 model server starts automatically if it is not running.
echo UI opens in your browser at http://localhost:8081 - press Ctrl+C to stop.
echo.
python bonsai_web.py