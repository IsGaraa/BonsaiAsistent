"""Capture the screen to tools/.. (out/screenshots). Requires Pillow.
Usage:
  python tools/screenshot.py
  python tools/screenshot.py <x> <y> <w> <h>   # capture a region
Then attach the .png to a chat message and I can read it.
"""
from pathlib import Path
from datetime import datetime
from PIL import ImageGrab
import sys

OUT = Path(__file__).parent.parent / "out" / "screenshots"
OUT.mkdir(parents=True, exist_ok=True)

if len(sys.argv) == 5:
    x, y, w, h = map(int, sys.argv[1:5])
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
else:
    img = ImageGrab.grab()

path = OUT / f"screen_{datetime.now():%Y%m%d_%H%M%S}.png"
img.save(path)
print(path)
