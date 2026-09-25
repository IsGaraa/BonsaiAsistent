# Bonsai PC Assistant

Self-contained, 100% offline **local AI assistant** built around the **Bonsai 2
27B** ternary model. Multi-turn text chat (with optional microphone input), live
"thinking" display, real
tool calls (open apps, open websites, work on your files), vision, optional web
lookup, a shell runner and a code sandbox - all in one Python file, no cloud, no
API keys, nothing leaves your machine.

![Windows](https://img.shields.io/badge/Platform-Windows-0078D6?logo=windows&logoColor=white)
![Linux](https://img.shields.io/badge/Platform-Linux-FCC624?logo=linux&logoColor=black)
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

Fresh Windows machine? Run **`AUTO-SETUP.cmd`** first - it installs every Python
package (required + optional), verifies the model server and model files, and
tells you what is missing. `OPTIONALS.cmd` installs the system-level extras
(Docker Desktop + WSL2, Tesseract OCR, Blender MCP). See `requirements.md`
for the full dependency list.

On **Linux** it's just the same, with the `.sh` twins:

```
./AUTO-SETUP.sh     # first time: Python + pip packages + system deps + Piper voices
./run.sh
./stop.sh
```

`AUTO-SETUP.sh` uses `sudo apt` (Debian/Ubuntu), falls back to `dnf` (Fedora)
or `pacman` (Arch) where available, and skips anything already present. It
installs the system packages the tools want (PortAudio for TTS playback,
xclip for the clipboard, wmctrl for window tools, scrot/ImageMagick for
screenshots) and downloads the Piper voices. Window tools are X11-only
(Wayland compositors manage windows only partially).

It starts the assistant UI at **http://localhost:8081** and opens your browser.
**The model is not loaded at startup** - it loads into RAM/VRAM the first time
you send a message (so just opening the UI costs nothing), and the header shows
`ONLINE / IDLE` → `LOADING MODEL...` → `ONLINE / LOADED`. EJECT stops it again,
and the next message brings it back. On Windows the model server is
`prism-llama`'s `llama-server.exe`; on Linux any `llama-server` binary found on
`PATH` is used (set `PC_LLAMA_SERVER` to point at one explicitly). To stop the
model server manually, double-click `stop.cmd` on Windows or run `./stop.sh` on
Linux.

A second, cleaner chat UI is available at **http://localhost:8081/chat**
(GPT-style look, same backend and features: tools, thinking, streaming, plan/build
mode, effort, attachments, mic, workdir, EJECT and Blender status). The two UIs
keep separate chat histories (the GPT UI stores its history in the browser).

Once the UI is open, try a few commands:

```
Create a Python script in the workspace that renames files with regex
Open the README in the workspace and summarize it
Screenshot the current window and describe what you see
Check the latest news about Nvidia and pick three highlights
List the .py files in this folder with their sizes
```

While Bonsai is replying you can keep typing: **press Enter and the message is
queued** - it appears in the conversation straight away with a pulsing
**QUEUED · waiting for the current reply** badge, and is sent automatically as
soon as the current reply ends (order is kept, and you can queue several). The
button turns into **STOP · 2 queued**, so you always see the backlog; clicking
it aborts the current reply, after which the queue starts. Starting a new chat
clears it, and a queued message survives a page reload.

## 2. Requirements

| Requirement | Why | Verdict |
|---|---|---|
| Windows or Linux | the app is a Python script; on Windows launching apps uses Windows URIs/commands, on Linux xdg-open/PATH binaries | required |
| Python 3.10+ | runs `bonsai_web.py` | required |
| GPU with ~7-8 GB VRAM | fast inference (tested on an RTX 4070, ~48 tok/s); CPU-only works, just slower | recommended |
| `prism-llama` `llama-server` | the PrismML llama.cpp fork with ternary kernels - stock llama.cpp **can't** run Bonsai 2 files; Windows exe in `%LOCALAPPDATA%\Programs\prism-llama\`, Linux any `llama-server` on `PATH` (or `PC_LLAMA_SERVER`) | required |
| `Pillow` / `pyautogui` / `pyperclip` | power `take_screenshot`, `control_input` and `clipboard` (Linux needs an X11 session + scrot/ImageMagick and xclip for the last two) | recommended (tools report a clear error if missing) |
| `pywin32` / `psutil` | power the `window_list` / `window_action` tools (Linux uses `wmctrl` instead; pywin32 is Windows-only) | recommended (installed here; tools report a clear error if missing) |
| `requests` / `websocket-client` | power the `api_call` / `ws_test` tools | recommended (installed here) |
| `piper-tts` / `onnxruntime` / `sounddevice` | power the `tts_speak` / `tts_voices` tools (local neural TTS + direct playback; Linux falls back to `paplay`/`aplay`/`ffplay`) | recommended; voice models in the project's `piper\` (fetch with `python piper\download_voices.py` or `python3 piper/download_voices.py`) |
| **Blender + MCP addon** | for the Blender tools (§4) | optional; `mcp-for-blender` Python package + the bundled addon enabled in Blender (Edit > Preferences > Add-ons > "MCP for Blender") |
| Docker / Hyper3D / sketchfab accounts | optional extra Blender *content* tools | not bundled here - only the core Blender tools ship |

## 3. Capabilities

| Capability | Details |
|---|---|
| **Multi-turn chat** | conversational assistant powered by a local 27B model (Bonsai 2, ternary 2-bit quantization) |
| **Model selector** | swap the active AI model from the header dropdown - pick another local `.gguf` (Bonsai restarts the local model server with it) or an external OpenAI-compatible endpoint (LM Studio, Ollama, another port). Add models with the **+** button; see §8 |
| **Live reasoning** | shows its chain of thought in real time, streaming as it is produced |
| **Tool calls** | decides autonomously when to use a tool and surfaces every call (see §4) |
| **Vision + screen capture** | understands attached images, can `take_screenshot` the live screen, and `screenshot_window` captures just the window it needs - it actually sees what's displayed (apps, error dialogs, terminal output) |
| **PC control** | `control_input` moves the mouse, clicks, drags, scrolls and types like a human; `click_text` clicks a control by its visible label (no pixel guessing) |
| **Waits instead of polling** | `wait_for` blocks until a file/download, port, URL, process, window or on-screen text is ready - so no more screenshot loops |
| **Clipboard as a channel** | `copy_to_clipboard` / `paste_from_clipboard` with type hints, including pasting an image from another app straight into the conversation |
| **Downloads that finish** | every download runs in the background with a live **DOWNLOADS** panel (bytes, %, speed, ETA, pause / resume / cancel / retry) - a 5 GB file no longer freezes the chat. One file can be split across up to 4 connections, throttled, or left running after the reply ends. `download_file` streams to disk with retries and resume; `download_batch` grabs many files (or every link on a page) at once; `download_authed` fetches what needs a token or cookie (secrets redacted); `download_page` saves a page to work offline; `download_verify` checks sha256/size and reports `FAILED CHECK`; `download_media` pulls video/audio/subs via yt-dlp |
| **Shell + sandbox** | `run_command` executes real shell commands - PowerShell/cmd on Windows, `sh` on Linux (configurable timeout, up to 600s); `run_code` runs snippets in an isolated temp folder (auto-detects what's installed on the PC: Python + Node by default, plus Go/Lua/PHP/Ruby/Perl/Bash when present; timeouts up to 600s) |
| **Asks you questions** | `ask_user` pauses and asks you a question (with optional clickable options) exactly like a human would - just like opencode |
| **Todo list** | `todo_write` shows a live, visible checklist on the left panel as it works through longer tasks |
| **Live HTML preview** | any `.html` page Bonsai writes (or `preview_html` explicitly) takes over the **center stage** - the arc reactor slides away and the page is shown full-panel, with a name and an **X** to bring the reactor back. Served from this PC, so JavaScript and local assets (CSS/JS/images next to the page) work. In the GPT-style chat (`/chat`) the same page opens as a full-screen lightbox |
| **Screenshots appear in the chat** | every `take_screenshot` / `screenshot_window` / `click_text` result is drawn as a labelled card (dimensions + saved path, click to open full size) **above** the collapsed tool log - no need to expand anything |
| **Not locked to one folder** | with the default `ASK` scope Bonsai can read, write and **search the whole PC** (`C:\`, `/home/...`), and you approve each new folder - so "find my tax PDF" works outside the workspace too. The **PATH SCOPE** control in the sidebar switches between `WORKSPACE` / `ASK` / `SYSTEM` (§4.1) |
| **Real system meters** | live CPU / RAM / GPU usage under the reactor, read from `psutil` + `nvidia-smi` instead of placeholder numbers |
| **Window management** | `window_list` lists every open window (title, process, PID) and `window_action` brings one to the front, maximizes, minimizes or restores it - so Bonsai can switch apps before acting |
| **Docker / containers** | drives the Docker CLI from chat (list images/containers, start, stop, restart, tail logs, run a command inside) - works with Docker Desktop |
| **API client** | `api_call` speaks REST/GraphQL (any HTTP method, JSON or raw bodies, custom headers) and `ws_test` connects to WebSocket endpoints, sends and collects replies |
| **Scheduled tasks** | `schedule_task` (once / every N seconds / 5-field cron) runs shell commands in the background while the PC is on; they survive a restart and report back as toasts (§7) |
| **Speaks out loud** | `tts_speak` reads text aloud with the local neural Piper engine (English + Romanian voices) and saves the WAV in the workspace; no cloud, no Windows voices, no media player - streams straight to the speakers with `sounddevice`. **Off by default** - flip the **TTS** header button to let Bonsai speak |
| **Thinking effort** | **THINK: OFF / LOW / MED / HIGH** selector in the composer controls how deep Bonsai reasons (maps to `enable_thinking` / `reasoning_effort`) |
| **Never waits idle** | Enter while a reply is streaming and your message is queued with a **QUEUED** badge, then sent automatically - no cancelling, no waiting for the UI (§1) |
| **Live token stats** | real-time think vs. speak time, tokens/second and context usage under the input (see §6) |
| **Always-in-memory** | the model stays loaded (resident) for the whole session - no idle timer ever unloads it, so a reply never stalls because of a pause (see §6) |
| **Persistent history** | conversations saved in the browser and on disk; survive server restarts (see §7) |
| **Plan / Build modes** | *Plan* is read-only (inspection only), *Build* grants full tool access |
| **Blender control** | when Blender is open, Bonsai can create, move and edit 3D scenes inside it and screenshot the viewport - you keep the mouse (no exodus) |
| **Fully offline** | model, inference and UI all run locally, with no cloud dependency |

## 4. Tool execution (how it works)

Bonsai is a **tool-calling agent**. Tool calls are effectively **unlimited** -
the model calls tools for as long as it needs and the conversation context is
the natural stop (no artificial per-reply cap). Each tool runs, its result is
folded back into the conversation, and the model continues. Every
call shows up live in the chat as a chip (green = ok, red = error) with a
full log; repeated back-to-back calls of the same tool collapse into one chip
(e.g. `⚙️ web_search ×8`) so the console stays clean. A closing answer is
always produced at the end.

| Tool | What it does | Notes |
|---|---|---|
| `launch_or_open` | Opens apps, websites, files and settings by name | Notepad, Steam, Chrome, Spotify, YouTube, workspace files, system settings... |
| `list_dir` / `read_file` / `grep` / `write_file` / `edit_file` | Work on your files | the **workspace folder** by default; with `PATH SCOPE: ASK` Bonsai may also use an absolute path anywhere on the PC and you approve it per folder (§4.1). `grep` can sweep the whole disk |
| `take_screenshot` | Captures the screen (or a region) and **feeds the image to Bonsai's eyes** | also saves a PNG in the workspace; you see it as a card in the chat |
| `screenshot_window` | Captures **one named window** (title substring / process / PID) and feeds it to Bonsai's eyes | no more guesswork between `window_list` and a full-screen grab; a minimized window is restored first, and the list of open windows is suggested if nothing matches |
| `wait_for` | Waits until something is true instead of polling screenshots | `kind`: `file` (appears, or reaches `min_bytes` - a download finishing), `port` (starts listening), `url` (HTTP 2xx/3xx), `process` (app launched), `window` (title appears), `text` (string in a file), `screen_text` (visible on screen, needs OCR); `must_disappear` waits for the opposite; reports `timed_out` + what it last saw instead of erroring |
| `control_input` | Moves the mouse, clicks, double/right-clicks, drags, scrolls, types text, presses keys/hotkeys | screen-pixel coordinates; pair with `take_screenshot` to see the result |
| `click_text` | Clicks a button/link **by the text it shows** - OCR locates it, typos are tolerated, and its centre is clicked | scope it with `window=` so background windows are not scanned; `dry_run` reports what would be clicked without clicking; a miss returns close matches as `did_you_mean`. Needs OCR (pytesseract + Tesseract) |
| `clipboard` | Reads (`get`) or writes (`set`) the system clipboard | |
| `copy_to_clipboard` / `paste_from_clipboard` | First-class clipboard channel with type hints | `copy` validates `format=json` before copying; `paste` with `format=auto` also picks up a copied **picture** and returns it as an image Bonsai can see (`text`/`json`/`image`) |
| `download_file` | One public http(s) URL → a file in the workspace | streamed straight to disk (never buffers the whole file), `max_mb` cap, `retries`, resumes a `.part` file, keeps the server's `Content-Disposition` filename, and shows a preview for text |
| `download_status` | Asks what the downloads are doing | pass a job id, or call it bare to list everything queued / running / paused with bytes, speed and ETA; the panel is the same data, live |
| `download_batch` | **Many** files in one call | give `urls`, or point `from_page` at a page and let it collect the links (`match` filters them); 4 at a time by default, one folder, per-file ok/failed report |
| `download_authed` | A file behind a login / token | `bearer`, `cookie` or full `headers`; the secret values are **redacted everywhere** - not in the result, not in the chat history, not in the tool log |
| `download_page` | Save a web page **offline** | downloads the HTML plus its images/CSS/JS/fonts, rewrites the links to the local copies and writes an `index.html` that works with no internet |
| `download_verify` | A file that must be **provably** correct | big downloads continue with HTTP resume, broken connections retry, then it checks `sha256` and/or the exact byte size and says `FAILED CHECK` instead of pretending |
| `download_media` | Video / audio / subtitles | via `yt-dlp` (`pip install yt-dlp`): audio-only as mp3, subtitles, thumbnail, playlist, or cookies from your browser for private videos |
| `connections` / `throttle_kbps` / `background` (on every download tool) | Speed and control knobs | `connections: 1-4` splits one big file across parallel range requests (a large speed-up on slow single-stream hosts; ignored for small files and when the server refuses ranges). `throttle_kbps` caps one transfer so it leaves bandwidth free. `background: true` returns a job id at once and the transfer keeps going |
| `archive` | Creates / extracts zip, tar, tar.gz, tgz archives | unpack or pack inside the workspace |
| `ask_user` | Asks you a question and **waits for your answer** (options or free text) | pauses its work like opencode's question skill |
| `todo_write` | Replaces the visible TODO checklist (pending / in_progress / completed) | shown live on the left panel |
| `window_list` / `window_action` | Lists open windows (title, process, PID); brings one to front / maximizes / minimizes / restores | needs `pywin32` + `psutil` (both installed here) |
| `docker_ps` / `docker_images` / `docker_start` / `docker_stop` / `docker_restart` / `docker_logs` / `docker_exec` | Manage Docker containers and images | shells out to the `docker` CLI (Docker Desktop); clean error if not installed |
| `api_call` | Any HTTP method to REST or GraphQL APIs, JSON or raw body, custom headers | needs `requests`; returns status, headers, elapsed ms and body |
| `ws_test` | Connects to a `ws://`/`wss://` endpoint, optionally sends a message, collects replies | needs `websocket-client` |
| `schedule_task` / `list_schedules` / `unschedule_task` | Run a shell command later: once, every N seconds, or by 5-field cron | see §7 for persistence, the run toasts and completed one-shots |
| `tts_voices` / `tts_speak` | Lists local Piper voices; speaks text aloud and saves the WAV (`out\tts\`) | needs `piper-tts` + `onnxruntime` + `sounddevice`; voice models live in the project's `piper\` - run `python piper\download_voices.py` once; `tts_speak` only works after the **TTS** header button is turned on |
| `web_search` / `web_fetch` | Look up current information online when it's not sure | `web_search` returns **structured** results (title, url, domain, snippet, source) plus `did_you_mean` and `related_searches`, so a typo like `serach` recovers in one call; `action="suggest"` is the cheap autocomplete-only check. `web_fetch` reads a page |
| `run_command` | Runs a real shell command (cmd/PowerShell on Windows, `sh` on Linux) | timeout default 45s, configurable up to 600s, output capped to ~8 KB |
| `run_code` | Runs a snippet in any installed language (see §2) | isolated temp folder, deleted afterwards; timeout default 30s, up to 600s; ~8 KB output cap |
| `preview_html` | Live-preview an `.html` page from the workspace: it takes over the center stage (reactor hidden, **X** restores it), or opens as a lightbox in `/chat` | also auto-suggested when `write_file` targets an `.html`/`.htm` file (returns a `preview_url`) |
| `get_scene_info` | Lists the open Blender scene: objects, types, locations (Mesh, Camera, Light, ...) | requires Blender running with the *MCP for Blender* addon enabled |
| `get_object_info` | Details on one object (location, rotation, scale, ...) | |
| `execute_blender_code` | Runs real Python inside Blender's `bpy` context | add cubes, move them, re-parent, set materials - always step by step |
| `get_viewport_screenshot` | Captures the Blender viewport and **feeds it to Bonsai's eyes** | shown as a thumbnail; the model confirms its work visually |
| `bpy_api_lookup` / `describe_node_type` / `export_scene` | look up the exact `bpy` API, inspect one shader-node type, export the scene | Bonsai reads real API docs so its code actually runs |

The **Blender** tools run through `mcp-for-blender` (a stdio MCP server that talks to
the addon on `localhost:9876`). Bonsai keeps full control of the tool loop and
reasoning - only the actual Blender commands are delegated. A status indicator in
the header shows the connection: the Blender logo with a **green** dot
(*connected*), **yellow** (*addon off*) or **red** (*not connected*); hover it
for the detail.

The **workspace** is the default area for the file tools, and (with
`PATH SCOPE: ASK`) not a wall - see §4.1. You can pick any folder from the UI
(📁 button in the header) with a native folder dialog, or set `PC_WORKDIR`
(see §8).

### 4.1 Path scope - reaching the rest of the PC

The **PATH SCOPE** button in the sidebar (classic UI: under the TODO list;
GPT UI: above the footer links) has three modes. Click it to cycle:

| Mode | Behaviour |
|---|---|
| `WORKSPACE` | hard sandbox - only the workspace, no prompts (the old behaviour) |
| `ASK` *(default)* | anything outside the workspace raises a **PERMISSION NEEDED** dialog: **ALLOW FOR THIS CONV** · **ALLOW ONCE** · **DENY** |
| `SYSTEM` | trusted - the whole PC is reachable, no prompts at all |

- **ALLOW FOR THIS CONV** remembers that folder (and everything under it) for
  the rest of the conversation, so a second look at the same place is silent.
  Starting a new chat clears the list.
- **ALLOW ONCE** lets that single tool call through, then asks again.
- **DENY** refuses the call, and Bonsai is told so - it won't keep hammering you
  with the same request, and it should ask you what to do instead.
- Every approved/denied folder is listed under the button with an **×** to
  revoke it, plus **CLEAR APPROVALS** to forget them all.
- Applies to `list_dir`, `read_file`, `write_file`, `edit_file`, `grep`,
  `archive`, `download_file` and to the HTML preview routes. `grep` with
  a path like `C:\Users` or `C:\` is how Bonsai searches the entire machine
  (heavy system folders such as `Windows`, `Program Files` and `node_modules` are
  skipped, and a scan stops after 40 000 files / 45 s and says so).
- Approvals are asked **only while a reply is streaming in the UI** - a tool run
  in the background (API, script) gets a clean "no one is around to approve it"
  error instead of blocking forever. Set `PC_PATH_POLICY=workspace|ask|system`
  to choose the mode at startup.
- `run_command` / `run_code` run a shell and can already reach the whole disk, so
  they are not path-gated - that is the same trust level as giving Bonsai the
  **SYSTEM** scope.

### 4.2 The DOWNLOADS panel

Every download runs as a background job, so a big file no longer freezes the
chat. The **DOWNLOADS** panel (classic UI: under the TODO list; GPT UI: in the
sidebar) refreshes about once a second and shows, per transfer:

- a progress bar with **bytes / total**, **%**, current **speed** and **ETA**,
  plus `4 conn` when the file is split across parallel connections and
  `resumed` when it continued an earlier attempt;
- **PAUSE** (releases the socket and keeps the bytes), **RESUME**,
  **CANCEL** (the `.part` file stays on disk) and **RETRY** after a failure;
- **CLEAR FINISHED** to tidy the list, and a toast when something completes.

A few things worth knowing:

- A paused, cancelled or crashed download leaves a `.part` file (plus a small
  `.segments.json` ledger for split downloads). Nothing is ever half-renamed
  into its final name, and the next attempt continues from those bytes.
- `connections: 1-4` splits one file into parallel range requests. It is
  **skipped automatically** when the server does not support ranges or the file
  is small, so it can never make things worse.
- If a download outlasts the tool's `timeout`, the reply says it is still going
  and hands back a job id - the transfer is not killed. `download_status` (or
  the panel) picks it up from there.
- The panel is shared state, so it survives a page refresh; only in-flight
  transfers live in the server's memory and are gone if the server restarts
  (the `.part` files remain, so a retry continues).

## 5. The console - controls & HUD

| Control | What it does |
|---|---|
| **SEND / STOP** | Submits your message; while Bonsai is replying it becomes **STOP** to abort the run |
| **Enter while busy** | queues the message instead of cancelling it (see §1); the backlog is shown on the button, e.g. `STOP · 2 queued` |
| **THINK: OFF / LOW / MED / HIGH** | Selects how deeply Bonsai reasons (`enable_thinking` / `reasoning_effort`); higher = deeper reasoning, slower replies. Default MED |
| **TODO panel** | Live checklist on the left panel, updated by `todo_write` as longer tasks progress |
| **Workspace (folder icon)** | Choose/open the working folder for the file tools |
| **PATH SCOPE** | `WORKSPACE` / `ASK` / `SYSTEM` - how far the file tools may reach outside the workspace, and lists the folders you approved or denied (§4.1) |
| **Model dropdown (+ ⚙)** | Pick the active model, add one, and - via the gear - set its runtime options (see §8) |
| **Model status ball** | **Red** = no model loaded · **orange→green** cycling = loading into RAM/VRAM · **green** = loaded and resident |
| **Blender icon + dot** | The Blender logo with a dot beside it: **green** = connected, **yellow** = addon off, **red** = not connected. Hover for the full reason |
| **EJECT** | Stops the model server **now** and frees its RAM/VRAM; it restarts automatically on the next message |
| **TTS** | Toggles Piper text-to-speech. **Off by default** - click to let Bonsai speak replies aloud (state resets on server restart) |
| **Plan / Build** | Toggle mode - *Plan* read-only, *Build* full tool access |
| **Mic (🎙)** | microphone voice input (speech-to-text) for your messages |
| **NEW CHAT** | Start a fresh conversation |

The **arc reactor** in the center panel shows both the mode and what Bonsai is
doing:

- **Rainbow** (slow colour cycle) - idle in **Build** mode
- **Purple** - idle in **Plan** mode (read-only, no tools)
- **Yellow** - thinking / processing the command
- **Green** - executing a tool call

The line under it mirrors the state, so it never claims to be waiting while
Bonsai is busy: *"Awaiting your command"* appears only when idle; it switches to
*"Thinking it through..."* / *"Working on your PC..."* / *"Read-only - no tools
will run"* as needed.

The **CPU / RAM / GPU** meters under the reactor are **real readings**, not
decoration: CPU and RAM come from `psutil`, GPU utilisation from `nvidia-smi`
(sampled every 1.5 s on a dedicated thread, cached, and shown as `n/a` when
`nvidia-smi` isn't available - e.g. no NVIDIA GPU).

Both UIs use a dark theme. The classic HUD console (`/`) has a clean near-black
palette with a single accent colour; the GPT-style UI (`/chat`) has its own
light/dark toggle (the sun/moon button in the top bar).

## 6. Live token stats & memory relief

**Real-time stats line** under the input while Bonsai works:

```
THINK 2.8s · SPEAK 0.4s · 45.3 tok/s · tokens 138 · ctx 1638/32768
```

- **THINK** - time spent reasoning before the first output token.
- **SPEAK** - time spent generating the reply.
- **tok/s** - average generation speed (RTX 4070 ≈ 44-48).
- **ctx** - context slots used / total for the model you actually selected, refreshed live.

A compact gold chip with the same numbers is saved onto each reply so the
stats survive reloads.

**Always resident, eject when you want** - the model stays fully loaded for as
long as the app (and `llama-server`) are running, so there is no idle timer
that could unload it mid-conversation or right before your next message. When
you *want* the memory back (e.g. before running a big game), click **EJECT** in
the header and the model unloads immediately - it simply loads again on the
next message, no restart needed.

## 7. Chat history

The two UIs keep **separate** histories (as designed - the classic HUD console
and the GPT-style UI never mix):

| UI | Browser (`localStorage`) | Disk |
|---|---|---|
| Classic (`/`) | `jarvis_chats` - instant load | `%APPDATA%\BonsaiAsistent\chats.json` (Linux: `$XDG_CONFIG_HOME`/`~/.config`) |
| GPT (`/chat`) | `bonsai_gpt_chats` - the **only** copy | - |

How it behaves:

- **Saved on every change** - after each reply, when you start/delete a chat,
  and when the title is first set. The title becomes your first message
  (truncated to 34 chars, plus `+ files` / `+ images`).
- **Capped at the newest 200 conversations** in both places.
- **The disk copy is the durable one** for the classic UI: the browser copy is
  only a fast local mirror/fallback, and the two are *merged* on startup (the
  richer copy of a chat wins) so a failed or stale save can never hide newer
  messages. If the disk file is unreadable, `chats.json.bak` is used instead.
- **Writes are atomic** - the file is written to `chats.json.tmp` and swapped in,
  keeping the previous version as `chats.json.bak`, so a crash mid-write cannot
  truncate your history. Malformed entries are dropped instead of poisoning the
  file.
- **Attachments are size-capped in storage.** Tool screenshots (base64 previews)
  are never persisted. Large images you attach are kept for the 5 most recent
  conversations in the classic UI's disk copy; older ones are replaced with a
  placeholder so history cannot grow without limit. Images are still sent to the
  model for the current conversation.
- **GPT UI history is browser-only.** Clearing site data, using another browser
  or a private window loses it - copy anything you want to keep. The classic
  UI is the one that survives a wiped browser.
- **Scheduled tasks are persisted separately** in `schedules.json` in the same
  BONSAI folder (`%APPDATA%\BonsaiAsistent` on Windows, `$XDG_CONFIG_HOME` or
  `~/.config` on Linux). On startup Bonsai reloads it: `interval` and `cron`
  jobs get their next future slot, and a `once` job whose moment passed while
  Bonsai was closed runs immediately. Deleting a task (or clearing the file)
  removes it.

## 8. Configuration

All settings are optional. Unless an environment variable is set, Bonsai uses
the defaults defined at the top of `bonsai_web.py` (which resolve to the
project's local folders).

| Variable | Purpose | When to set it |
|---|---|---|
| `BONSAI_DIR` | Folder containing the `.gguf` model files | only if the model files live somewhere other than the project folder |
| `PC_WORKDIR` | The workspace folder the file tools use | to point file access at a specific folder by default |
| `PC_PATH_POLICY` | Default path scope: `workspace`, `ask` (default) or `system` | to start in a fixed mode instead of `ASK` (§4.1) |
| `PC_MAX_DOWNLOAD_MB` | Optional ceiling for one downloaded file (default: **no limit**) | set it only if you want a hard stop; per-call `max_mb` overrides it |
| `BONSAI_DL_JOBS` | How many downloads may run at the same time, default 4 | lower it on a slow connection or a laptop that must stay responsive |
| `PC_LLAMA_SERVER` | Path to the `llama-server` binary used for local models | only if it isn't on `PATH` (Linux) or in the default PrismML location (Windows) |
| `PC_PIPER_DIR` | Folder holding the Piper voice models | only if the voices live outside the project's `piper\` |

The workspace can also be changed at runtime from the UI (📁 button in the
header) - this is the recommended way.

### Choosing the AI model

The header dropdown (next to **TTS**) selects which model answers, and the
**+** button next to it opens a small dialog that uses the normal Windows/ Linux
file browser.

**Dropping models into the folder is enough.** On startup BONSAI scans the
project folder and a `models/` subfolder and registers every `*.gguf` it finds
(`mmproj` files are not listed as models, they are attached to their model).
So if you copy `Qwen3.8.gguf` next to the Bonsai weights, it simply appears in
the dropdown - nothing to configure.

Use the **+** button when models live somewhere else:

| Option | What it does |
|---|---|
| **Model file (.gguf)** | opens a file browser filtered to `*.gguf` and adds that one model |
| **Model folder** | opens a folder browser and adds **every** `.gguf` in that folder *and its subfolders* (already-known files are skipped) |
| **Vision projector** | pick a local model and **Browse…** its `mmproj` file, or **Clear** it |
| **API server** | type a base URL + model id instead of a local file |

How each type behaves:

- **Local `.gguf`** entries are served by the bundled `llama-server`. Switching
  between them is also lazy: the old model is stopped immediately (freeing its
  VRAM) and the newly selected one loads on your next message - so browsing the
  dropdown never costs a model load, and you can never accidentally be answered
  by the model you just switched away from.
- **Vision projectors (`mmproj`)** turn a text-only model into a seeing one.
  They are auto-matched to their model by **filename prefix** (`Qwen3-8B-…`
  pairs with `Qwen3-8B-mmproj-…`), both at startup and whenever a new file
  appears, so dropping `qwen3.8-mmproj-F16.gguf` next to `Qwen3.8.gguf` is
  enough. Because matching is heuristic, the **+** dialog can also attach or
  clear a projector explicitly. Models with a working projector are marked
  `· vision` in the dropdown, and the header shows `VISION: mmproj ON/OFF` for
  the **active** model. Without a projector the model still works, but it
  cannot read images or `take_screenshot`.
- **API** entries point at any OpenAI-compatible server (LM Studio, Ollama,
  vLLM, another llama.cpp instance...). Switching is instant - the app just
  sends requests to that base URL and model id instead. Projectors do not apply
  to them (the server decides what it supports).

The list and the active choice are stored in `models.json` (git-ignored, created
on first change). A cloud icon marks API entries, a target icon marks local
ones; a local model whose file has gone missing is shown as `(missing)`. You
must switch away from a model before it can be removed from the list. The
`+` dialog also re-scans the project and `models/` folders, so a newly dropped
model shows up without touching `models.json`.

**Context size and sampling** - the gear (⚙) next to the model dropdown opens
**MODEL SETTINGS** for the currently selected local model:

| Setting | Default | Notes |
|---|---|---|
| **Context size (ctx)** | **32768** | tokens the model can see. Bigger = longer conversations/files, but more VRAM and slower prompt processing |
| **Temperature** | 1.0 | lower = more focused/deterministic, higher = more varied |
| **Top-p** | 0.95 | nucleus sampling cut-off |
| **Top-k** | 20 | candidate limit; 0 disables it |
| **GPU layers (-ngl)** | 99 | how many layers to offload to the GPU. Lower it (e.g. 20) if the model doesn't fit in VRAM |

These are stored **per model** in `models.json`, so each model keeps its own
settings. They are llama-server launch flags, so they are applied by starting a
fresh server - never in the middle of a reply. **Save** does that for you: if the
model is running it is stopped, and your next message brings it back with the new
values. **EJECT**, re-clicking the already-selected model, and restarting the app
do the same thing by hand. The context meter in the stats bar follows the saved
value immediately, without waiting for the reload. **Defaults** restores
32768 / 1.0 / 0.95 / 20 / 99, and values are clamped to sane ranges server-side.
They apply to locally-served models; an external API endpoint is configured by
its own server.

If a conversation ever grows past the context window, BONSAI says so in plain
language and names the model and its real context size instead of surfacing a raw
server error. Start a new chat, raise the context size, or drop some long
attachments.

## 9. The AI model

The assistant talks to **Bonsai 2 27B** - a ternary (1.58-bit) 27B-class model
built on a Qwen3 hybrid-attention backbone, ~9x smaller than its FP16 original
at near-parity quality (~5.9-7.2 GB instead of ~54 GB).

| File | What it is | Download |
|---|---|---|
| `Ternary-Bonsai-2-27B-PQ2_0.gguf` | Language model (7.21 GB, ternary g128) | [Hugging Face](https://huggingface.co/prism-ml/Ternary-Bonsai-2-27B-gguf) |
| `Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf` | Vision projector (0.63 GB) - required for image input | same Hugging Face repo (row above) |

Server profile: 32k context, `-ngl 99`, flash-attention, temp 1.0, top-p 0.95,
top-k 20.

Useful links (the model files themselves are in the table above):

- 📚 PrismML model downloads & docs: **https://docs.prismml.com/download/models**
- 🔧 Runtime engine (`llama-server`): **https://github.com/PrismML-Eng/llama.cpp** (stock llama.cpp won't load PQ2_0/PTQ1_0 tensors)
- 🧪 Official demos: https://github.com/PrismML-Eng/Bonsai-demo

## 10. Security & privacy

- Everything runs locally - the browser only talks to `127.0.0.1`.
- File tools are confined to the configured workspace folder **unless you widen
  the path scope** (§4.1): `ASK` asks you per folder, `SYSTEM` trusts Bonsai with
  the whole disk. Every approval is visible in the sidebar and can be revoked.
- Downloads only accept `http(s)` (no `file://`) and stream to disk in chunks, so
  a 5 GB ISO costs the same RAM as a 5 KB file - there is **no size limit** by
  default. Instead of a cap, Bonsai checks the free disk space first and refuses
  cleanly when the file obviously will not fit; `PC_MAX_DOWNLOAD_MB` or a
  per-call `max_mb` can still impose a ceiling. Credentials passed to
  `download_authed` are replaced with `***redacted***` before the call is stored
  in the chat history, the tool log or the server transcript - only the header
  *names* are echoed back.
- `run_code` runs in an isolated temp folder that is deleted afterwards (30s
  timeout by default, up to 600s). Note: it runs as your user on this PC, so
  snippets do have network access - prefer Python's sandbox tools where strict
  isolation matters.
- HTML previews are served only from the workspace: `/preview` serves only
  `.html`/`.htm`, and its companion `/previewfile/…` route serves the page's
  **non-HTML** assets (CSS/JS/images/fonts, with a fixed MIME allow-list) so
  local assets resolve. Both are confined to the workspace, both preview
  `<iframe>`s (the center stage and the `/chat` lightbox) run with
  `sandbox="allow-scripts"` (so a previewed page is in an opaque origin and
  cannot touch the rest of the UI), and the file is never executed on the
  server.
- Chat history lives on your PC only (`%APPDATA%\BonsaiAsistent\chats.json`,
  written atomically with a `.bak` copy).
- Scheduled tasks are stored in `schedules.json` next to it. They only run while
  Bonsai is running, but a one-shot whose moment passed while it was closed runs
  on the next start.
- Don't expose the server to the internet; it's an assistant, not a web service.

## 11. Troubleshooting

| Symptom | Fix |
|---|---|
| First reply after a pause is slow | normal only if `llama-server` was stopped/restarted meanwhile - otherwise the model stays resident for the whole session |
| Model won't start / blank UI | make sure `prism-llama`'s `llama-server` is installed (Windows exe or a binary on `PATH` / `PC_LLAMA_SERVER` on Linux) and `BONSAI_DIR` points at the `.gguf` files |
| Vision doesn't work | the active model needs a vision projector: put a `*mmproj*.gguf` with a matching filename prefix next to the model, or attach one from the **+** dialog |
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
run.bat / run.sh       # the ONE launcher: starts UI + model server (Win / Linux)
stop.cmd / stop.sh     # stops the model server (Win / Linux)
AUTO-SETUP.cmd         # one-shot installer on Windows: Python deps + required-file check
AUTO-SETUP.sh          # same for Linux (apt/dnf/pacman + system packages + Piper voices)
OPTIONALS.cmd          # system-level optionals: Docker/WSL2, Tesseract, Blender MCP (Windows)
requirements.md        # full dependency list (required vs optional)
requirements.txt       # pip installs for the extended tools (pywin32 is Windows-only)
tools/                 # optional helper scripts (screenshot, clipboard, scraper, OCR) - the same capabilities are also built into bonsai_web.py
tools.json             # tool-call spec reference (auto-generated from code)
gpt_ui.html            # standalone copy of the /chat UI (extracted for reference)
instructions.txt       # quick-start guide (English)
piper/                 # Piper TTS: tts.py + download_voices.py; voice .onnx models are git-ignored (fetch with `python piper\download_voices.py`)
*.gguf                 # the model weights (local only, not in git)
.gitattributes         # LF/CRLF normalization (shell scripts stay LF everywhere)
README.md              # this file
```

The repo intentionally does **not** contain the model weights (git-ignored;
download from §9) nor the Piper voice models (fetch with
`python piper\download_voices.py` / `python3 piper/download_voices.py`).

### Platform notes (Windows vs Linux)

- `launch_or_open` opens apps either via the Windows shell (`os.startfile`,
  URIs, Start-menu AppIDs) or on Linux via `xdg-open`/`gio` and binaries found
  on `PATH`; workspace files open with the system default handler either way.
- `window_list` / `window_action` use win32 on Windows and `wmctrl` on Linux
  (X11 sessions); install `wmctrl` for them.
- `take_screenshot` needs a real X11 desktop on Linux (PIL/ImageGrab or
  scrot/ImageMagick fallback).
- `run_code` picks up whatever runtimes are installed (python, node, go, lua,
  php, ruby, perl, bash).
- Paths are portable: `BONSAI_DIR` / `PC_WORKDIR` / `PC_LLAMA_SERVER` env vars
  override the per-platform defaults (chat history lives under `%APPDATA%` on
  Windows and `$XDG_CONFIG_HOME`/`~/.config` on Linux).

## 13. License

Apache-2.0 (same as the Bonsai 2 model - see `LICENSE` in the model package).