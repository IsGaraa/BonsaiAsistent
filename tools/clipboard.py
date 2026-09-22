"""Clipboard helper. Requires pyperclip.
Usage:
  python tools/clipboard.py get        # print clipboard contents
  python tools/clipboard.py save       # save clipboard to out/clipboard.txt
  python tools/clipboard.py put <text> # set clipboard
"""
import sys
from pathlib import Path

try:
    import pyperclip
except ImportError:
    sys.exit("Install it first:  pip install pyperclip")

OUT = Path(__file__).parent.parent / "out" / "clipboard.txt"
cmd = sys.argv[1] if len(sys.argv) > 1 else "get"

if cmd == "get":
    print(pyperclip.paste())
elif cmd == "save":
    OUT.write_text(pyperclip.paste(), encoding="utf-8")
    print(OUT)
elif cmd == "put":
    pyperclip.copy(" ".join(sys.argv[2:]))
    print("clipboard set")
else:
    sys.exit("usage: clipboard.py get | save | put <text>")
