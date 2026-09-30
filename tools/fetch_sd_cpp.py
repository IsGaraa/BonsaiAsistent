"""Fetch the stable-diffusion.cpp engine for Windows + CUDA.

A prebuilt binary, not a source build. The toolchain for building is present on
most machines that got this far (MSVC, the CUDA toolkit) but cmake usually is
not, and compiling a ggml CUDA target is a long way to find out something is
wrong. The maintainers publish release builds, and the one pinned below is from
2026-09-27 - new enough to include the Qwen-Image 2.1 support that upstream
documents in docs/qwen_image_2.1.md.

    python tools\\fetch_sd_cpp.py

The CUDA 12 build is deliberate. It is the best-supported path on an NVIDIA
card, and the 13.x driver on this machine runs a CUDA 12 runtime without
trouble. The separate cudart archive is not optional: it carries the
redistributable runtime DLLs the engine links against.
"""
import json
import os
import sys
import time
import urllib.request
import zipfile

REPO = "leejet/stable-diffusion.cpp"
TAG = "master-929-3f8527a"
DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "sd.cpp")
WANT = ("sd-master-3f8527a-bin-win-cuda12-x64.zip", "cudart-sd-bin-win")


def main():
    url = "https://api.github.com/repos/%s/releases/tags/%s" % (REPO, TAG)
    req = urllib.request.Request(url, headers={
        "User-Agent": "bonsai-fetch", "Accept": "application/vnd.github+json"})
    rel = json.loads(urllib.request.urlopen(req, timeout=60).read().decode())
    assets = {a["name"]: a for a in rel.get("assets") or []}
    os.makedirs(DEST, exist_ok=True)
    for name, asset in assets.items():
        if name not in WANT:
            continue
        if os.path.exists(os.path.join(DEST, "sd-cli.exe")) and name != WANT[0]:
            continue
        print("  %-46s %7.1f MB" % (name, asset["size"] / 1048576))
        tmp = os.path.join(DEST, name)
        r = urllib.request.Request(asset["browser_download_url"],
                                   headers={"User-Agent": "bonsai-fetch"})
        with urllib.request.urlopen(r, timeout=600) as resp, open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        with zipfile.ZipFile(tmp) as z:
            z.extractall(DEST)
        os.remove(tmp)
    exe = os.path.join(DEST, "sd-cli.exe")
    if not os.path.exists(exe):
        print("  sd-cli.exe is missing - the download did not complete")
        return 1
    print("\nengine ready: %s" % exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
