@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title BONSAI Optionals
color 0B

rem =============================================================================
rem  BONSAI - OPTIONAL extras.
rem
rem  Nothing here is needed to chat. These are the pieces that add Blender
rem  control, OCR, Docker tools and a few other system-level integrations. Some
rem  need administrator rights, some need a reboot, and some are a large
rem  download - so nothing runs unless you pick it.
rem
rem  Type a number to run one, several like "1 3", or "a" for everything.
rem  Can also be driven non-interactively:  OPTIONALS.cmd 2
rem =============================================================================

set "PYEXE="
for %%P in (python py) do (
    if not defined PYEXE (
        %%P --version >nul 2>&1
        if not errorlevel 1 set "PYEXE=%%P"
    )
)

rem Without this the script runs its way down into the first step and exits
rem having done only that, whatever you actually asked for.
goto :entry

:o_blender
echo.
echo == Blender control (mcp-for-blender) ==
if not defined PYEXE (
    echo   [X] Python not found - run AUTO-SETUP.cmd step 1 first.
    goto :after
)
%PYEXE% -m pip show mcp-for-blender >nul 2>&1
if not errorlevel 1 (
    echo   [ok] already installed
    goto :after
)
echo   Installs the MCP bridge so Bonsai can inspect and edit 3D scenes.
%PYEXE% -m pip install mcp-for-blender
if errorlevel 1 (
    echo   [!] pip install failed - check your internet connection.
) else (
    echo   [ok] installed.
    echo   Next: in Blender, Edit ^> Preferences ^> Add-ons, install "MCP for
    echo   Blender", and keep Blender open when you want scene tools.
)
goto :after

:o_ocr
echo.
echo == Tesseract OCR engine ==
where tesseract >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%v in ('tesseract --version 2^>^&1 ^| findstr /b "tesseract"') do set "TV=%%v"
    echo   [ok] already installed: !TV!
    goto :after
)
echo   Needed by click_text and screen_text (pytesseract is just the wrapper).
where winget >nul 2>&1
if errorlevel 1 (
    echo   [!] winget not found. Get Tesseract from
    echo       https://github.com/UB-Mannheim/tesseract/wiki
    goto :after
)
echo   Installing via winget - about 30 MB...
winget install --id UB-Mannheim.TesseractOCR -e --accept-package-agreements --accept-source-agreements --disable-interactivity
if errorlevel 1 (
    echo   [!] winget install failed - install Tesseract manually from the link above.
) else (
    echo   [ok] installed. Restart your terminal so PATH picks it up.
)
goto :after

:o_docker
echo.
echo == Docker Desktop (the docker_* tools) ==
docker --version >nul 2>&1
if not errorlevel 1 (
    docker version --format "  [ok] client {{.Client.Version}} / server {{.Server.Version}}" 2>nul
    if errorlevel 1 echo   [!] CLI is there but the engine is not running - start Docker Desktop.
    goto :after
)
echo   Not installed. This is a ~600 MB download and it needs WSL2 (option 4).
echo   Nothing else in Bonsai depends on it.
where winget >nul 2>&1
if errorlevel 1 (
    echo   [!] winget not found - install Docker Desktop from docker.com.
    goto :after
)
echo   Installing Docker Desktop - about 600 MB, this takes a while...
winget install --id Docker.DockerDesktop -e --accept-package-agreements --accept-source-agreements --disable-interactivity
if errorlevel 1 (
    echo   [!] winget install failed - install Docker Desktop manually.
) else (
    echo   [ok] installed.
    echo   A REBOOT is required. After rebooting, start Docker Desktop once so
    echo   it finishes setting up its engine, then the docker_* tools work.
)
goto :after

:o_wsl
echo.
echo == WSL2 + Virtual Machine Platform (Docker needs this) ==
powershell -NoProfile -Command "exit ([int]((Get-CimInstance Win32_Processor).VirtualizationFirmwareEnabled -ne $true))"
if errorlevel 1 (
    echo   [X] Virtualization is DISABLED in your firmware.
    echo       Turn on "SVM Mode" / "Intel VT-x" in the BIOS, then re-run.
    goto :after
)
echo   [ok] virtualization is enabled in firmware.
echo   Enabling the Virtual Machine Platform feature - accept the UAC prompt...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -All ^| Out-Null'"
echo   Installing WSL - accept the UAC prompt...
powershell -NoProfile -Command "Start-Process wsl.exe -Verb RunAs -Wait -ArgumentList '--install','--no-distribution'"
echo   [ok] done. A REBOOT is required before this takes effect.
goto :after

