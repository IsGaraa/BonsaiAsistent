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

echo "==> Installing Linux system packages (audio, clipboard, window tools, screenshots)"
if command -v apt-get >/dev/null 2>&1; then
    sudo apt update
    sudo apt install -y portaudio19-dev xclip wmctrl scrot imagemagick x11-utils
elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y portaudio-devel xclip wmctrl scrot ImageMagick
elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --noconfirm portaudio xclip wmctrl scrot imagemagick
else
    echo "  - Could not detect a package manager; install manually:"
    echo "    portaudio, xclip/xsel, wmctrl, scrot or imagemagick"
    echo "    (tkinter/portaudio are needed by the TTS and pick-folder tools)"
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
echo "  Setup complete - start with: ./run.sh"