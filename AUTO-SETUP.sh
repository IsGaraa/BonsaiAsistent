#!/usr/bin/env bash
# =============================================================================
#  BONSAI - interactive setup for Linux.  Mirrors AUTO-SETUP.cmd.
#
#  Every step is optional and nothing runs unless you pick it. Type the number
#  of a step and press Enter; type several like "1 3 4"; "a" runs everything
#  recommended. Steps already satisfied say so instead of redoing them.
#
#  Can also be driven non-interactively:
#      ./AUTO-SETUP.sh 5          just check the model files
#      ./AUTO-SETUP.sh 2 3        packages, then system libraries
#      ./AUTO-SETUP.sh a          everything recommended
# =============================================================================
cd "$(dirname "$0")" || exit 1

BOLD=; DIM=; GRN=; YLW=; RST=
if [ -t 1 ]; then
    BOLD=$(printf '\033[1m'); DIM=$(printf '\033[2m')
    GRN=$(printf '\033[32m'); YLW=$(printf '\033[33m'); RST=$(printf '\033[0m')
fi

say()  { printf '%s\n' "$*"; }
ok()   { printf '  %s[ok]%s %s\n' "$GRN" "$RST" "$*"; }
warn() { printf '  %s[--]%s %s\n' "$YLW" "$RST" "$*"; }
die()  { printf '  [X] %s\n' "$*"; }

# --- how are we invoking python? -------------------------------------------
PY=""

# Quiet: just look, never prompt. Used at start-up so that probing for python
# cannot swallow an answer that was meant for the menu.
detect_python_quiet() {
    [ -n "$PY" ] && return 0
    if command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else return 1
    fi
    return 0
}

detect_python() {
    if detect_python_quiet; then return 0; fi
    warn "Python 3 not found."
    say ""
    say "  [1] Install it with the system package manager (needs sudo)"
    say "  [b] Back to the menu"
    read -r -p "  Choose [1/b]: " c
    case "$c" in
        1) install_python ;;
        *) return 1 ;;
    esac
}

install_python() {
    say "  Installing python3..."
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt update && sudo apt install -y python3 python3-pip python3-venv
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y python3 python3-pip
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm python python-pip
    else
        die "No supported package manager. Install python3 and re-run."
        return 1
    fi
    detect_python
}

need_py() {
    if ! detect_python; then
        warn "This step needs Python - install it first."
        return 1
    fi
}

# --- steps -----------------------------------------------------------------

step_python() {
    say ""; say "== Python =="
    "$PY" --version
    "$PY" -m pip --version
    ok "python ($PY) works and pip is available."
}

step_packages() {
    need_py || return
    say ""; say "== Python packages (requirements.txt + tools/requirements.txt) =="
    "$PY" -m pip install --upgrade pip
    "$PY" -m pip install -r requirements.txt -r tools/requirements.txt \
        || die "pip install failed - read the error above."
    ok "packages installed."
}

step_tk() {
    need_py || return
    say ""; say "== python3-tk =="
    say "  Every folder/file picker in the app opens a Tk dialog. Without this"
    say "  those buttons do nothing at all, with no error."
    if "$PY" -c "import tkinter" >/dev/null 2>&1; then
        ok "tkinter already works."
        return
    fi
    "$PY" -m pip install --quiet python3-tk 2>/dev/null \
        || "$PY" -m pip install --quiet tk 2>/dev/null \
        || "$PY" -c "import tkinter" >/dev/null 2>&1 \
        || warn "pip could not provide it - use your package manager (e.g. apt install python3-tk)."
}

step_syslibs() {
    say ""; say "== System libraries (audio, clipboard, windows, screenshots, OCR) =="
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt update
        sudo apt install -y portaudio19-dev xclip xsel wl-clipboard wmctrl \
            scrot imagemagick x11-utils python3-tk tesseract-ocr
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y portaudio-devel xclip xsel wl-clipboard wmctrl \
            scrot ImageMagick xorg-x11-utils python3-tkinter tesseract
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm portaudio xclip xsel wl-clipboard wmctrl \
            scrot imagemagick xorg-x11-utils tk tesseract
    else
        warn "Could not detect a package manager. Install by hand:"
        say "      portaudio, xclip / xsel / wl-clipboard, wmctrl,"
        say "      scrot or imagemagick, python3-tk, tesseract-ocr"
        return
    fi
    ok "system libraries installed."
}

