#!/usr/bin/env bash
# BONSAI - Linux first-time setup (mirrors AUTO-SETUP.cmd).
set -e
cd "$(dirname "$0")"

PY=""
if command -v python3 >/dev/null 2>&1; then
    PY="python3"
elif command -v python >/dev/null 2>&1; then
    PY="python"
else
    echo "Installing python3... (needs sudo)"
    sudo apt update && sudo apt install -y python3 python3-pip python3-venv
    PY="python3"
fi

echo "==> Installing Python packages ($PY)"
$PY -m pip install -r requirements.txt

# python3-tk is not part of most Linux python builds, and every folder/file
# picker in the app opens a Tk dialog. Without it those buttons do nothing at
# all, with no error, so it is installed here rather than left as a note.
$PY -m pip install --quiet python3-tk 2>/dev/null \
  || $PY -m pip install --quiet tk 2>/dev/null \
  || echo "  - Could not install python3-tk via pip; use your package manager (apt install python3-tk)"

echo "==> Installing Linux system packages (audio, clipboard, window tools, screenshots)"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y portaudio19-dev xclip xsel wl-clipboard wmctrl scrot \
        imagemagick x11-utils python3-tk tesseract-ocr
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y portaudio-devel xclip xsel wl-clipboard wmctrl scrot \
        ImageMagick xorg-x11-utils python3-tkinter tesseract
elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm portaudio xclip xsel wl-clipboard wmctrl scrot \
        imagemagick xorg-x11-utils tk tesseract
else
    echo "  - Could not detect a package manager; install manually:"
    echo "    portaudio, xclip/xsel/wl-clipboard, wmctrl, scrot or imagemagick,"
    echo "    python3-tk (folder pickers), tesseract-ocr (click_text / screen_text)"
fi

# On Wayland, scrot and ImageMagick's import cannot see the screen at all, and
# grim (wl-clipboard's sibling, from wl-clipboard/wl-screenshot) can. It is a
# small package and it is the only thing that makes take_screenshot work on
# the desktop most current distributions ship.
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "  (no Wayland session detected - scrot/ImageMagick will be used)"
else
    echo "==> Wayland session: installing grim for screenshots"
    if command -v apt-get >/dev/null 2>&1; then
        sudo apt install -y grim
    elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y grim
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -Sy --noconfirm grim
    else
        echo "  - install grim yourself for screenshots on Wayland"
    fi
fi

echo "==> Downloading Piper TTS voices (if missing)"
if ls "$PWD"/piper/*.onnx >/dev/null 2>&1; then
    echo "  Piper models already present."
else
    $PY piper/download_voices.py
fi

echo ""
if [ -z "$BONSAI_DIR" ]; then
    echo "  Hint: export BONSAI_DIR=$(pwd)  (or set it in ~/.bashrc / ~/.profile)"
fi

# The local model is the one thing this script cannot set up, and failing to
# say so here means the first message just fails to load the model with no
# explanation.
echo ""
echo "==> Checking the model server"
if command -v llama-server >/dev/null 2>&1; then
    echo "  Found: $(command -v llama-server)"
    echo "  Note: Bonsai 2 is a ternary model. Stock llama.cpp CANNOT read"
    echo "        these .gguf files - you need the PrismML build with ternary"
    echo "        kernels. If the server starts and then cannot load the"
    echo "        weights, this is why."
else
    echo "  llama-server was not found on PATH."
    echo "  The local Bonsai 2 model will not load until you install it:"
    echo "    - download a PrismML llama.cpp build (ternary kernels required)"
    echo "    - make llama-server executable and put it on PATH, or"
    echo "    - export PC_LLAMA_SERVER=/full/path/to/llama-server"
    echo "  The chat API models (the free ones in the model dropdown) work"
    echo "  without it."
fi
if [ ! -f Ternary-Bonsai-2-27B-PQ2_0.gguf ]; then
    echo ""
    echo "  The model file Ternary-Bonsai-2-27B-PQ2_0.gguf is not in this"
    echo "  folder. No .gguf is bundled - copy it in, or pick a hosted model"
    echo "  from the dropdown."
fi

echo ""
echo "  Setup complete - start with: ./run.sh"