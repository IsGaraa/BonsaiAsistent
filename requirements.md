# Requirements

This file lists everything BONSAI needs to run, split into **required** (the
app will not start without them) and **optional** (extra tool families; BONSAI
keeps working without them and each tool reports a clear error when its
package is missing).

Fast track: run `AUTO-SETUP.cmd` - it installs the Python packages below,
checks the required files and tells you exactly what is still missing.
`OPTIONALS.cmd` installs the system-level optionals (Docker, WSL2, Blender MCP).

## Required

| Requirement | What it is for | Notes |
|---|---|---|
| Windows 10/11 (64-bit) | the app controls a real Windows PC (launching apps, screenshots, input, windows) | required |
| Python 3.10+ | runs `bonsai_web.py` and the tools | required; verify with `python --version` |
| `prism-llama` `llama-server.exe` | runs the Bonsai 2 model (PrismML's ternary-optimized llama.cpp fork) | **required**; stock llama.cpp cannot run Bonsai 2 files. Expected at `%LOCALAPPDATA%\Programs\prism-llama\llama-server.exe` |
| `Ternary-Bonsai-2-27B-PQ2_0.gguf` | the 27B model weights | place in the project root (`.gitignore`d - too large to version) |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` | vision projector, gives BONSAI its eyes | place in the project root next to the model |

> No `.gguf` or binaries are bundled in this repo. The setup scripts check for
> them and print where to drop them / how to get prism-llama.

## Optional - Python packages (all included in `requirements.txt`)

| Package | Powers | Tool(s) |
|---|---|---|
| `pyautogui` | mouse/keyboard robot control | `control_input` |
| `pillow` | screen capture | `take_screenshot`, `tools/screenshot.py` |
| `pyperclip` | system clipboard | `clipboard`, `tools/clipboard.py` |
| `pywin32` (`win32gui` etc.) | window enumeration / bring-to-front | `window_list`, `window_action` |
| `psutil` | process names for windows | `window_list`, `window_action` |
| `requests` | HTTP/REST/GraphQL client | `api_call`, `tools/scraper.py` |
| `websocket-client` | WebSocket client | `ws_test` |
| `beautifulsoup4` | page parsing | `tools/scraper.py` |
| `pytesseract` | OCR | `tools/ocr.py` *(engine too: Tesseract OCR, see below)* |
| `piper-tts` + `onnxruntime` | local neural speech synthesis (Piper engine) | `tts_speak`, `tts_voices` |
| `sounddevice` | plays the synthesized WAV straight to the speakers (bundled PortAudio - no media player involved) | playback half of `tts_speak`; falls back to `winsound` |

Install everything with: `pip install -r requirements.txt -r tools\requirements.txt`

> **Piper voices**: the pack lives in `piper\` (inside the project). The
> `.onnx` + `.onnx.json` model files are **not** committed (too large) - fetch
> them once with `python piper\download_voices.py` (English + Romanian). Drop
> any other `<voice>.onnx` pair in `piper\` and use it via the `voice`
> parameter. Point elsewhere with the `PC_PIPER_DIR` environment variable.
>
> TTS is **off by default**: the model only speaks after you click the
> **TTS** button in the header (state is per-run, resets on restart).

| Package | Powers |
|---|---|
| `mcp-for-blender` | lets BONSAI drive Blender (scenes, meshes, materials, viewport screenshots) when Blender + the "MCP for Blender" addon are running |

## Optional - system level

| Item | Powers | Install |
|---|---|---|
| **Docker Desktop + WSL2 + Virtual Machine Platform** | the `docker_ps` / `docker_images` / `docker_start` / `docker_stop` / `docker_restart` / `docker_logs` / `docker_exec` tools | `winget install --id Docker.DockerDesktop`, then `wsl --install --no-distribution` and enable the Virtual Machine Platform feature; **reboot required**. `OPTIONALS.cmd` drives all of it |
| **"MCP for Blender" addon** (in Blender) | enables the Blender tool family | Blender > Edit > Preferences > Add-ons, enable it after installing `mcp-for-blender` |
| **Tesseract OCR engine** | powers `tools/ocr.py` | `winget install --id UB-Mannheim.TesseractOCR` |
| **Node.js / Go / Lua / PHP / Ruby / Perl / bash runtime** | extra languages for the `run_code` sandbox - installed ones are auto-detected (Python always works) | Node: `winget install --id OpenJS.NodeJS` (Go: `winget install GoLang.Go`) |

## Files that must be present (fresh clone)

```
bonsai_web.py        the whole assistant (server + UI + tools)
run.bat              one-click launcher (opens the UI at http://127.0.0.1:8081)
stop.cmd             stops the model server (port 8080)
AUTO-SETUP.cmd       installs Python deps + verifies required files
OPTIONALS.cmd        installs Docker/WSL2/Blender-MCP system optionals
requirements.md      this file
requirements.txt     optional Python packages (see table above)
tools/               helper scripts (screenshot, clipboard, scraper, ocr)
piper/               Piper TTS: tts.py + download_voices.py (voice models are
                     git-ignored - run `python piper\download_voices.py` once)
tools.json           machine-readable tool reference (auto-generated from code)
instructions.txt    what BONSAI knows about itself
README.md            the full manual
```