step_wayland() {
    say ""; say "== Screenshots on Wayland =="
    if [ -z "${WAYLAND_DISPLAY:-}" ]; then
        say "  No Wayland session here - scrot / ImageMagick will be used."
        return
    fi
    if command -v grim >/dev/null 2>&1; then
        ok "grim is installed (the only thing that can screenshot Wayland)."
        return
    fi
    if command -v apt-get >/dev/null 2>&1; then sudo apt install -y grim
    elif command -v dnf >/dev/null 2>&1; then sudo dnf install -y grim
    elif command -v pacman >/dev/null 2>&1; then sudo pacman -Sy --noconfirm grim
    else warn "Install grim yourself for screenshots on Wayland."; return
    fi
    ok "grim installed."
}

step_llama() {
    say ""; say "== llama-server (the local model server) =="
    if command -v llama-server >/dev/null 2>&1; then
        ok "found: $(command -v llama-server)"
        say "  Note: Bonsai 2 is a ternary model. Stock llama.cpp CANNOT read"
        say "  these .gguf files - you need a PrismML build with ternary"
        say "  kernels. If it starts and then cannot load the weights, that is why."
        return
    fi
    warn "llama-server is not on PATH."
    say ""
    say "  Install a PrismML llama.cpp build (ternary kernels required), put"
    say "  llama-server on PATH, or export PC_LLAMA_SERVER=/full/path/to/llama-server."
    say "  The hosted models in the dropdown work without it."
    read -r -p "  Path to llama-server (blank to skip): " lp
    [ -z "$lp" ] && return
    if [ -x "$lp" ]; then
        ok "using $lp"
        say "  Add this to your shell profile:"
        say "      export PC_LLAMA_SERVER=\"$lp\""
    else
        warn "Not executable at that path."
    fi
}

step_models() {
    say ""; say "== Model files =="
    local missing=0
    if [ -f Ternary-Bonsai-2-27B-PQ2_0.gguf ]; then
        ok "Ternary-Bonsai-2-27B-PQ2_0.gguf"
    else
        warn "Ternary-Bonsai-2-27B-PQ2_0.gguf is missing"; missing=1
    fi
    if [ -f Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf ]; then
        ok "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf"
    else
        warn "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf is missing (needed for screenshots)"
        missing=1
    fi
    if [ "$missing" = "1" ]; then
        say ""
        say "  No .gguf is bundled - they are 6-7 GB each. Put them in this folder,"
        say "  or skip this entirely and use a hosted model from the dropdown."
    fi
}

step_keys() {
    say ""; say "== API keys =="
    if [ -f "API KEYS.txt" ]; then
        ok '"API KEYS.txt" exists - not showing its contents.'
        return
    fi
    warn 'No "API KEYS.txt" yet.'
    say ""
    say "  A template ships with the repo: API KEYS.example.txt. Copy it and"
    say "  fill in whatever keys you have. A hosted model needs one; a local"
    say "  model needs none. The real file is git-ignored - never commit it."
    read -r -p "  Create it from the template? [Y/n] " c
    case "$c" in
        n|N) return ;;
        *) cp "API KEYS.example.txt" "API KEYS.txt" && ok "Created API KEYS.txt - edit it, and keep it out of git." ;;
    esac
}

step_modelsjson() {
    say ""; say "== models.json =="
    if [ -f models.json ]; then
        ok '"models.json" exists - leaving your settings alone.'
        return
    fi
    warn 'No "models.json" yet.'
    say ""
    say "  You do not need one. Without it Bonsai finds every .gguf in this"
    say "  folder itself. You only want this to pre-set context sizes, ports"
    say "  or sampling values."
    read -r -p "  Create it from the template? [y/N] " c
    case "$c" in
        y|Y) cp models.example.json models.json && ok "Created models.json - set the paths to where your .gguf files are." ;;
        *) return ;;
    esac
}

