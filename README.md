# Bonsai PC Assistant

A local-first AI assistant that runs on your own machine, can see your screen, and
actually does things — files, screenshots, windows, clipboard, browser automation,
Docker, Blender, and image generation — all through tool calls rather than
describing how it would do them.

One Python file serves the UI, the HTTP API and the agent loop. No build step, no
bundler, no framework install.

---

## Table of contents

1. [What it does](#1-what-it-does)
2. [Quick start](#2-quick-start)
3. [Setup, step by step](#3-setup-step-by-step)
4. [Configuration files](#4-configuration-files)
5. [Choosing a model](#5-choosing-a-model)
6. [The optional extras](#6-the-optional-extras)
7. [How a tool call happens](#7-how-a-tool-call-happens)
8. [Path scope and approvals](#8-path-scope-and-approvals)
9. [Image generation](#9-image-generation)
10. [Web search](#10-web-search)
11. [Where things are stored](#11-where-things-are-stored)
12. [Troubleshooting](#12-troubleshooting)
13. [Repository layout](#13-repository-layout)
14. [Platform notes](#14-platform-notes)
15. [License](#15-license)

---

## 1. What it does

**Sees the screen.** Point it at a window or the whole desktop and it looks at the
result before answering. Screenshots go into the conversation as real image
messages, not as text descriptions.

**Acts on the machine.** It has tools for reading and writing files, searching
across the PC, running commands, driving the mouse and keyboard, managing
windows, using the clipboard, fetching and scraping web pages, calling HTTP APIs,
speaking aloud with local neural TTS, talking to Docker, editing Blender scenes,
and generating images.

**Two ways to reach it.** A browser UI at `http://127.0.0.1:8081`, and a plain
HTTP API.

**Keeps your history.** Conversations are saved and can be reopened. Images in a
chat — ones you attach, ones it generates, ones it receives from a tool — stay in
that conversation and come back after a reload.

**Runs the model locally or not.** A local GGUF model through llama.cpp, or any
OpenAI-compatible hosted endpoint. Both are usable at the same time; you pick per
message.

---

## 2. Quick start

The short version, if you already have Python and a model:

```bash
git clone https://github.com/IsGaraa/BonsaiAsistent
cd BonsaiAsistent
python bonsai_web.py
```

Then open **http://127.0.0.1:8081**.

That is genuinely all that is required. Python's standard library is enough — every
optional package only unlocks extra tools, and any tool whose package is missing
reports that clearly instead of failing silently.

To go further — pick and choose your extras — use the setup menu instead:

| | |
|---|---|
| **Windows** | `AUTO-SETUP.cmd` |
| **Linux** | `./AUTO-SETUP.sh` |

---

## 3. Setup, step by step

Both setup scripts are **menus, not scripts**. Nothing happens until you choose it.

### The menu

```
==============================================================================
  BONSAI  -  choose what to set up
==============================================================================

  REQUIRED
    1  Python itself
    2  Python packages
    8  Verify the install

  OPTIONAL - pick only what you want
    3  Desktop extras (screenshots, OCR, TTS)
    4  Point Bonsai at llama-server
    5  Check the model files are here
    6  Create API KEYS.txt from the template
    7  Create models.json from the template
    9  Docker Desktop status

  OTHER
    a  Run everything recommended  (1, 2, 4, 5, 8)
    r  Start Bonsai
    x  Quit
```

Type a number to run one step, several numbers separated by spaces to queue
several, or `a` for the recommended set. Steps that are already satisfied say so
instead of redoing them. You can come back as often as you like — nothing is
recorded as "done" against you.

### Running a step without the menu

Both scripts accept step numbers as arguments, which is handy from another script
or a terminal:

```bash
AUTO-SETUP.cmd 5          # just check the model files are present
AUTO-SETUP.cmd 2 3        # packages, then desktop extras
AUTO-SETUP.sh a           # everything recommended
```

### What each step does

| # | Step | Why |
|---|---|---|
| 1 | Python itself | Installs Python 3.13 via winget / the system package manager |
| 2 | Python packages | `requirements.txt` + `tools/requirements.txt` |
| 3 | Desktop extras | The optional libraries for screenshots, OCR and local TTS |
| 4 | llama-server | Tells Bonsai where your model server is |
| 5 | Model files | Checks the `.gguf` files are actually here |
| 6 | API keys | Copies the template to `API KEYS.txt` and opens it |
| 7 | models.json | Copies the template to `models.json` |
| 8 | Verify | Imports the app and checks each optional library individually |
| 9 | Docker | Reports whether Docker is installed and its engine is running |

### About step 4 and ternary models

Bonsai 2 is a **ternary** model. Stock llama.cpp cannot read these `.gguf` files.
You need a **PrismML** build of llama.cpp with ternary kernels. If the server
starts and then cannot load the weights, that is why.

If you would rather not deal with that, skip the local model entirely and pick a
hosted one from the dropdown. Everything except local inference keeps working, and
no model download is needed.

---

## 4. Configuration files

Two files are yours to edit. **Neither is required** — the app builds a working
configuration without them.

### `API KEYS.txt` — your secrets

Git-ignored, plaintext, and never read by anything but the code that signs a
request. Nothing in it is sent to the browser, printed in the console, or written
into a chat.

A committed template ships with the repo so you know every field:

| file | committed | holds |
|---|---|---|
| `API KEYS.example.txt` | **yes** | every provider, all commented out, no values |
| `API KEYS.txt` | **no** | your actual keys |

Create yours from the menu (step 6) or by hand:

```bash
copy "API KEYS.example.txt" "API KEYS.txt"     # Windows
cp API KEYS.example.txt "API KEYS.txt"         # Linux
```

Then uncomment the line you need:

```
OPENAI_API_KEY=sk-proj-...
GEMINI_API_KEY=AIzaSy-...
```

### `models.json` — which models, and how

Git-ignored, because it holds absolute paths from your machine.

| file | committed | holds |
|---|---|---|
| `models.example.json` | **yes** | the schema, with placeholder paths |
| `models.json` | **no** | your actual model list |

**You probably do not need this file.** Without it, Bonsai finds every `.gguf` in
the project folder by itself, pairs a vision projector to a text model when the
filenames match, and drops any local entry whose file is not actually present.

Reach for it when you want to:

- set a context window or port per model
- change sampling defaults per model (`cfg`)
- add a hosted model without going through the UI
- hide a tool from a model that cannot use it (`omit`)

Shape:

```jsonc
{
  "active": "local-bonsai2",
  "models": [
    {
      "id": "local-bonsai2",
      "label": "Bonsai 2 27B (local, ternary)",
      "type": "local",
      "path": "C:\\path\\to\\Ternary-Bonsai-2-27B-PQ2_0.gguf",
      "mmproj": "C:\\path\\to\\Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf",
      "ctx": 32768,
      "port": 8080,
      "cfg": { "ctx": 20000, "temp": 1.0, "top_p": 0.95, "top_k": 20, "ngl": 99 }
    },
    {
      "id": "my-hosted-model",
      "label": "Something hosted",
      "type": "api",
      "wire": "chat",
      "base_url": "https://api.example.com/v1",
      "model": "some-model",
      "api_key": "OPENAI_API_KEY",     // the NAME, not the key
      "cfg": { "ctx": 32768 }
    }
  ]
}
```

Note `api_key` is the **name** of an entry in `API KEYS.txt`, never the key
itself. If you put a live key in `models.json` it will be committed the moment
you stop ignoring that file.

---

## 5. Choosing a model

The dropdown at the top of the page, and the **+** button next to it.

**Local (free, private, slow to load).** A `.gguf` in the project folder, served by
llama.cpp. Nothing leaves the machine. Needs a PrismML build for ternary models.

**Hosted (no download, needs a key).** Any OpenAI-compatible `/v1/chat/completions`
endpoint, plus Anthropic-style `/v1/responses`. Add one with **+ → connect to an
API server**, then pick it from the same list.

Switching is per message. The local server is stopped before image generation so
the two do not fight over VRAM.

### The context meter

`CTX` in the composer shows how much of the model's window the conversation is
using, measured against the window that model actually has. It is read from the
server, not a hardcoded default.

---

## 6. The optional extras

None of this is needed to chat. Each is behind a menu item so you only install
what you will use.

`OPTIONALS.cmd` (Windows) presents these as a menu in the same way:

| # | Extra | Size | Notes |
|---|---|---|---|
| 1 | Blender control | small | `mcp-for-blender`, then enable the addon in Blender |
| 2 | Tesseract OCR | ~30 MB | needed by `click_text` / `screen_text` |
| 3 | Docker Desktop | ~600 MB | needs item 4, and a reboot |
| 4 | WSL2 + VM Platform | — | Docker's backend; reboot required |
| 5 | Windows stability | — | long paths, a 48–96 GB page file |
| 6 | Local image generation | ~17 GB | Qwen-Image 2.1 weights |
| 7 | Browser engine | small | Playwright, for screenshots of web pages |

```
==============================================================================
  BONSAI  -  OPTIONAL extras  (none of this is required to chat)
==============================================================================

    1  Blender control             mcp-for-blender        ~small
    2  Tesseract OCR              click_text/screen_text ~30 MB
    3  Docker Desktop             docker_* tools         ~600 MB (needs 4)
    4  WSL2 + VM Platform         Docker's backend       reboot
    5  Windows stability          long paths, page file  admin
    6  Local image generation     Qwen-Image 2.1         ~17 GB
    7  Browser engine             page screenshots       small
    a  Run everything that is missing
    x  Quit
```

On Linux, steps 1–7 of `AUTO-SETUP.sh` cover the same ground with the system
package manager, including `grim` for screenshots on Wayland — which `scrot` and
ImageMagick cannot do.

### Text to speech — Kokoro

Speech is [Kokoro-82M v1.0](https://github.com/hexgrad/kokoro) running locally
through `kokoro-onnx`. Two parts, like the image model:

```bash
pip install kokoro-onnx          # code; pulls espeakng-loader + phonemizer
python kokoro\download_model.py  # weights, ~340 MB, once
```

`--model int8` fetches a 109 MB quantised model instead of the 310 MB fp32 one,
`--model fp16` a 156 MB one. The engine loads once and stays resident, so the
second reply to speak costs about a second rather than a reload.

**English only.** The voices file ships styles for a dozen languages; this app
offers the 28 American and British ones (`af_*`, `am_*`, `bf_*`, `bm_*`).
**There is no Romanian** — Kokoro publishes no `ro_` styles, so the `ro_RO-*`
voices that used to ship with the project are gone with no replacement.

| | |
|---|---|
| **Output** | 24 kHz mono WAV, written to `out\tts\` |
| **Voices** | `tts_voices` lists them; default `af_sarah` |
| **Speed** | `tts_speak` takes `speed` from 0.5× to 2× |
| **Playback** | `sounddevice`, falling back to `winsound` / `paplay` / `aplay` / `ffplay` |

**A page for trying voices.** <http://127.0.0.1:8081/kokoro> — type something,
pick a voice, drag the speed, hear it. It always plays in the browser and so
ignores the header TTS toggle.

Speech in the chat is **off by default**: replies only speak after you click the
**TTS** button in the header, and that state resets on restart.

---

## 7. How a tool call happens

1. You send a message.
2. The model replies. If it wants to use a tool, its reply contains a tool call
   instead of text.
3. Bonsai runs the tool, streams a progress card for it, and puts the result back
   into the conversation.
4. The model reads that result and replies again.

That repeats until it stops asking for tools, up to a hard round limit. A round
in the middle can ask you a question — the UI shows the options and the turn
waits for your answer rather than guessing.

Anything that changes a file records a diff, and the page offers to revert it.

---

## 8. Path scope and approvals

The workspace — the default folder — is not a wall. For anything outside it,
Bonsai passes the absolute path and **you are asked to approve**.

- A granted path stays granted for the rest of the conversation.
- The scope can be widened to everything, for this chat or permanently.
- Denials are respected. If a path is refused, the model is told to ask rather
  than try again.

The sidebar shows the current scope (`SCOPE: ASK` in the screenshot at the top of
this file).

---

## 9. Image generation

Text to image, locally, through stable-diffusion.cpp with the **uncensored**
Qwen-Image 2.1 (`UC` in the filename means uncensored).

```bash
python tools/fetch_image_model.py     # ~17 GB, once
```

**How it is configured, and why.** This is the full 7B model, not a distilled
few-step one, so the settings are the ordinary ones:

- **40 steps by default** (`IMAGE_DEFAULT_STEPS`), clamped to 10–80. That is the
  step count the model documents. On a base model, unlike a distilled one, more
  steps genuinely refines the picture rather than oversampling it — so raising
  it is a real option if you want to spend the time.
- **`cfg-scale` is 6.0** (`IMAGE_CFG`). The checkpoint that used to be the
  default was distilled with no classifier-free guidance and wanted 1.0; this one
  does not, and sampling it at 1.0 would quietly ignore your prompt.
- **No explicit sigma schedule.** The engine picks a resolution-dependent flow
  schedule itself for this architecture, and the spacing depends on the pixel
  count. Passing a schedule overrides the one choice it is better placed to make.

A **negative prompt is still ignored** — not because guidance is off, but because
the model's own recipe leaves it empty. Say what you don't want in the prompt.

**Live preview.** A preview is written every 5th step, so a 40-step run shows
steps 5, 10, 15 … 40. This needs a tiled decode (`--vae-tiling`): the preview
decode runs while the diffusion model is still resident, so it has less memory
than the final decode and fails without tiling at anything above about 1.3
megapixels.

**Cost.** At the 1920×1088 base that is roughly **10 minutes per image** —
about 40 s to read the weights, ~9 s a sampling step, and ~24 s per preview
decode. If you would rather have roughly half that, set the base frame to
1536×864; the preview decode is what dominates, and it scales with pixel count.

**Output.** One file per generation, delivered at 1440p on the short edge — so
16:9 lands at 2541×1440, and a 16:9 source that is not a whole number of 32-pixel
steps is 0.7% narrow. The engine's raw output is not kept beside it.

**VRAM.** At 2.09 megapixels this wants the whole card, and the Q8_0 diffusion
weights are 7.07 GiB on their own. A game holding 11 GB will stop it. Close other
GPU work before generating. Dropping the diffusion model to Q4_0 in
`tools/fetch_image_model.py` saves 3.2 GiB at some cost in quality.

---

## 10. Web search

DuckDuckGo, over HTTP POST. This is worth one note because it is not the obvious
choice: **a GET returns a CAPTCHA** — "select all squares containing a duck" —
and the search silently falls through to a fallback that answers a different
question. Posting returns real results.

The model is told when it is being served something irrelevant. Results have to
carry at least half the query's distinctive words, so a search for a game map
cannot be satisfied by a page about logging into Facebook. Anything filtered out
is reported as `dropped_irrelevant` rather than hidden.

DuckDuckGo rate limits by IP, and it answers automated traffic with a CAPTCHA for
a while after that. When it does, Bonsai **stops asking** for 15 minutes rather
than retrying — asking a source that has already refused is what makes the refusal
last — and says so plainly in the result.

---

## 11. Where things are stored

| what | where |
|---|---|
| Chats, history | `%APPDATA%\BonsaiAsistent\` (Linux: `~/.config/BonsaiAsistent/`) |
| Attachments — every image in a chat | `attachments\` beside the chat file |
| Generated images | `out\images\` in the workspace |
| API keys | `API KEYS.txt`, beside `bonsai_web.py` |
| Model list | `models.json`, beside `bonsai_web.py` |

Attachment names are a hash of the file's bytes, so the same image attached twice
is stored once. The chat stores the short `/att/...` reference, never megabytes of
base64 — which is why pictures survive a reload.

Environment variables: `PC_WORKDIR`, `PC_LLAMA_SERVER`, `PC_SD_DIR`,
`BONSAI_CHATS_FILE`, `PC_IMG_PROVIDER`.

---

## 12. Troubleshooting

**Nothing happens when I send a message.**
The page shows an empty bubble when a provider error was swallowed. It should
now name the reason — a rejected API key reads *"The model rejected the API key
for …"*. Check the console window; it has the real traceback.

**"Invalid credential" from a hosted model.**
The key is expired, revoked, or is still the placeholder from the template. Replace
it in `API KEYS.txt`.

**Local model will not load.**
Bonsai 2 is ternary. Stock llama.cpp cannot read it — you need a PrismML build.
Point Bonsai at it with setup step 4, or `set PC_LLAMA_SERVER=<path>`.

**A tool says a package is missing.**
It is optional and not installed. Setup step 3 covers the Python ones; `OPTIONALS`
covers the system ones. The app is designed to stay fully usable without them.

**Screenshot works on Windows, not on Linux/Wayland.**
Install `grim` — setup step 5. `scrot` and ImageMagick cannot capture a Wayland
session.

**Image generation says out of memory.**
Something else is holding VRAM. Close games first. If it persists, drop the base
frame to 1536×864.

**Search returns nothing relevant.**
Check whether it says DuckDuckGo is rate limiting. If so, wait — it clears itself.

**Windows says the path is too long.**
`OPTIONALS` item 5. Long paths need a sign-out to take effect.

**Nothing speaks.**
Speech is off until you click the **TTS** button in the header, and that resets on
restart. If the toggle is on and it is still silent, check `GET /api/tts` — it
reports `installed` and what is `missing`. Two separate installs are involved and
people usually only do the first: `pip install kokoro-onnx` for the code, then
`python kokoro\download_model.py` for the 340 MB of weights.

**A voice name is rejected.**
Kokoro names are like `af_sarah`, not Piper's `en_US-lessac-medium`, and only
English styles exist here. `tts_voices` lists them. A Piper-era name is stripped
of its `.onnx` and then reported as unknown rather than silently accepted.

---

## 13. Repository layout

```
BonsaiAsistent/
├── bonsai_web.py              the whole app - UI, API, agent loop
├── gpt_ui.html                generated snapshot of the served page
│
├── AUTO-SETUP.cmd             Windows setup menu
├── AUTO-SETUP.sh              Linux setup menu
├── OPTIONALS.cmd              Windows optional extras menu
├── run.bat / run.sh           launch
├── stop.cmd / stop.sh         stop
│
├── API KEYS.example.txt       template - committed, no values
├── models.example.json        template - committed, no paths or keys
│
├── tools/                     helper scripts and their requirements
├── kokoro/                    Kokoro TTS (downloaded, ~340 MB)
├── image_models/              Qwen-Image weights (downloaded, ~17 GB)
├── sd.cpp/                    stable-diffusion.cpp (downloaded)
│
├── requirements.txt           every optional Python package
├── requirements.md            which package powers which tool
├── tools.json                 tool registry
│
├── *.gguf                     model weights - NOT in the repo
└── API KEYS.txt               your keys - NOT in the repo
```

**Not in the repo, by design:** model weights (6–7 GB each), image model weights
(~17 GB), the Kokoro TTS weights (~340 MB), the llama.cpp binaries, the inference
engine, `API KEYS.txt`, `models.json`, chat history, and generated images. The
`.gitignore` is explicit about each, and the `.example` files exist so the
*shape* of the two config files is versioned without their contents.

---

## 14. Platform notes

**Windows** is the best-supported path: window management, `pywin32`, the page
file and long-path fixes in `OPTIONALS`.

**Linux** works. Differences to expect:

- `python3-tk` is not in most distro Python builds and every folder picker opens a
  Tk dialog — without it those buttons do nothing, with no error. Setup step 3
  handles it.
- Wayland needs `grim` for screenshots. Setup step 5.
- Window management is `wmctrl`/`xdotool`, not `pywin32`.
- The local model server must be on `PATH`, or `PC_LLAMA_SERVER` set.

Both platforms use the same `bonsai_web.py`. Nothing in it is Windows-only except
the handful of tools that say so.

---

## 15. License

See `LICENSE`. The local model, the image model and the engine each carry their
own licences, and for image generation the terms belong to the checkpoint rather
than to upstream.

The diffusion weights fetched by `tools/fetch_image_model.py` are
`abenzerps/Qwen-Image-2.1-Uncensored-GGUF` — a third-party uncensored fine-tune
of `Qwen/Qwen-Image-2.1`, published under a custom licence (`license: other`),
not the upstream one. Treat it as **not cleared for commercial use** and read the
model card before you rely on it for anything; an "uncensored" checkpoint carries
extra obligations of its own on top of whatever the base model requires. The text
encoder and VAE are Qwen's own and carry Qwen's terms.