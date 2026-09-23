@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title BONSAI Optionals Installer

echo ============================================================
echo   BONSAI Optionals
echo   Installs SYSTEM-LEVEL optional features (needs internet,
echo   some steps need admin / a reboot).
echo ============================================================

echo.
echo [A] Python package for Blender control (mcp-for-blender)...
python --version >nul 2>&1
if errorlevel 1 ( echo   [X] Python not found - run AUTO-SETUP.cmd first. ) else (
    python -m pip install mcp-for-blender
    if errorlevel 1 echo   [!] pip install failed - check internet / pip.
)

echo.
echo [B] Tesseract OCR engine (for tools\ocr.py)...
where tesseract >nul 2>&1
if not errorlevel 1 (
    echo   tesseract is already installed: OK
) else (
    echo   Installing via winget...
    winget install --id UB-Mannheim.TesseractOCR -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 echo   [!] winget install failed - get Tesseract from https://github.com/UB-Mannheim/tesseract/wiki
)

echo.
echo [C] Docker Desktop (container tools)...
docker --version >nul 2>&1
if not errorlevel 1 (
    echo   docker CLI already present: OK
) else (
    echo   Installing Docker Desktop via winget - about 600 MB download...
    winget install --id Docker.DockerDesktop -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    if errorlevel 1 echo   [!] winget install failed - re-run or install Docker Desktop manually.
)

echo.
echo [D] WSL2 backend + Virtual Machine Platform (Docker engine needs this)...
powershell -NoProfile -Command "exit ([int]((Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled -ne $true))"
if errorlevel 1 (
    echo   [X] Virtualization is DISABLED in firmware - BIOS.
    echo       Enable "SVM Mode"/"VT-x" in your BIOS settings, then re-run this step.
) else (
    echo   Virtualization enabled in firmware: OK
)
echo   Enabling the Virtual Machine Platform Windows feature (accept the UAC prompt)...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All | Out-Null'"
echo   Installing WSL (accept the UAC prompt)...
powershell -NoProfile -Command "Start-Process wsl.exe -Verb RunAs -Wait -ArgumentList '--install','--no-distribution'"

echo.
echo ============================================================
echo   Optionals finished.
echo   - A REBOOT IS REQUIRED for WSL2/VirtualMachinePlatform.
echo   - After reboot, start Docker Desktop once - it sets up the
echo     engine, then every docker_* tool works.
echo   - For Blender: install the "MCP for Blender" addon in
echo     Blender - Edit ^> Preferences ^> Add-ons - and keep Blender
echo     open when you want BONSAI to edit 3D scenes.
echo ============================================================
pause