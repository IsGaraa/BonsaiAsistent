# BonsaiAsistent

**BONSAI — a 100% offline, JARVIS-style PC assistant.** Multi-turn voice & text chat, live "thinking" display, real tool calls (open apps, open websites, work on your files), vision, and optional web lookup — all powered by the local **Bonsai 2 27B** ternary model. No cloud, no API keys, nothing leaves your machine.

![status](https://img.shields.io/badge/offline-100%25-00f0ff) ![model](https://img.shields.io/badge/Bonsai%202-27B%20ternary-39ff14) ![license](https://img.shields.io/badge/license-Apache--2.0-blue)

---

## What it can do

- 🗣️ **Multi-turn chat** with a locally running 27B model (Bonsai 2, ternary 2-bit quantization).
- 🧠 **Live thinking** — the assistant shows its reasoning as it flows, like OpenAI's "reasoning" mode.
- 🛠️ **Tool calls** — it decides by itself when to use a tool and shows every call it makes:
  - 🚀 **Launch/open** — Notepad, Steam, Chrome, YouTube, Spotify, system settings, apps & sites by name.
  - 📁 **File tools** — `list_dir`, `read_file`, `search_files`, `write_file`, safely restricted to a **workspace folder**.
  - 👁️ **Vision** — attach an image and it understands it (red t-shirt, screenshots, documents…).
  - 🌐 **Web lookup** — when it's not sure or you ask for something current, it can `web_search` and `web_fetch` to document itself from the internet before answering.
  - 📁 **Workspace picker** — choose any folder on your PC as the working directory (📁 button in the header), directly from a native folder dialog.
- 💻 **Shell + sandbox** — the `run_command` tool executes real Windows commands (build, install, debug, inspect); the `run_code` tool runs short Python/Node snippets in a sandboxed, network-less environment with a 30s timeout.
- ⏹️ **STOP + QUEUE** — the SEND button turns into STOP while I'm replying so you can cut me off; QUEUE holds a follow-up message that gets answered right after the current reply.
- ⏱️ **Live token stats** — a real-time line under the input shows how long I spend thinking vs. speaking, my tokens/second, tokens used and context used/left (ctx 32768); a final chip is saved with each reply.
- 🛌 **Auto memory relief** — if you don't message me for 2 minutes, the model unloads from RAM; the moment you text again it loads itself back (first reply after a long pause takes a few seconds longer).
- 📚 **Chat history** — every conversation is saved both in your browser and on disk (`%APPDATA%\BonsaiAsistent\chats.json`); reopen it any time, even after a server restart.
- 🔵 **Reactive HUD** — the arc reactor reacts to what it's doing:
  - 🔵 **Blue** – idle (rotating reactor)
  - 🟡 **Yellow** – thinking / processing the command
  - 🟢 **Green** – executing a tool call
  - 🌊 **Blue + waveform** – speaking out loud
- 🔊 **Talk-back** — optional text-to-speech that **auto-matches the language** of the reply (Romanian / English) and picks the best natural voice. Off by default.
- 🔒 **Plan / Build modes** — *Plan* is read-only (just looks at files), *Build* has full tool access.
- 📴 **Fully offline** — model, inference and UI all run on your PC.

---

## Requirements

- **Windows** (the app ships as a Python script; launching apps uses Windows URIs/commands).
- **Python 3.10+**.
- A GPU with ~7–8 GB VRAM for fast inference (tested on an RTX 4070, ~48 tok/s). Bonsai 2 could also run CPU-only, just slower.
- The **prism-llama** `llama-server.exe` (the PrismML llama.cpp fork with ternary kernels — stock llama.cpp can't run Bonsai 2 files).

---

## How to run

Everything lives in this folder — the model files, the model server scripts and the assistant app.

Double-click **`run.bat`** — it starts the assistant, auto-starts the Bonsai 2 model server on port 8080 if it isn't running yet, and opens the UI at **http://localhost:8081**. To stop the model server, double-click `stop.cmd`.

You're in. Type a command, e.g.:

```
Open Notepad and start YouTube
What is the CPU doing?
Tell me a joke
```

The SEND button becomes **STOP** while I'm replying, so you can cut me off. There's also a **QUEUE** button: type a follow-up while I'm busy and hit QUEUE — it gets answered right after the current reply (QUEUE saves up a backlog in order).

### Configuration (optional)

Environment variables, all optional:

| Variable    | Default                              | Purpose                              |
|-------------|--------------------------------------|--------------------------------------|
| `BONSAI_DIR` | `C:\Users\drago\Desktop\bonsai2`    | Folder containing the `.gguf` files  |
| `PC_WORKDIR` | `C:\Users\drago\Desktop\workspace`  | Workspace folder the file tools use  |

The workspace folder can also be changed at runtime from the UI (the 📁 button in the header).

---

## The AI model

The assistant talks to **Bonsai 2 27B** — a ternary (1.58-bit) 27B-class model built on a Qwen3 hybrid-attention backbone, ~9x smaller than its FP16 original at near-parity in quality (~5.9–7.2 GB instead of ~54 GB).

| File | What it is | Download |
| --- | --- | --- |
| `Ternary-Bonsai-2-27B-PQ2_0.gguf` | Language model (7.21 GB, ternary g128) | [Hugging Face](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf) |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` | Vision projector (0.63 GB) — required for image input | [Hugging Face](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf) |

Useful links:

- 📄 Model card & weights: **https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf**
- 📚 PrismML model downloads & docs: **https://docs.prismml.com/download/models**
- 🔧 Runtime engine (`llama-server`): the **PrismML llama.cpp fork** — https://github.com/PrismML-Eng/llama.cpp (stock llama.cpp won't load PQ2_0/PTQ1_0 tensors)
- 🧪 Official demos: https://github.com/PrismML-Eng/Bonsai-demo

---

## Project layout

```
bonsai_web.py                     ← the whole assistant (single file: backend + UI)
run.bat                           ← the ONE launcher: starts UI + model server
stop.cmd                          ← stops the model server
tools/                            ← helper scripts (screenshot, clipboard, scraper, OCR)
tools.json                        ← tool-call spec reference
instructions.txt                  ← quick-start guide (English)
*.gguf                            ← the model weights (local only, not in git)
README.md                         ← this file
```

The repo intentionally does **not** contain the model weights — they are git-ignored; download them from the links above if missing.

---

## Security & privacy

- Everything runs locally — the browser only talks to `127.0.0.1`.
- File tools are confined to the configured workspace folder.
- Don't expose the server to the internet; it's an assistant, not a web service.

## License

Apache-2.0 (same as the Bonsai 2 model — see `LICENSE` in the model package).