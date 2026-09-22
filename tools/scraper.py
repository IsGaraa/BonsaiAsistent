"""Fetch a webpage and save readable text to out/. Requires requests + bs4.
Usage:
  python tools/scraper.py https://example.com
"""
import re
import sys
from pathlib import Path
import requests
from bs4 import BeautifulSoup

if len(sys.argv) < 2:
    sys.exit("usage: scraper.py <url>")

url = sys.argv[1]
resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
resp.raise_for_status()
name = (url.rstrip("/").rsplit("/", 1)[-1] or url.split("://")[1].split("/")[0] or "page")
name = re.sub(r"[^A-Za-z0-9._-]", "_", name) or "page"
path = Path(__file__).parent.parent / "out" / (name + ".txt")
path.parent.mkdir(parents=True, exist_ok=True)
text = "\n".join(
    s for s in BeautifulSoup(resp.text, "html.parser").get_text("\n", strip=True) if s
)
path.write_text(text, encoding="utf-8")
print(path)
