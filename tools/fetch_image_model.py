"""Fetch the Qwen-Image 2.1 weights this PC needs to draw pictures.

Three files, not one. Qwen-Image 2.1 is a diffusion transformer plus a
Qwen3-VL-8B text encoder plus its own VAE, and all three have to be present or
nothing runs. The text encoder is 8.71 GiB of the 15.6 GiB total, which surprises
people who expect a "4 GB model".

    python tools\\fetch_image_model.py

Resumable: an interrupted transfer continues rather than starting over.

QUANTISATION. The diffusion model is fetched at Q8_0, which is what
IMAGE_DIFFUSION_FILE in bonsai_web.py names as the default - so a fresh install
ends up with the same file this repository already documents as the one in use.
The same repository publishes Q4_0, Q4_K_M, Q5_K_M and Q6_K of the same
uncensored model, and the app will run any of them: it matches on
"qwen-image-2.1-uc" and ignores the quantisation, so trading quality for disk
or VRAM is a one-line change to FILES below rather than a code change. Q4_0 is
3.87 GiB against Q8_0's 7.07, which is worth knowing on a small card.

NOTE the two families in that repository. It carries the uncensored
qwen-image-2.1-UC-*.gguf AND the plain qwen-image-2.1-*.gguf. This fetches the
UC ones deliberately - they are the point - and the plain ones are not fetched
at all, so there is no risk of the app picking up censored weights that happen
to sort first.
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
    ("qwen-image-2.1-UC-Q8_0.gguf", "diffusion", 7591557920),
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
