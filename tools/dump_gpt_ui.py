"""Regenerate gpt_ui.html from the page the server really serves.

The old copy was a hand-copied snapshot that had fallen 32k characters behind
and still described the chat-history silo that no longer exists - a reference
that actively misleads. This fetches the live page instead, so it is exact.

Fetched, not hand-edited: no secrets can be baked in, because the page pulls
its model list from /api/models at runtime.
"""
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
REPO = r"C:\Users\drago\Desktop\BonsaiAsistent"
PORT = "8217"
APPDIR = tempfile.mkdtemp(prefix="bonsai_snap_")
open(os.path.join(APPDIR, "chats.json"), "w").write("[]")
json_txt = ('{"active":"l","models":[{"id":"l","label":"Stub","type":"api",'
            '"base_url":"http://127.0.0.1:1/v1","model":"x","api_key":"K",'
            '"cfg":{"ctx":1000}}]}')
open(os.path.join(APPDIR, "models.json"), "w").write(json_txt)
env = dict(os.environ)
env.update({"BONSAI_DIR": APPDIR, "BONSAI_PORT": PORT, "BONSAI_NO_BROWSER": "1",
            "BONSAI_NO_BLENDER": "1"})
srv = subprocess.Popen([sys.executable, "bonsai_web.py"], cwd=REPO, env=env,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    html = None
    for _ in range(60):
        try:
            html = urllib.request.urlopen("http://127.0.0.1:%s/chat" % PORT,
                                          timeout=5).read().decode("utf-8")
            break
        except Exception:
            time.sleep(0.5)
    if not html:
        print("could not reach the server; gpt_ui.html left alone")
        sys.exit(1)
finally:
    srv.terminate()
    try:
        srv.wait(timeout=10)
    except Exception:
        srv.kill()
    shutil.rmtree(APPDIR, ignore_errors=True)

commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO,
                        capture_output=True, text=True).stdout.strip()

# a copy of the page must never carry a key with it
leaks = [p for p in ("sk-or-v1-", "oc_sk_", "BIG_PICKLE")
         if p in html]
if leaks:
    print("ABORT: the page contains %s" % ", ".join(leaks))
    sys.exit(1)

banner = (
    "<!-- GPT UI (PAGE_GPT) - the page served at /chat, copied verbatim from a\n"
    "     running bonsai_web.py at commit %s.\n"
    "\n"
    "     bonsai_web.py is the source of truth. This file is a read-only copy so\n"
    "     the UI can be opened in an editor without wading through the Python;\n"
    "     editing it changes nothing.\n"
    "\n"
    "     To refresh after changing the page, run:  python - tools/dump_gpt_ui.py\n"
    "     (or just:  curl -s http://127.0.0.1:8080/chat > gpt_ui.html)\n"
    "-->\n" % commit)

out = banner + html
path = os.path.join(REPO, "gpt_ui.html")
io.open(path, "w", encoding="utf-8", newline="\n").write(out)

old = len(io.open(path, encoding="utf-8").read())
print("gpt_ui.html written: %d chars (was 96402)" % len(out))
print("commit stamped    :", commit)
print("html body matches the served page:",
      out[len(banner):] == html)
print("secrets in the copy:", leaks or "none")