:o_stability
echo.
echo == Windows extras for stability (long paths, virtual memory) ==
echo   Long paths: lets tools use paths over the old 260 character limit.
reg query "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled >nul 2>&1
if not errorlevel 1 (
    echo   [ok] long paths already enabled
) else (
    echo   Enabling long path support...
    reg add "HKLM\SYSTEM\CurrentControlSet\Control\FileSystem" /v LongPathsEnabled /t REG_DWORD /d 1 /f >nul 2>&1 \
        && echo   [ok] enabled - a reboot or sign-out is needed \
        || echo   [!] could not write the registry - try as Administrator.
)
echo.
echo   Making the page file large enough for a 7 GB model (admin)...
powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile','-Command','$c=Get-CimInstance Win32_ComputerSystem; if($c.AutomaticManagedPagework -ne $true){ Set-CimInstance -InputObject $c -Property @{AutomaticManagedPagework=$false}; $p=Get-CimInstance Win32_PageFileSetting; if($p){ Set-CimInstance -InputObject $p -Property @{InitialSize=49152;MaximumSize=98304} } else { Set-CimInstance -InputObject $c -Property @{AutomaticManagedPagework=$false} | Out-Null; New-CimInstance -ClassName Win32_PageFileSetting -Property @{Name=\"C:\\\\pagefile.sys\";InitialSize=49152;MaximumSize=98304} | Out-Null }; Write-Host \"  [ok] page file set to 48-96 GB\"'"
goto :after

:o_images
echo.
echo == Local image generation (Qwen-Image 2.1) ==
echo   Weights are ~13 GB, fetched by a script rather than committed.
if exist "image_models\diffusion\*.gguf" (
    echo   [ok] diffusion weights already present
    goto :after
)
if not exist "tools\fetch_image_model.py" (
    echo   [!] tools\fetch_image_model.py not found - fetch the repo again.
    goto :after
)
set "q="
set /p "q=  Download them now? About 13 GB. [y/N]: "
if /i not "%q%"=="y" goto :after
python tools\fetch_image_model.py
if errorlevel 1 (
    echo   [!] download failed - run it again when you have a better connection.
) else (
    echo   [ok] done.
    echo   Note: at 1920x1088 the engine needs the whole card. Close anything
    echo   else holding VRAM - a game alone will use ~11 GB.
)
goto :after

:o_browser
echo.
echo == Browser engine for rendering web pages ==
echo   Tools that screenshot a web page need a real browser (Playwright).
%PYEXE% -c "import playwright" >nul 2>&1
if not errorlevel 1 (
    echo   [ok] playwright is installed
) else (
    echo   [--] not installed
    where winget >nul 2>&1 && echo   Install it with: pip install playwright ^&^& playwright install chromium
)
goto :after

rem -----------------------------------------------------------------------------

:entry
rem Called with arguments means "just run these, no menu":  OPTIONALS.cmd 2 4
if not "%~1"=="" (
    for %%S in (%*) do call :dispatch %%S
    goto :done
)
goto :menu

:dispatch
if "%~1"=="1" goto :o_blender
if "%~1"=="2" goto :o_ocr
if "%~1"=="3" goto :o_docker
if "%~1"=="4" goto :o_wsl
if "%~1"=="5" goto :o_stability
if "%~1"=="6" goto :o_images
if "%~1"=="7" goto :o_browser
echo   Unknown item "%~1".
goto :eof

:after
if "%~1"=="" (
    echo.
    set /p "dummy=  Press Enter to go back to the menu..."
)
goto :eof

:menu
cls
echo.
echo =============================================================================
echo   BONSAI  -  OPTIONAL extras  ^(none of this is required to chat^)
echo =============================================================================
echo.
echo     1  Blender control             mcp-for-blender        ~small
echo     2  Tesseract OCR              click_text/screen_text ~30 MB
echo     3  Docker Desktop             docker_* tools         ~600 MB ^(needs 4^)
echo     4  WSL2 + VM Platform         Docker's backend       reboot
echo     5  Windows stability          long paths, page file  admin
echo     6  Local image generation     Qwen-Image 2.1         ~13 GB
echo     7  Browser engine             page screenshots       small
echo.
echo     a  Run everything that is missing
echo     x  Quit
echo.
set "pick="
set /p "pick=  Choose (numbers can be spaced, e.g. 1 2): "
if not defined pick goto :menu
for /f "tokens=* delims=" %%p in ("!pick!") do (
    set "pick=%%p"
    goto :have_pick
)
:have_pick
if not defined pick goto :menu
echo(%pick%| findstr /r /c:"^[0-9aAxX ]*$" >nul
if errorlevel 1 (
    echo   That is not one of the choices.
    goto :menu
)
if /i "%pick%"=="x" goto :done
set "ITEMS=%pick%"
if /i "%pick%"=="a" set "ITEMS=1 2 4 5 7"
for %%S in (%ITEMS%) do call :dispatch %%S
goto :menu

:done
echo.
echo   Finished. Nothing above is required - AUTO-SETUP.cmd covers the rest.
echo.
pause
endlocal