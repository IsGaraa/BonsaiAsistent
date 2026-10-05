@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title BONSAI Setup
color 0B

rem =============================================================================
rem  BONSAI - interactive setup.
rem
rem  Every step is optional and nothing runs without you choosing it. Type the
rem  number of a step and press Enter to run just that one; type several like
rem  "1 3 4" to queue them; "a" runs everything recommended. Anything that is
rem  already done says so instead of redoing it.
rem
rem  This is Windows. On Linux use ./AUTO-SETUP.sh, which has the same menu.
rem =============================================================================

set "OK=  [ok]"
set "NO=  [--]"
set "GO=-> "

set "PYEXE="
set "STAGEPICK="
set "CANRUN=0"

:find_python
for %%P in (python py) do (
    if not defined PYEXE (
        %%P --version >nul 2>&1
        if not errorlevel 1 set "PYEXE=%%P"
    )
)
if defined PYEXE goto :got_python
echo   Python was not found on your PATH.
echo.
echo   [1] Install Python 3.13 with winget
echo   [2] Tell me where Python already is
echo   [b] Back to the menu
set "p1="
set /p "p1=  Choose [1/2/b]: "
if /i "%p1%"=="1" goto :install_python
if /i "%p1%"=="2" goto :ask_python
goto :menu

:install_python
echo   Installing Python 3.13 via winget...
winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements --disable-interactivity
echo.
echo   Python is installed, but this window was opened before it existed.
echo   Close this, open a NEW terminal, and run AUTO-SETUP again.
goto :done

:ask_python
set "ppath="
set /p "ppath=  Full path to python.exe (e.g. C:\Python313\python.exe): "
if not exist "%ppath%" (
    echo   No file at that path.
    goto :find_python
)
set "PYEXE=%ppath%"
:got_python
set "CANRUN=1"
rem Finding Python must not fall through into the step definitions below, or
rem the script runs step 1 and exits without ever showing the menu.

rem Given arguments mean "run these steps and stop" - no menu, no prompts.
rem   AUTO-SETUP.cmd 5        just check the model files
rem   AUTO-SETUP.cmd 2 3      packages, then desktop extras
rem   AUTO-SETUP.cmd a        everything recommended
if not "%~1"=="" (
    set "STAGEPICK=%~1"
    if /i not "!STAGEPICK:~0,1!"=="a" set "STAGEPICK=%*"
    goto :runqueue
)
goto :menu

rem -----------------------------------------------------------------------------
rem  the individual steps
rem -----------------------------------------------------------------------------

:s_python
echo.
echo == Python ==
%PYEXE% --version
%PYEXE% -m pip --version
echo   %OK% Python %PYEXE% works and pip is available.
goto :after

:s_packages
echo.
echo == Python packages (from requirements.txt + tools\requirements.txt) ==
%PYEXE% -m pip install --upgrade pip
%PYEXE% -m pip install -r requirements.txt -r tools\requirements.txt
if errorlevel 1 (
    echo   [X] pip install failed - read the error above.
) else (
    echo   %OK% packages installed.
)
goto :after

:s_extras
echo.
echo == Extra Python packages for the desktop tools ==
echo   screenshots, mouse/keyboard, clipboard, windows, screenshots of windows
%PYEXE% -m pip install pywin32 pyautogui psutil pyperclip pillow requests websocket-client
echo.
echo   OCR helper (click_text / screen_text need it plus the Tesseract binary)
%PYEXE% -m pip install pytesseract
echo.
echo   Local neural TTS (Kokoro)
%PYEXE% -m pip install kokoro-onnx onnxruntime numpy sounddevice
echo.
echo   %OK% done. Any that failed are optional - the rest of Bonsai still works.
goto :after