step_voices() {
    need_py || return
    say ""; say "== Piper TTS voices =="
    if ls piper/*.onnx >/dev/null 2>&1; then
        ok "voices already present in ./piper"
        return
    fi
    say "  Downloading one small voice..."
    "$PY" piper/download_voices.py || warn "download_voices.py failed - run it again later."
}

step_verify() {
    need_py || return
    say ""; say "== Verifying the install =="
    if "$PY" -c "import bonsai_web" >/dev/null 2>&1; then
        ok "bonsai_web.py imports cleanly."
    else
        die "bonsai_web.py does not import - run step 2 first."
    fi
    for m in PIL requests websocket; do
        "$PY" -c "import $m" >/dev/null 2>&1 && ok "$m" || warn "$m missing (optional)"
    done
    "$PY" -c "import tkinter" >/dev/null 2>&1 && ok "tkinter (folder pickers)" \
        || warn "tkinter missing - folder pickers will do nothing"
    "$PY" -c "import pytesseract" >/dev/null 2>&1 && ok "pytesseract" \
        || warn "pytesseract missing - click_text / screen_text unavailable"
    "$PY" -c "import piper, onnxruntime, sounddevice" >/dev/null 2>&1 && ok "piper + onnxruntime (local TTS)" \
        || warn "local TTS not installed - Piper voices will not play"
    command -v tesseract >/dev/null 2>&1 && ok "tesseract binary" \
        || warn "tesseract binary missing - install tesseract-ocr"
    say ""
    say "  Lines marked [--] are optional. None of them stop you chatting."
}

step_run() {
    say ""; say "== Starting Bonsai =="
    say "  Running ./run.sh ..."
    chmod +x ./run.sh 2>/dev/null
    ./run.sh
}

# --- the menu --------------------------------------------------------------

ALL="1 2 3 4 5 6 7 8 9"

dispatch() {
    case "$1" in
        1) step_python ;;
        2) step_packages ;;
        3) step_tk ;;
        4) step_syslibs ;;
        5) step_wayland ;;
        6) step_llama ;;
        7) step_models ;;
        8) step_keys ;;
        9) step_modelsjson ;;
        v) step_voices ;;
        c) step_verify ;;
        r) step_run; exit 0 ;;
        *) warn "Unknown step '$1'." ;;
    esac
}

run_pick() {
    for s in $1; do dispatch "$s"; done
}

menu() {
    clear 2>/dev/null || true
    printf '%s\n' "$BOLD"
    say "============================================================================="
    say "  BONSAI  -  choose what to set up"
    say "============================================================================="
    printf '%s' "$RST"
    say ""
    if [ -n "$PY" ]; then say "  Python: $PY"; else say "  Python: not found yet - step 1 handles it"; fi
    say ""
    say "  REQUIRED"
    say "    1  Python itself                    $PY"
    say "    2  Python packages                  pip"
    say "    3  python3-tk (folder pickers)      tkinter"
    say "    c  Verify the install"
    say ""
    say "  OPTIONAL - pick only what you want"
    say "    4  System libraries                 apt/dnf/pacman"
    say "    5  Screenshots on Wayland           grim"
    say "    6  Point Bonsai at llama-server     local model server"
    say "    7  Check the model files are here   .gguf"
    say "    8  Create API KEYS.txt              secrets"
    say "    9  Create models.json               model list"
    say "    v  Piper TTS voices                 ./piper"
    say ""
    say "  OTHER"
    say "    a  Run everything recommended      ($ALL)"
    say "    r  Start Bonsai"
    say "    x  Quit"
    say ""
    read -r -p "  Choose (numbers can be spaced, e.g. 2 3): " pick
    pick=$(printf '%s' "$pick" | head -n1)
    [ -z "$pick" ] && return 0
    case "$pick" in
        x|X) exit 0 ;;
        a|A) run_pick "$ALL" ;;
        r|R) step_run; exit 0 ;;
        *)    run_pick "$pick" ;;
    esac
    say ""
    read -r -p "  Press Enter to go back to the menu..." _
}

# --- entry -----------------------------------------------------------------

detect_python_quiet

if [ "$#" -gt 0 ]; then
    run_pick "$*"
    say ""
    say "  Done. Start Bonsai with: ./run.sh"
    exit 0
fi

while true; do menu; done