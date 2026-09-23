# Bonsai PC Assistant

Self-contained, 100% offline **JARVIS-style PC assistant** built around the local
**Bonsai 2 27B** ternary model. Multi-turn voice & text chat, live "thinking"
display, real tool calls (open apps, open websites, work on your files), vision,
optional web lookup, a shell runner and a code sandbox - all in one Python file,
no cloud, no API keys, nothing leaves your machine.

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

You're in. Type a command:

```
Open Notepad and start YouTube
What is the CPU doing?
Tell me a joke
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

## 3. Capabilities

- 🗣️ **Multi-turn chat** with a locally running 27B model (Bonsai 2, ternary 2-bit quantization).
- 🧠 **Live thinking** - the assistant shows its reasoning as it flows, like OpenAI's "reasoning" mode.
- 🛠️ **Tool calls** - Bonsai decides by itself when to use a tool and shows every call it makes (see §4).
- 👁️ **Vision** - attach an image (photo, screenshot, document) and it understands it.
- 💻 **Shell + sandbox** - `run_command` executes real Windows commands (build, install, debug, inspect); `run_code` runs short Python/Node snippets in a sandboxed, network-less environment with a 30s timeout.
- ⏹️ **STOP + QUEUE** - cut off a reply mid-stream, or queue a follow-up to be answered right after.
- ⏱️ **Live token stats** - real-time line under the input showing think vs. speak time, tokens/second, tokens used and context used/left (see §6).
- 🛌 **Auto memory relief** - idle for 2 minutes and the model unloads from RAM; the moment you text again it loads itself back (see §6).
- 📚 **Chat history** - every conversation is saved both in the browser and on disk; survives server restarts (see §7).
- 🔊 **Talk-back** - optional text-to-speech that **auto-matches the language** of the reply (Romanian / English) and picks the best natural voice. Off by default.
- 🔒 **Plan / Build modes** - *Plan* is read-only (just looks at files), *Build* has full tool access.
- 📴 **Fully offline** - model, inference and UI all run on your PC.

## 4. Tool execution (how it works)

Bonsai is a **tool-calling agent**. Each reply runs up to 5 model rounds
(`MAX_TOOL_ROUNDS`): the model decides whether it needs a tool, the tool runs,
its result is folded back into the conversation, and the model continues. Every
call shows up live in the chat as a chip (green = ok, red = error) with a
full log.

| Tool | What it does | Notes |
|---|---|---|
| `launch_or_open` | Opens apps, websites, files and Windows settings by name | Notepad, Steam, Chrome, Spotify, YouTube, system settings... |
| `list_dir` / `read_file` / `search_files` / `write_file` | Work on your files | strictly confined to the **workspace folder** |
| `screenshot` / `clipboard` / `ocr` / `scraper` | Capture screen, read clipboard, OCR text from images, grab web page text | helper scripts in `tools/` |
| `web_search` / `web_fetch` | Look up current information online when it's not sure | documents itself before answering |
| `run_command` | Runs a real Windows command | timeout-capped, output capped to ~8 KB |
| `run_code` | Runs short Python/Node snippets | sandboxed, **no network**, 30s timeout, ~8 KB output cap |

The **workspace** is the only area the file tools touch. You can pick any folder
from the UI (📁 button in the header) with a native folder dialog, or set
`PC_WORKDIR` (see §8).

## 5. The console - controls & HUD

| Control | What it does |
|---|---|
| **SEND / STOP** | Submits your message; while Bonsai is replying it becomes **STOP** to abort the run |
| **QUEUE** | Holds your typed message so it's answered right after the current reply (ordered backlog) |
| **📁 workspace** | Choose/open the working folder for the file tools |
| **Plan / Build** | Toggle mode - *Plan* read-only, *Build* full tool access |
| **🔊 / 🎙 voice** | Talk-back (TTS) toggle and microphone input |
| **+ NEW CHAT / CLEAR** | Start a fresh conversation / reset the current one |

The **arc reactor** reacts to what Bonsai is doing:

- 🔵 **Blue** - idle (rotating reactor)
- 🟡 **Yellow** - thinking / processing the command
- 🟢 **Green** - executing a tool call
- 🌊 **Blue + waveform** - speaking out loud

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

**Auto memory relief** - every request tells the model server
`keep_alive: 120`, so if you don't message Bonsai for 2 minutes the model
unloads from RAM to free memory. The moment you text again it loads itself
back automatically - only the first reply after a long pause is slower.

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

Environment variables, all optional:

| Variable | Default | Purpose |
|---|---|---|
| `BONSAI_DIR` | `C:\Users\drago\Desktop\bonsai2` | folder containing the `.gguf` files |
| `PC_WORKDIR` | `C:\Users\drago\Desktop\workspace` | the workspace folder the file tools use |

The workspace can also be changed at runtime from the UI (📁 button in the
header).

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
| First reply after a pause is slow | normal - the model unloaded from RAM after 2 min idle and is reloading (auto) |
| Model won't start / blank UI | make sure `prism-llama`'s `llama-server.exe` is installed and `BONSAI_DIR` points at the `.gguf` files |
| Vision doesn't work | the `*.mmproj-Q8_0.gguf` file must be next to the language model |
| "Bonsai 2 model server could not start" | run `run.bat` again; check port 8080 isn't taken and the GPU/driver support CUDA |
| Model answers but sees no files | pick the workspace folder (📁 button) - file tools are confined to it |
| Tool output looks truncated | results are intentionally capped (~8 KB) to protect the 32k context |

## 12. Repository layout

```
bonsai_web.py          # the whole assistant (single file: backend + UI)
run.bat                # the ONE launcher: starts UI + model server
stop.cmd               # stops the model server
tools/                 # helper scripts (screenshot, clipboard, scraper, OCR)
tools.json             # tool-call spec reference
instructions.txt       # quick-start guide (English)
*.gguf                 # the model weights (local only, not in git)
README.md              # this file
```

The repo intentionally does **not** contain the model weights - they are
git-ignored; download them from the links in §9 if missing.

## 13. License

Apache-2.0 (same as the Bonsai 2 model - see `LICENSE` in the model package).