:s_llama
echo.
echo == llama-server (the local model server) ==
set "LLAMA=%LOCALAPPDATA%\Programs\prism-llama\llama-server.exe"
if exist "%LLAMA%" (
    echo   %OK% found at "%LLAMA%"
    goto :after
)
echo   [--] Not at the default location "%LLAMA%"
echo.
echo   IMPORTANT: Bonsai 2 is a ternary model. Stock llama.cpp cannot read
echo   these .gguf files - you need a PrismML build with ternary kernels.
echo.
set "q="
set /p "q=  Do you want to enter the path to your llama-server.exe? [y/N]: "
if /i not "%q%"=="y" goto :after
set "lp="
set /p "lp=  Full path: "
if exist "%lp%" (
    echo   %OK% using "%lp%"
    echo   Set PC_LLAMA_SERVER="%lp%" as a user environment variable so
    echo   Bonsai finds it without being asked.
    setx PC_LLAMA_SERVER "%lp%" >nul 2>&1
) else (
    echo   No file there. Install PrismML llama.cpp and run this again.
)
goto :after

:s_models
echo.
echo == Model files ==
set "MISSING="
if exist "Ternary-Bonsai-2-27B-PQ2_0.gguf" (
    echo   %OK% Ternary-Bonsai-2-27B-PQ2_0.gguf
) else (
    echo   [--] Ternary-Bonsai-2-27B-PQ2_0.gguf is missing
    set "MISSING=1"
)
if exist "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf" (
    echo   %OK% Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf
) else (
    echo   [--] Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf is missing ^(needed for screenshots^)
    set "MISSING=1"
)
rem !MISSING! and not %MISSING%: the sets above sit inside parenthesised blocks,
rem and a plain %MISSING% here is expanded when the block is parsed - before
rem those sets run - so this always read as unset and the "nothing bundled"
rem warning printed even with both models sitting in the folder.
if defined MISSING (
    echo.
    echo   No .gguf is bundled with the repo - they are 6-7 GB each.
    echo   Put them in this folder. You can skip this entirely and use a hosted
    echo   model from the dropdown instead; that needs no download at all.
)
goto :after

:s_keys
echo.
echo == API keys ==
if exist "API KEYS.txt" (
    echo   %OK% "API KEYS.txt" exists - not showing its contents.
    goto :after
)
echo   [--] No "API KEYS.txt" yet.
echo.
echo   A template ships with the repo: "API KEYS.example.txt". Copy it and fill
echo   in whatever keys you have. Hosted models need one; a local model needs none.
echo.
set "q="
set /p "q=  Create it from the template now? [Y/n]: "
if /i "%q%"=="n" goto :after
copy /y "API KEYS.example.txt" "API KEYS.txt" >nul
echo.
echo   %OK% Created "API KEYS.txt". Open it in Notepad and uncomment the lines
echo       you need. It is git-ignored - never commit it.
start notepad "API KEYS.txt"
goto :after

:s_modelsjson
echo.
echo == models.json ==
if exist "models.json" (
    echo   %OK% "models.json" exists - leaving your settings alone.
    goto :after
)
echo   [--] No "models.json" yet.
echo.
echo   You do not actually need one. Without it, Bonsai finds every .gguf in
echo   this folder by itself and works out the rest. You only want this if you
echo   want to pre-set context sizes, ports or sampling values.
echo.
set "q="
set /p "q=  Create it from the template? [y/N]: "
if /i not "%q%"=="y" goto :after
copy /y "models.example.json" "models.json" >nul
echo   %OK% Created "models.json" from the template. Edit the paths to match
echo       where your .gguf files actually are.
goto :after

:s_verify
echo.
echo == Verifying the install ==
%PYEXE% -c "import bonsai_web" >nul 2>&1
if errorlevel 1 (
    echo   [X] bonsai_web.py does not import - run step 2 first.
) else (
    echo   %OK% bonsai_web.py imports cleanly.
)
%PYEXE% -c "import win32gui, psutil, pyautogui, PIL, pyperclip, requests, websocket; print('  desktop tools: ok')" 2>nul
%PYEXE% -c "import pytesseract; print('  ocr helper  : ok')" 2>nul
%PYEXE% -c "import kokoro_onnx, onnxruntime, sounddevice; print('  local tts   : ok')" 2>nul
if exist "%~dp0kokoro\voices-v1.0.bin" (
    echo   %OK% kokoro model found
) else (
    echo   [--] no Kokoro model - run: %PYEXE% kokoro\download_model.py
)
echo.
echo   That is every optional library checked. Lines that did not print are the
echo   ones you did not install. Nothing here is required to chat.
goto :after

