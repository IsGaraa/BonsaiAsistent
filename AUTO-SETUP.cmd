@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title BONSAI Auto-Setup

echo ============================================================
echo   BONSAI Auto-Setup
echo   Installs Python packages and verifies required files.
echo ============================================================

set "PYDETECTED=0"

echo.
echo [1/5] Python
python --version >nul 2>&1
if errorlevel 1 (
    echo   Python not found on PATH.
    echo   Trying to install Python 3.13 via winget...
    winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements --disable-interactivity >nul 2>&1
    python --version >nul 2>&1
    if errorlevel 1 (
        echo   [X] Could not get Python. Install Python 3.10+ from https://www.python.org then re-run.
        pause
        exit /b 1
    )
)
python --version

echo.
echo [2/5] Installing Python packages (recommended + optional)...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt -r tools\requirements.txt
if errorlevel 1 (
    echo   [X] pip install failed - see the error above.
    pause
    exit /b 1
)

echo.
echo [3/5] Verifying optional tool libraries...
python -c "import win32gui, psutil, pyautogui, PIL, pyperclip, requests, websocket; print('   screenshots, input, clipboard, windows, api, websockets: OK')" 2>nul
python -c "import pytesseract; print('   OCR helper: OK')" 2>nul
python -c "import piper, onnxruntime, sounddevice; print('   local neural TTS - piper + sounddevice: OK')" 2>nul
if exist "%~dp0piper\*.onnx" (
    echo   Piper voices: found in "%~dp0piper"
) else (
    echo   [!] No Piper voice models found - run: python piper\download_voices.py
)

echo.
echo [4/5] Checking required inference files...
set "LLAMA=%LOCALAPPDATA%\Programs\prism-llama\llama-server.exe"
if exist "%LLAMA%" (
    echo   llama-server.exe: OK
) else (
    echo   [!] llama-server.exe not found at "%LLAMA%"
    echo       Install the PrismML llama.cpp fork and make sure llama-server.exe
    echo       lands there - stock llama.cpp cannot run Bonsai 2 files.
)
if exist "Ternary-Bonsai-2-27B-PQ2_0.gguf" (
    echo   Text model GGUF: OK
) else (
    echo   [!] Missing Ternary-Bonsai-2-27B-PQ2_0.gguf in this folder.
    echo       Put the model file here - it is not bundled in the repo.
)
if exist "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf" (
    echo   Vision mmproj: OK
) else (
    echo   [!] Missing Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf in this folder.
)

echo.
echo [5/5] Optional Docker containers
docker --version >nul 2>&1
if not errorlevel 1 (
    docker version --format "   Docker: {{.Client.Version}} client / {{.Server.Version}} server" 2>nul
    if errorlevel 1 echo   Docker CLI present but engine not running - start Docker Desktop.
) else (
    echo   [!] docker not installed. Run OPTIONALS.cmd if you want the container tools.
)

echo.
echo ============================================================
echo   Setup check finished.
echo   - Any [X] lines above = blocking: fix them, then rerun this.
echo   - Any [!] lines above = optional component missing.
echo   - Everything OK? Just launch the assistant:  run.bat
echo ============================================================
pause