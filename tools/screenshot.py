"""Capture the screen to out/screenshots. Requires Pillow.

Usage:
  python tools/screenshot.py
  python tools/screenshot.py <x> <y> <w> <h>   # capture a region
Then attach the .png to a chat message and I can read it.

PIL's ImageGrab only knows how to grab a screen on Windows and macOS. On
Linux it needs the XCB support that manylinux Pillow wheels are not built
with, so it raised OSError and the script simply did not work on a machine
where the app itself screenshots fine. It now falls back to the same
command-line grabbers the app uses: scrot or ImageMagick's import on X11,
grim on Wayland. Wayland is the default session on current GNOME and KDE,
and neither scrot nor import can see it.
"""
from pathlib import Path
from datetime import datetime
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

OUT = Path(__file__).parent.parent / "out" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

IS_LINUX = sys.platform.startswith("linux")


def _grab_with_command(bbox):
    """Screenshot via a command-line tool, then crop to bbox ourselves.

    The grabbers take their own crop flags, but they disagree with each other
    about what a region means, and a full grab plus one Image.crop is the
    same picture from all of them."""
    candidates = ["scrot", "grim", "import", "gnome-screenshot"]
    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        with tempfile.TemporaryDirectory() as tmp:
            raw = os.path.join(tmp, "shot.png")
            if name == "scrot":
                argv = [exe, "-z", raw]
            elif name == "grim":
                argv = [exe, raw]
            elif name == "import":
                argv = [exe, "-window", "root", raw]
            else:
                argv = [exe, "-f", raw]
            try:
                subprocess.run(argv, check=True, timeout=30,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            except Exception:
                continue
            if not os.path.exists(raw):
                continue
            try:
                img = Image.open(raw).convert("RGB")
            except Exception:
                continue
            if bbox:
                img = img.crop(bbox)
            return img
    return None


def grab(bbox=None):
    try:
        from PIL import ImageGrab
        # all_screens is silently ignored on Linux; harmless to ask for.
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        if img is not None and img.size[0] > 1 and img.size[1] > 1:
            return img.convert("RGB")
    except Exception:
        pass
    if IS_LINUX:
        img = _grab_with_command(bbox)
        if img is not None:
            return img
        raise SystemExit(
            "Could not capture the screen.\n"
            "  On Linux this needs one of: scrot, grim, ImageMagick (import)\n"
            "  or gnome-screenshot. Install one, e.g.:\n"
            "    sudo apt install scrot          (X11)\n"
            "    sudo apt install grim           (Wayland)\n"
            "  On a Wayland session scrot and ImageMagick cannot see the\n"
            "  screen at all - use grim.")
    raise SystemExit("Could not capture the screen - is Pillow installed, and "
                     "is there a desktop session to capture?")


bbox = None
if len(sys.argv) == 5:
    x, y, w, h = map(int, sys.argv[1:5])
    bbox = (x, y, x + w, y + h)

img = grab(bbox)

path = OUT / f"screen_{datetime.now():%Y%m%d_%H%M%S}.png"
img.save(path)
print(path)