:s_docker
echo.
echo == Docker Desktop (the docker_* tools) ==
docker --version >nul 2>&1
if not errorlevel 1 (
    docker version --format "  %OK% client {{.Client.Version}} / server {{.Server.Version}}" 2>nul
    if errorlevel 1 echo   [--] Docker CLI is there but the engine is not running - start Docker Desktop.
    goto :after
)
echo   [--] docker is not installed.
echo   OPTIONALS.cmd has it as a menu item - about 600 MB.
goto :after

:s_run
echo.
echo == Starting Bonsai ==
echo   Launching run.bat in a new window...
start "" "%~dp0run.bat"
echo   %OK% started. The page opens at http://127.0.0.1:8081
echo.
pause
goto :done

rem -----------------------------------------------------------------------------

:menu
cls
echo.
echo =============================================================================
echo   BONSAI  -  choose what to set up
echo =============================================================================
echo.
if defined PYEXE (
    echo   Python: %PYEXE%
) else (
    echo   Python: not found yet - step 1 handles it
)
echo.
echo   REQUIRED
echo     1  Python itself                      %PYEXE%
echo     2  Python packages                    %PYEXE%
echo     8  Verify the install
echo.
echo   OPTIONAL - pick only what you want
echo     3  Desktop extras ^(screenshots, OCR, TTS^)
echo     4  Point Bonsai at llama-server
echo     5  Check the model files are here
echo     6  Create API KEYS.txt from the template
echo     7  Create models.json from the template
echo     9  Docker Desktop status
echo.
echo   OTHER
echo     a  Run everything recommended  ^^(1, 2, 4, 5, 8^)
echo     r  Start Bonsai
echo     x  Quit
echo.
set "pick="
set /p "pick=  Choose (numbers can be spaced, e.g. 2 3): "
if not defined pick goto :menu
rem set /p reads to end of line, but a pasted or piped answer can arrive with
rem an embedded newline, and then every `if /i "..."` below is a syntax error
rem that kills the script mid-menu. Take the first line only - a real answer is
rem one line, and "2 3" on one line still works as two picks.
for /f "tokens=* delims=" %%p in ("!pick!") do (
    set "pick=%%p"
    goto :have_pick
)
:have_pick
if not defined pick goto :menu
echo(%pick%| findstr /r /c:"^[0-9aArRxX ]*$" >nul
if errorlevel 1 (
    echo   That is not one of the choices. Type a number, a/r, or x.
    goto :menu
)
if /i "%pick%"=="x" goto :done
if /i "%pick%"=="r" goto :s_run
if /i "%pick%"=="a" (
    set "STAGEPICK=1 2 4 5 8"
    goto :runqueue
)

:runqueue
if not defined STAGEPICK set "STAGEPICK=%pick%"
for %%S in (%STAGEPICK%) do call :dispatch %%S
if "%~1"=="" goto :menu
goto :done

:dispatch
if "%~1"=="1" goto :s_python
if "%~1"=="2" if not "%CANRUN%"=="1" goto :no_python
if "%~1"=="2" goto :s_packages
if "%~1"=="3" if not "%CANRUN%"=="1" goto :no_python
if "%~1"=="3" goto :s_extras
if "%~1"=="4" goto :s_llama
if "%~1"=="5" goto :s_models
if "%~1"=="6" goto :s_keys
if "%~1"=="7" goto :s_modelsjson
if "%~1"=="8" if not "%CANRUN%"=="1" goto :no_python
if "%~1"=="8" goto :s_verify
if "%~1"=="9" goto :s_docker
echo   Unknown step "%~1".
goto :eof

:no_python
echo   Step %~1 needs Python - run step 1 first.
goto :eof

:after
if "%~1"=="" (
    echo.
    set /p "dummy=  Press Enter to go back to the menu..."
)
goto :eof

:done
echo.
echo   Done. Start Bonsai with:  run.bat
echo.
pause
endlocal