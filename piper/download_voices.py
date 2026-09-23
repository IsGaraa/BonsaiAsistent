"""Download the default Piper voices used by BONSAI.

The .onnx model files are too large to ship in git, so a fresh clone gets this
script instead. Run it from the project root:

    python piper\\download_voices.py

It fetches the two bundled-default voices into the piper/ folder:
  - en_US-lessac-medium    (English)
  - ro_RO-mihai-medium     (Romanian)

Other voices: https://huggingface.co/rhasspy/piper-voices
You can drop any <voice>.onnx + <voice>.onnx.json pair into piper/ and call it
with the tts_speak voice parameter.
"""

import os
import sys
import urllib.request
from pathlib import Path

PIPER_DIR = Path(__file__).resolve().parent

VOICES = {
    "en_US-lessac-medium": [
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/lessac/medium/en_US-lessac-medium.onnx.json",
    ],
    "ro_RO-mihai-medium": [
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ro/ro_RO/mihai/medium/ro_RO-mihai-medium.onnx",
        "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/ro/ro_RO/mihai/medium/ro_RO-mihai-medium.onnx.json",
    ],
}


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"  already present: {dest.name}")
        return
    print(f"  downloading {dest.name} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "bonsai-piper"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(str(dest), "wb") as fh:
        total = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk)
            total += len(chunk)
    print(f"  saved {dest.name} ({total / 1024 / 1024:.1f} MB)")


def main() -> None:
    which = [v for v in sys.argv[1:] if v in VOICES] or list(VOICES)
    os.makedirs(PIPER_DIR, exist_ok=True)
    for voice in which:
        for url in VOICES[voice]:
            download(url, PIPER_DIR / url.rsplit("/", 1)[-1])
    print("Done. Voices are ready for tts_speak / tts_voices.")


if __name__ == "__main__":
    main()