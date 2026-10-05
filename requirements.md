# Requirements

This file lists everything BONSAI needs to run, split into **required** (the
app will not start without them) and **optional** (extra tool families; BONSAI
keeps working without them and each tool reports a clear error when its
package is missing).

Fast track: run `AUTO-SETUP.cmd` (Windows) or `AUTO-SETUP.sh` (Linux) - it
installs the Python packages below, checks the required files and tells you
exactly what is still missing. `OPTIONALS.cmd` installs the Windows
system-level optionals (Docker, WSL2, Blender MCP).

## Required

| Requirement | What it is for | Notes |
|---|---|---|
| Windows 10/11 (64-bit) or Linux | the app controls a real PC (launching apps, screenshots, input, windows) | required; on Linux `xdg-open` + binaries replace the Windows shell |
| Python 3.10+ | runs `bonsai_web.py` and the tools | required; verify with `python3 --version` |
| `prism-llama` `llama-server` | runs the Bonsai 2 model (PrismML's ternary-optimized llama.cpp fork) | **required**; stock llama.cpp cannot run Bonsai 2 files. Windows: `%LOCALAPPDATA%\Programs\prism-llama\llama-server.exe`; Linux: any `llama-server` on `PATH` or set `PC_LLAMA_SERVER` |
| `Ternary-Bonsai-2-27B-PQ2_0.gguf` | the 27B model weights | place in the project root (`.gitignore`d - too large to version) |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` | vision projector, gives BONSAI its eyes | place in the project root next to the model |

> No `.gguf` or binaries are bundled in this repo. The setup scripts check for
> them and print where to drop them / how to get prism-llama.

## Optional - Python packages (all included in `requirements.txt`)

| Package | Powers | Tool(s) |
|---|---|---|
| `pyautogui` | mouse/keyboard robot control | `control_input` (Linux needs an X11 session) |
| `pillow` | screen capture | `take_screenshot`, `tools/screenshot.py` (Linux: ImageGrab or scrot/ImageMagick fallback) |
| `pyperclip` | system clipboard | `clipboard`, `tools/clipboard.py` (Linux needs `xclip`/`xsel`) |
| `pywin32` (`win32gui` etc.) | window enumeration / bring-to-front (Windows-only package) | `window_list`, `window_action`; on Linux the same tools use `wmctrl` |
| `psutil` | process names for windows | `window_list`, `window_action` |
| `requests` | HTTP/REST/GraphQL client | `api_call`, `tools/scraper.py` |
| `websocket-client` | WebSocket client | `ws_test` |
| `beautifulsoup4` | page parsing | `tools/scraper.py` |
| `pytesseract` | OCR | `tools/ocr.py`, plus the `click_text` and `wait_for(kind=screen_text)` tools *(engine too: Tesseract OCR, see below)* |
| `kokoro-onnx` + `onnxruntime` | local neural speech synthesis (Kokoro-82M v1.0) | `tts_speak`, `tts_voices`, the `/kokoro` page |
| `sounddevice` | plays the synthesized WAV straight to the speakers (bundled PortAudio - no media player involved) | playback half of `tts_speak`; falls back to `winsound` on Windows and `paplay`/`aplay`/`ffplay` on Linux |

Install everything with: `pip install -r requirements.txt -r tools\requirements.txt`
(pywin32 in `requirements.txt` is marked Windows-only with a PEP 508 marker, so
`pip` on Linux skips it automatically).

> **Kokoro weights**: they live in `kokoro\` (inside the project) and are **not**
> committed — `kokoro-v1.0.onnx` is 310 MB and `voices-v1.0.bin` is 27 MB. Fetch
> them once with `python kokoro\download_model.py`. Pass `--model int8` for a
> 109 MB quantised model instead, or `--model fp16` for 156 MB. Point elsewhere
> with `PC_KOKORO_DIR`, and choose the file with `PC_KOKORO_MODEL`.
>
> `kokoro-onnx` brings `espeakng-loader` and `phonemizer` with it; those are the
> text-to-phoneme step and are not optional extras.
>
> **English only.** The voices file carries styles for a dozen languages, but
> this app offers the American and British ones (`af_*`, `am_*`, `bf_*`, `bm_*`).
> Kokoro publishes no Romanian styles, so the Piper `ro_RO-*` voices that used to
> ship with the project are gone with no replacement.
>
> TTS in the chat is **off by default**: replies only speak after you click the
> **TTS** button in the header (state is per-run, resets on restart). The
> `/kokoro` page always plays in the browser and ignores that toggle.

| Package | Powers |
|---|---|
| `mcp-for-blender` | lets BONSAI drive Blender (scenes, meshes, materials, viewport screenshots) when Blender + the "MCP for Blender" addon are running |
| `yt-dlp` | the `download_media` tool (video, audio-only as mp3, subtitles, thumbnails) |

## Optional - system level

| Item | Powers | Install |
|---|---|---|
| **"MCP for Blender" addon** (in Blender) | enables the Blender tool family | Blender > Edit > Preferences > Add-ons, enable it after installing `mcp-for-blender` |
| **Tesseract OCR engine** | powers `tools/ocr.py` | `winget install --id UB-Mannheim.TesseractOCR` |
| **Node.js / Go / Lua / PHP / Ruby / Perl / bash runtime** | extra languages for the `run_code` sandbox - installed ones are auto-detected (Python always works) | Windows: `winget install --id OpenJS.NodeJS` (Go: `winget install GoLang.Go`); Linux: your distro packages (`sudo apt install nodejs golang lua5.4 php ruby perl`) |

## Files that must be present (fresh clone)

```
bonsai_web.py        the whole assistant (server + UI + tools)
run.bat / run.sh     one-click launcher (opens the UI at http://127.0.0.1:8081)
stop.cmd / stop.sh   stops the model server (port 8080)
AUTO-SETUP.cmd/.sh   installs Python deps + verifies required files (Win/Linux)
OPTIONALS.cmd        installs Docker/WSL2/Blender-MCP system optionals (Windows)
requirements.md      this file
requirements.txt     optional Python packages (see table above)
tools/               helper scripts (screenshot, clipboard, scraper, ocr)
kokoro/               Kokoro TTS: download_model.py (the .onnx model and the
                     voices file are git-ignored - run
                     `python kokoro\download_model.py` once)
tools.json           machine-readable tool reference (auto-generated from code)
instructions.txt     what BONSAI knows about itself
README.md            the full manual
```