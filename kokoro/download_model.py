"""Download the Kokoro TTS model and voice styles used by BONSAI.

Both files are far too large to ship in git, so a fresh clone gets this script
instead. Run it from the project root:

    python kokoro\\download_model.py

It fetches into kokoro/ :
  - kokoro-v1.0.onnx        310 MB   the 82M-parameter model, fp32
  - voices-v1.0.bin          27 MB   every voice style the release ships

SPEAKING IS ENGLISH ONLY. The voices file holds styles for a dozen languages
because that is how the release is built, but BONSAI only offers the English
ones: the af_ / am_ (American) and bf_ / bm_ (British) prefixes. They are
listed by tts_voices and on the /kokoro page. Note that means no Romanian -
Kokoro has no ro_ styles, so this replaces the Piper Romanian voices with
nothing rather than with a replacement.

MODEL CHOICE. fp32 by default because it is the reference export and TTS is
cheap enough that the size does not matter. --model int8 drops it to 109 MB and
is noticeably faster to load on CPU; --model fp16 is 156 MB and needs an
onnxruntime build with fp16 kernels, which the CPU one may not have.

English sources:
  model   https://github.com/thewh1teagle/kokoro-onnx (MIT; model Apache-2.0)
  voices  https://huggingface.co/hexgrad/Kokoro-82M
"""

import os
import sys
import urllib.request
from pathlib import Path

KOKORO_DIR = Path(__file__).resolve().parent
BASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1"

MODELS = {
    # name on disk          -> (url, approx size in bytes)
    "kokoro-v1.0.onnx": (BASE + "/kokoro-v1.0.onnx", 325463296),
    "kokoro-v1.0.int8.onnx": (BASE + "/kokoro-v1.0.int8.onnx", 114085888),
    "kokoro-v1.0.fp16.onnx": (BASE + "/kokoro-v1.0.fp16.onnx", 163577856),
}

VOICES_FILE = "voices-v1.0.bin"
VOICES_URL = BASE + "/" + VOICES_FILE

# Which file bonsai_web.py should load. Changed by --model.
DEFAULT_MODEL = "kokoro-v1.0.onnx"


def download(url, dest):
    """Fetch one file, skipping it if it is already there.

    A partial download is written to .part and moved into place only once the
    transfer finishes, so an interrupted run never leaves a truncated model that
    looks present to the app - which would fail much later, inside onnxruntime,
    with a message about the model rather than about the download.
    """
    if dest.exists() and dest.stat().st_size > 0:
        print("  already present: %s (%.1f MB)"
              % (dest.name, dest.stat().st_size / 1048576.0))
        return
    part = dest.with_suffix(dest.suffix + ".part")
    print("  downloading %s ..." % dest.name)
    req = urllib.request.Request(url, headers={"User-Agent": "bonsai-kokoro"})
    total = 0
    try:
        with urllib.request.urlopen(req, timeout=120) as resp, \
                open(str(part), "wb") as fh:
            while True:
                chunk = resp.read(1 << 18)
                if not chunk:
                    break
                fh.write(chunk)
                total += len(chunk)
    except Exception:
        if part.exists():
            part.unlink()
        raise
    if total == 0:
        part.unlink()
        raise RuntimeError("%s came back empty" % url)
    os.replace(str(part), str(dest))
    print("  saved %s (%.1f MB)" % (dest.name, total / 1048576.0))


def main():
    args = [a for a in sys.argv[1:]]
    model = DEFAULT_MODEL
    if "--model" in args:
        i = args.index("--model")
        if i + 1 >= len(args):
            print("--model needs a name: %s" % ", ".join(sorted(MODELS)))
            return 1
        model = args[i + 1]
        del args[i:i + 2]
    if model not in MODELS:
        print("unknown model %r - choose one of: %s"
              % (model, ", ".join(sorted(MODELS))))
        return 1

    os.makedirs(KOKORO_DIR, exist_ok=True)
    url, expect = MODELS[model]
    print("Kokoro TTS -> %s" % KOKORO_DIR)
    print("  model  %-24s %6.1f MB (expected)"
          % (model, expect / 1048576.0))
    print("  voices %-24s %6.1f MB (expected)"
          % (VOICES_FILE, 28206656 / 1048576.0))
    download(url, KOKORO_DIR / model)
    download(VOICES_URL, KOKORO_DIR / VOICES_FILE)
    print("\nDone. Kokoro is ready for tts_speak / tts_voices and /kokoro.")
    print("English only - list them with the tts_voices tool.")
    print("The Python side needs:  pip install kokoro-onnx")
    return 0


if __name__ == "__main__":
    sys.exit(main())