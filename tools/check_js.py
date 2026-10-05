"""Check the served JavaScript of BOTH pages with a real parser.

This is the check that was missing. ast.parse validates the Python; the page is
a 166,000-character JavaScript program embedded inside that Python, and a
SyntaxError in it is invisible to every check that was being run. It is also
silent in a specific way: the page still loads and still looks like the app,
it simply never runs a line of code, so the first symptom is something else
entirely - "my chats stopped saving" - pointing nowhere near the real cause.

There are now two pages with script on them, the chat and /kokoro, and both are
checked.

Run it after any change to the page markup or the embedded script:

    python tools\\check_js.py
"""
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = os.environ.get("BONSAI_URL", "http://127.0.0.1:8081")
# Every page with script on it. /kokoro is a second, separate page rather than
# part of the chat UI, and leaving it off this list meant a syntax error there
# would pass this check silently - which is the exact failure this file exists
# to prevent, just on the other page.
PAGES = ("/", "/chat", "/kokoro")

# The two things that have actually broken this file, both from writing
# JavaScript inside a non-raw Python string.
DANGER = [
    (r"[=(,\[]\s*'[^'\n]*$",
     "a single-quoted JS string that runs off the end of its line"),
]


def main():
    failed = False
    for page in PAGES:
        try:
            html = urllib.request.urlopen(BASE + page, timeout=25).read().decode(
                "utf-8", "replace")
        except Exception as exc:
            print("%-7s could not fetch (%s)" % (page, type(exc).__name__))
            failed = True
            continue
        blocks = [b for b in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
                  if b.strip()]
        print("%-7s %7d bytes, %d script block(s)" % (page, len(html), len(blocks)))
        if not blocks:
            print("        no script found - suspicious")
            failed = True
        for i, body in enumerate(blocks):
            fd, path = tempfile.mkstemp(suffix=".js")
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(body)
                p = subprocess.run(["node", "--check", path], capture_output=True,
                                   text=True)
                if p.returncode == 0:
                    print("        block %d: %6d chars  syntax OK" % (i, len(body)))
                else:
                    failed = True
                    print("        block %d: %6d chars  SYNTAX ERROR"
                          % (i, len(body)))
                    for line in (p.stderr or "").strip().splitlines()[:8]:
                        print("          " + line)
                    m = re.search(r":(\d+)$",
                                  (p.stderr or "").strip().splitlines()[0]
                                  if (p.stderr or "").strip() else "")
                    if m:
                        lines = body.splitlines()
                        ln = int(m.group(1))
                        for j in range(max(0, ln - 3), min(len(lines), ln + 2)):
                            print("       %s %5d  %s"
                                  % (">>" if j == ln - 1 else "  ", j + 1,
                                     lines[j][:110]))
            finally:
                os.unlink(path)

    print()
    if failed:
        print("FAILED - the page will not run. Do not ship this.")
        return 1
    print("All pages parse. A syntax error here stops every script on the page.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
