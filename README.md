# Bonsai PC Assistant

Self-contained, 100% offline **local AI assistant** built around the **Bonsai 2
27B** ternary model. Multi-turn text chat (with optional microphone input), live
"thinking" display, real
tool calls (open apps, open websites, work on your files), vision, optional web
lookup, a shell runner and a code sandbox - all in one Python file, no cloud, no
API keys, nothing leaves your machine.

![Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)
![Offline](https://img.shields.io/badge/offline-100%25-00f0ff)
![Model](https://img.shields.io/badge/Bonsai%202-27B%20ternary-39ff14)
![License](https://img.shields.io/badge/License-Apache--2.0-blue)

> **🔒 PRIVACY**
>
> The model, inference and UI all run on your PC. The browser only ever talks to
> `127.0.0.1` - Bonsai is a local assistant, not a web service. Don't expose the
> server to the internet.

---

## Table of contents

1. Quick start
2. Requirements
3. Capabilities
4. Tool execution (how it works)
5. The console - controls & HUD
6. Live token stats & memory relief
7. Chat history
8. Configuration
9. The AI model
10. Security & privacy
11. Troubleshooting
12. Repository layout
13. License

---

## 1. Quick start

Everything lives in this folder - the model files, the model server and the
assistant app. **One click**:

```
run.bat
```

It starts the assistant UI at **http://localhost:8081**, auto-starts the
Bonsai 2 model server on port 8080 if it isn't running yet, and opens your
browser. To stop the model server, double-click `stop.cmd`.

Once the UI is open, try a few commands:

```
Create a Python script in the workspace that renames files with regex
Open the README in the workspace and summarize it
Screenshot the current window and describe what you see
Check the latest news about Nvidia and pick three highlights
List the .py files in this folder with their sizes
```

The SEND button becomes **STOP** while Bonsai is replying, so you can cut it
off. There's also a **QUEUE** button: type a follow-up while it's busy and hit
QUEUE - it gets answered right after the current reply (QUEUE keeps a backlog
in order).

## 2. Requirements

| Requirement | Why | Verdict |
|---|---|---|
| Windows | the app is a Python script; launching apps uses Windows URIs/commands | required |
| Python 3.10+ | runs `bonsai_web.py` | required |
| GPU with ~7-8 GB VRAM | fast inference (tested on an RTX 4070, ~48 tok/s); CPU-only works, just slower | recommended |
| `prism-llama` `llama-server.exe` | the PrismML llama.cpp fork with ternary kernels - stock llama.cpp **can't** run Bonsai 2 files | required |
| `Pillow` / `pyautogui` / `pyperclip` | power `take_screenshot`, `control_input` and `clipboard` | recommended (tools report a clear error if missing) |
| **Blender + MCP addon** | for the Blender tools (§4) | optional; `mcp-for-blender` Python package + the bundled addon enabled in Blender (Edit > Preferences > Add-ons > "MCP for Blender") |
| Docker / Hyper3D / sketchfab accounts | optional extra Blender *content* tools | not bundled here - only the core Blender tools ship |

## 3. Capabilities

| Capability | Details |
|---|---|
| **Multi-turn chat** | conversational assistant powered by a local 27B model (Bonsai 2, ternary 2-bit quantization) |
| **Live reasoning** | shows its chain of thought in real time, streaming as it is produced |
| **Tool calls** | decides autonomously when to use a tool and surfaces every call (see §4) |
| **Vision + screen capture** | understands attached images *and* can `take_screenshot` the live screen - it actually sees what's displayed (apps, error dialogs, terminal output) |
| **PC control** | `control_input` moves the mouse, clicks, drags, scrolls and types, like a human using the PC |
| **Clipboard** | reads or writes the system clipboard with `clipboard` (get / set) |
| **Downloads & archives** | `download_file` saves files from the web into the workspace; `archive` creates/extracts zip & tar |
| **Shell + sandbox** | `run_command` executes real Windows commands (configurable timeout, up to 600s); `run_code` runs short Python/Node snippets in a sandboxed, network-less environment |
| **Asks you questions** | `ask_user` pauses and asks you a question (with optional clickable options) exactly like a human would - just like opencode |
| **Todo list** | `todo_write` shows a live, visible checklist on the left panel as it works through longer tasks |
| **Thinking effort** | **THINK: OFF / LOW / MED / HIGH** selector in the composer controls how deep Bonsai reasons (maps to `enable_thinking` / `reasoning_effort`) |
| **STOP / QUEUE** | abort a reply mid-stream, or queue a follow-up to be answered immediately after |
| **Live token stats** | real-time think vs. speak time, tokens/second and context usage under the input (see §6) |
| **Always-in-memory** | the model stays loaded (resident) for the whole session - no idle timer ever unloads it, so a reply never stalls because of a pause (see §6) |
| **Persistent history** | conversations saved in the browser and on disk; survive server restarts (see §7) |
| **Plan / Build modes** | *Plan* is read-only (inspection only), *Build* grants full tool access |
| **Blender control** | when Blender is open, Bonsai can create, move and edit 3D scenes inside it and screenshot the viewport - you keep the mouse (no exodus) |
| **Fully offline** | model, inference and UI all run locally, with no cloud dependency |

## 4. Tool execution (how it works)

Bonsai is a **tool-calling agent**. Each reply runs up to 5 model rounds
(`MAX_TOOL_ROUNDS`): the model decides whether it needs a tool, the tool runs,
its result is folded back into the conversation, and the model continues. Every
call shows up live in the chat as a chip (green = ok, red = error) with a
full log.

| Tool | What it does | Notes |
|---|---|---|
| `launch_or_open` | Opens apps, websites, files and Windows settings by name | Notepad, Steam, Chrome, Spotify, YouTube, system settings... |
| `list_dir` / `read_file` / `search_files` / `write_file` / `edit_file` | Work on your files | strictly confined to the **workspace folder** |
| `take_screenshot` | Captures the screen (or a region) and **feeds the image to Bonsai's eyes** | also saves a PNG in the workspace; shown as a thumbnail in the tool log |
| `control_input` | Moves the mouse, clicks, double/right-clicks, drags, scrolls, types text, presses keys/hotkeys | screen-pixel coordinates; pair with `take_screenshot` to see the result |
| `clipboard` | Reads (`get`) or writes (`set`) the system clipboard | |
| `download_file` | Downloads a file from a URL into the workspace | returns path, size and a text preview when possible |
| `archive` | Creates / extracts zip, tar, tar.gz, tgz archives | unpack or pack inside the workspace |
| `ask_user` | Asks you a question and **waits for your answer** (options or free text) | pauses its work like opencode's question skill |
| `todo_write` | Replaces the visible TODO checklist (pending / in_progress / completed) | shown live on the left panel |
| `web_search` / `web_fetch` | Look up current information online when it's not sure | documents itself before answering |
| `run_command` | Runs a real Windows command | timeout default 45s, configurable up to 600s, output capped to ~8 KB |
| `run_code` | Runs short Python/Node snippets | sandboxed, **no network**, 30s timeout, ~8 KB output cap |
| `get_scene_info` | Lists the open Blender scene: objects, types, locations (Mesh, Camera, Light, ...) | requires Blender running with the *MCP for Blender* addon enabled |
| `get_object_info` | Details on one object (location, rotation, scale, ...) | |
| `execute_blender_code` | Runs real Python inside Blender's `bpy` context | add cubes, move them, re-parent, set materials - always step by step |
| `get_viewport_screenshot` | Captures the Blender viewport and **feeds it to Bonsai's eyes** | shown as a thumbnail; the model confirms its work visually |
| `bpy_api_lookup` / `describe_node_type` / `export_scene` | look up the exact `bpy` API, inspect one shader-node type, export the scene | Bonsai reads real API docs so its code actually runs |

The **Blender** tools run through `mcp-for-blender` (a stdio MCP server that talks to
the addon on `localhost:9876`). Bonsai keeps full control of the tool loop and
reasoning - only the actual Blender commands are delegated. A status light in the
header shows the connection: green **BLENDER: CONNECTED**, yellow *ADDON OFF*
(Blender open but addon not enabled), red *OFF*.

The **workspace** is the only area the file tools touch. You can pick any folder
from the UI (📁 button in the header) with a native folder dialog, or set
`PC_WORKDIR` (see §8).

## 5. The console - controls & HUD

| Control | What it does |
|---|---|
| **SEND / STOP** | Submits your message; while Bonsai is replying it becomes **STOP** to abort the run |
| **QUEUE** | Holds your typed message so it's answered right after the current reply (ordered backlog) |
| **THINK: OFF / LOW / MED / HIGH** | Selects how deeply Bonsai reasons (`enable_thinking` / `reasoning_effort`); higher = deeper reasoning, slower replies. Default MED |
| **TODO panel** | Live checklist on the left panel, updated by `todo_write` as longer tasks progress |
| **Workspace (folder icon)** | Choose/open the working folder for the file tools |
| **Blender status light** | Shows the Blender MCP connection: green *CONNECTED*, yellow *ADDON OFF*, red *OFF*, grey *NO MCP* |
| **EJECT** | Unloads the model from RAM/VRAM **now** to free memory; it simply loads back on the next message |
| **Plan / Build** | Toggle mode - *Plan* read-only, *Build* full tool access |
| **Mic (🎙)** | microphone voice input (speech-to-text) for your messages |
| **NEW CHAT** | Start a fresh conversation |

The **arc reactor** indicator reflects what Bonsai is doing:

- **Blue** - idle (rotating reactor)
- **Yellow** - thinking / processing the command
- **Green** - executing a tool call

## 6. Live token stats & memory relief

**Real-time stats line** under the input while Bonsai works:

```
THINK 2.8s · SPEAK 0.4s · 45.3 tok/s · tokens 138 · ctx 1638/32768
```

- **THINK** - time spent reasoning before the first output token.
- **SPEAK** - time spent generating the reply.
- **tok/s** - average generation speed (RTX 4070 ≈ 44-48).
- **ctx** - context slots used / total (32k window), refreshed live.

A compact gold chip with the same numbers is saved onto each reply so the
stats survive reloads.

**Always resident, eject when you want** - the model stays fully loaded for as
long as the app (and `llama-server`) are running, so there is no idle timer
that could unload it mid-conversation or right before your next message. When
you *want* the memory back (e.g. before running a big game), click **EJECT** in
the header and the model unloads immediately - it simply loads again on the
next message, no restart needed.

## 7. Chat history

Conversations are saved in two places:

| Where | Path | Purpose |
|---|---|---|
| Browser | `localStorage` (`jarvis_chats`) | instant load on page open |
| Disk | `%APPDATA%\BonsaiAsistent\chats.json` | survives server restarts / cleared browser data |

Loaded from the disk file on startup, mirrored on every save, capped at the
last 200 conversations. Reopen any conversation from the left panel at any
time.

## 8. Configuration

All settings are optional. Unless an environment variable is set, Bonsai uses
the defaults defined at the top of `bonsai_web.py` (which resolve to the
project's local folders).

| Variable | Purpose | When to set it |
|---|---|---|
| `BONSAI_DIR` | Folder containing the `.gguf` model files | only if the model files live somewhere other than the project folder |
| `PC_WORKDIR` | The workspace folder the file tools use | to point file access at a specific folder by default |

The workspace can also be changed at runtime from the UI (📁 button in the
header) - this is the recommended way.

## 9. The AI model

The assistant talks to **Bonsai 2 27B** - a ternary (1.58-bit) 27B-class model
built on a Qwen3 hybrid-attention backbone, ~9x smaller than its FP16 original
at near-parity quality (~5.9-7.2 GB instead of ~54 GB).

| File | What it is | Download |
|---|---|---|
| `Ternary-Bonsai-2-27B-PQ2_0.gguf` | Language model (7.21 GB, ternary g128) | [Hugging Face](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf) |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` | Vision projector (0.63 GB) - required for image input | [Hugging Face](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf) |

Server profile: 32k context, `-ngl 99`, flash-attention, temp 1.0, top-p 0.95,
top-k 20.

Useful links:

- 📄 Model card & weights: **https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf**
- 📚 PrismML model downloads & docs: **https://docs.prismml.com/download/models**
- 🔧 Runtime engine (`llama-server`): the **PrismML llama.cpp fork** - https://github.com/PrismML-Eng/llama.cpp (stock llama.cpp won't load PQ2_0/PTQ1_0 tensors)
- 🧪 Official demos: https://github.com/PrismML-Eng/Bonsai-demo

## 10. Security & privacy

- Everything runs locally - the browser only talks to `127.0.0.1`.
- File tools are confined to the configured workspace folder.
- `run_code` runs in a sandbox with **no network access** and a 30s timeout.
- Chat history lives on your PC only (`%APPDATA%\BonsaiAsistent\chats.json`).
- Don't expose the server to the internet; it's an assistant, not a web service.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| First reply after a pause is slow | normal only if `llama-server` was stopped/restarted meanwhile - otherwise the model stays resident for the whole session |
| Model won't start / blank UI | make sure `prism-llama`'s `llama-server.exe` is installed and `BONSAI_DIR` points at the `.gguf` files |
| Vision doesn't work | the `*.mmproj-Q8_0.gguf` file must be next to the language model |
| "Bonsai 2 model server could not start" | run `run.bat` again; check port 8080 isn't taken and the GPU/driver support CUDA |
| Model answers but sees no files | pick the workspace folder (📁 button) - file tools are confined to it |
| `take_screenshot` / `control_input` / `clipboard` error ("not installed") | install Pillow, pyautogui and pyperclip (`pip install pillow pyautogui pyperclip`) |
| Tool output looks truncated | results are intentionally capped (~8 KB) to protect the 32k context |
| Blender light stuck yellow (**ADDON OFF**) | open Blender and enable the addon (Edit > Preferences > Add-ons > search "MCP for Blender") - the server auto-starts on port 9876 |
| Blender tools error "not reachable" | Blender must be running with the addon enabled; the reconnect is automatic on the next command |
| Blender tools missing from the chat | close any other app serving MCP on `localhost:9876` and make sure `mcp-for-blender` is pip-installed (`pip install mcp-for-blender`) |

## 12. Repository layout

```
bonsai_web.py          # the whole assistant (single file: backend + UI)
run.bat                # the ONE launcher: starts UI + model server
stop.cmd               # stops the model server
tools/                 # optional helper scripts (screenshot, clipboard, scraper, OCR) - the same capabilities are also built into bonsai_web.py
tools.json             # tool-call spec reference
instructions.txt       # quick-start guide (English)
*.gguf                 # the model weights (local only, not in git)
README.md              # this file
```

The repo intentionally does **not** contain the model weights - they are
git-ignored; download them from the links in §9 if missing.

## 13. License

Apache-2.0 (same as the Bonsai 2 model - see `LICENSE` in the model package).