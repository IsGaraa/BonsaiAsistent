"""Fetch the Qwen-Image 2.1 weights this PC needs to draw pictures.

Three files, not one. Qwen-Image 2.1 is a diffusion transformer plus a
Qwen3-VL-8B text encoder plus its own VAE, and all three have to be present or
nothing runs. The text encoder is 8.71 GB of the 13.2 GB total, which surprises
people who expect a "4 GB model".

    python tools\\fetch_image_model.py

Resumable: an interrupted transfer continues rather than starting over.
"""
import os
import sys
import threading
import time

REPO_ID = "abenzerps/Qwen-Image-2.1-Uncensored-GGUF"
REVISION = "base"
DEST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "image_models")

# (path in the repo, subfolder here, expected size in bytes)
FILES = [
    ("qwen-image-2.1-UC-Q4_0.gguf", "diffusion", 4156276736),
    ("vae/qwen_image_2.1_vae_bf16.safetensors", "vae", 676331008),
    ("text_encoders/qwen3vl_8b_int8_convrot.safetensors", "text_encoder",
     9353002496),
]


def main():
    try:
        from huggingface_hub import hf_hub_download
    except ImportError:
        print("This needs huggingface_hub:  pip install huggingface_hub")
        return 1
    os.makedirs(DEST, exist_ok=True)
    total = sum(s for _, _, s in FILES)
    got = 0
    for remote, sub, size in FILES:
        folder = os.path.join(DEST, sub)
        os.makedirs(folder, exist_ok=True)
        print("  %-52s %6.2f GB" % (remote, size / 1073741824))
        path = hf_hub_download(repo_id=REPO_ID, filename=remote,
                               revision=REVISION, local_dir=folder)
        # hf keeps the repo's own subfolder when the file has one; flatten it
        flat = os.path.join(folder, os.path.basename(remote))
        if os.path.abspath(path) != os.path.abspath(flat):
            if os.path.exists(flat):
                os.remove(flat)
            os.replace(path, flat)
        got += os.path.getsize(flat)
        print("      -> %s" % os.path.relpath(flat, DEST))
    print("\n%.2f GB of %.2f GB in %s" % (got / 1073741824, total / 1073741824,
                                           DEST))
    print("The engine itself is separate:  python tools\\fetch_sd_cpp.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
