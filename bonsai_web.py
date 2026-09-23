import asyncio
import base64
import glob
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    MCP_AVAILABLE = True
except Exception:
    MCP_AVAILABLE = False

HOST, PORT = "127.0.0.1", 8081

# ---- model backends ----
BONSAI_DIR = os.environ.get("BONSAI_DIR", r"C:\Users\drago\Desktop\bonsai2")
LLAMA_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "prism-llama", "llama-server.exe")
BONSAI_MODEL = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-PQ2_0.gguf")
BONSAI_MMPROJ = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf")
BONSAI_BASE = "http://127.0.0.1:8080"
BONSAI_MODEL_ID = "bonsai2"
BONSAI_CTX = 32768
BONSAI_KEEP_ALIVE = 120
CHATS_FILE = os.path.join(os.path.expandvars(r"%APPDATA%"), "BonsaiAsistent", "chats.json")
MAX_TOOL_ROUNDS = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 200 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 200
MODE_BUILD = "build"
MODE_PLAN = "plan"
WORKDIR = os.path.realpath(os.environ.get("PC_WORKDIR", r"C:\Users\drago\Desktop\workspace"))

CREATE_NO_WINDOW = 0x08000000

_BONSAI_LOCK = threading.Lock()
_ASKS = {}
_ASKS_LOCK = threading.Lock()
_TODOS = []
_TODOS_LOCK = threading.Lock()
_LAST_ACTIVITY = time.time()
_ACTIVITY_LOCK = threading.Lock()
_BONSAI_EFFORT = "xhigh"
_EFFORT_LOCK = threading.Lock()

PF = "C:\\Program Files"
PF86 = "C:\\Program Files (x86)"
OFFICE = "\\Microsoft Office\\root\\Office16\\"
LOCAL = os.path.expandvars(r"%LOCALAPPDATA%")
APPDATA = os.path.expandvars(r"%APPDATA%")

_APPS = {
    # ---- system built-ins ----
    "notepad": [("cmd", "notepad")],
    "calculator": [("uri", "calculator:"), ("cmd", "calc")],
    "paint": [("cmd", "mspaint")],
    "wordpad": [("cmd", "write")],
    "snipping tool": [("uri", "ms-screenclip:")],
    "cmd": [("cmd", "cmd")],
    "terminal": [("cmd", "wt")],
    "powershell": [("cmd", "powershell")],
    "explorer": [("cmd", "explorer")],
    "task manager": [("cmd", "taskmgr")],
    "control panel": [("cmd", "control")],
    "settings": [("uri", "ms-settings:")],
    "camera": [("uri", "microsoft.windows.camera:")],
    "photos": [("uri", "ms-photos:")],
    "clock": [("uri", "ms-clock:")],
    "weather": [("uri", "msnweather:")],
    "maps": [("uri", "bingmaps:")],
    "mail": [("uri", "outlookmail:")],
    "calendar": [("uri", "outlookcal:")],
    "microsoft store": [("uri", "ms-windows-store:")],
    "movies and tv": [("uri", "mswindowsvideo:")],
    "music": [("uri", "mswindowsmusic:")],
    "people": [("uri", "ms-people:")],
    "sticky notes": [("uri", "ms-sticky notes:")],
    "feedback hub": [("uri", "feedback-hub:")],
    "windows security": [("uri", "windowsdefender:")],
    "tips": [("uri", "ms-get-started:")],
    "xbox": [("uri", "xbox:")],
    "solitaire": [("uri", "xboxliveapp-1297287741:")],
    "device manager": [("cmd", "devmgmt.msc")],
    "disk management": [("cmd", "diskmgmt.msc")],
    "event viewer": [("cmd", "eventvwr.msc")],
    "services": [("cmd", "services.msc")],
    "registry editor": [("cmd", "regedit")],
    "system information": [("cmd", "msinfo32")],
    "disk cleanup": [("cmd", "cleanmgr")],
    "character map": [("cmd", "charmap")],
    "on-screen keyboard": [("cmd", "osk")],
    # ---- browsers ----
    "chrome": [("path", PF + "\\Google\\Chrome\\Application\\chrome.exe"),
               ("path", PF86 + "\\Google\\Chrome\\Application\\chrome.exe"),
               ("cmd", "chrome")],
    "edge": [("uri", "microsoft-edge:"), ("cmd", "msedge"),
             ("path", PF + "\\Microsoft\\Edge\\Application\\msedge.exe")],
    "firefox": [("path", PF + "\\Mozilla Firefox\\firefox.exe"),
                ("path", PF86 + "\\Mozilla Firefox\\firefox.exe"),
                ("cmd", "firefox")],
    "opera": [("path", PF + "\\Opera\\opera.exe"),
              ("path", PF86 + "\\Opera\\opera.exe")],
    "brave": [("path", PF + "\\BraveSoftware\\Brave-Browser\\Application\\brave.exe"),
              ("path", PF86 + "\\BraveSoftware\\Brave-Browser\\Application\\brave.exe")],
    "vivaldi": [("path", PF + "\\Vivaldi\\Application\\vivaldi.exe")],
    "internet explorer": [("cmd", "iexplore")],
    # ---- communication ----
    "discord": [("uri", "discord://"), ("cmd", "discord")],
    "telegram": [("uri", "tg://"), ("path", LOCAL + "\\Telegram Desktop\\Telegram.exe")],
    "whatsapp": [("uri", "whatsapp://"), ("path", LOCAL + "\\WhatsApp\\WhatsApp.exe")],
    "slack": [("uri", "slack://"), ("cmd", "slack")],
    "zoom": [("uri", "zoommtg://"), ("path", APPDATA + "\\Zoom\\bin\\Zoom.exe")],
    "teams": [("cmd", "ms-teams"),
              ("path", LOCAL + "\\Microsoft\\Teams\\current\\Teams.exe")],
    "messenger": [("uri", "fb-messenger://")],
    "signal": [("path", LOCAL + "\\Programs\\signal\\signal.exe")],
    "skype": [("cmd", "skype")],
    # ---- office ----
    "word": [("path", PF + OFFICE + "WINWORD.EXE"),
             ("path", PF86 + OFFICE + "WINWORD.EXE"), ("cmd", "winword")],
    "excel": [("path", PF + OFFICE + "EXCEL.EXE"),
              ("path", PF86 + OFFICE + "EXCEL.EXE"), ("cmd", "excel")],
    "powerpoint": [("path", PF + OFFICE + "POWERPNT.EXE"),
                   ("path", PF86 + OFFICE + "POWERPNT.EXE"), ("cmd", "powerpnt")],
    "access": [("path", PF + OFFICE + "MSACCESS.EXE"),
               ("path", PF86 + OFFICE + "MSACCESS.EXE"), ("cmd", "msaccess")],
    "publisher": [("path", PF + OFFICE + "MSPUB.EXE"),
                  ("path", PF86 + OFFICE + "MSPUB.EXE"), ("cmd", "mspub")],
    "onenote": [("path", PF + OFFICE + "ONENOTE.EXE"),
                ("path", PF86 + OFFICE + "ONENOTE.EXE"), ("cmd", "onenote")],
    "outlook": [("path", PF + OFFICE + "OUTLOOK.EXE"),
                ("path", PF86 + OFFICE + "OUTLOOK.EXE"), ("cmd", "outlook")],
    "onedrive": [("cmd", "onedrive")],
    # ---- media ----
    "spotify": [("uri", "spotify:"), ("path", APPDATA + "\\Spotify\\Spotify.exe")],
    "vlc": [("path", PF + "\\VideoLAN\\VLC\\vlc.exe"),
            ("path", PF86 + "\\VideoLAN\\VLC\\vlc.exe")],
    "obs": [("path", PF + "\\obs-studio\\bin\\64bit\\obs64.exe")],
    "itunes": [("path", PF + "\\iTunes\\iTunes.exe"), ("cmd", "itunes")],
    # ---- games ----
    "steam": [("uri", "steam://")],
    "epic games": [("path", PF86 + "\\Epic Games\\Launcher\\Portal\\Binaries\\Win64\\EpicGamesLauncher.exe")],
    "gog": [("path", PF86 + "\\GOG Galaxy\\GalaxyClient.exe")],
    "battlenet": [("path", PF86 + "\\Battle.net\\Battle.net.exe")],
    "game bar": [("uri", "ms-settings:gaming-gamebar")],
    "roblox": [("glob", LOCAL + "\\Roblox\\Versions\\RobloxPlayerBeta.exe"),
               ("glob", LOCAL + "\\Roblox\\Versions\\RobloxPlayerLauncher.exe")],
    "minecraft": [("uri", "minecraft://")],
    # ---- developer ----
    "vscode": [("cmd", "code")],
    "notepad++": [("path", PF + "\\Notepad++\\notepad++.exe"),
                  ("path", PF86 + "\\Notepad++\\notepad++.exe")],
    "git bash": [("path", PF + "\\Git\\git-bash.exe")],
    "python": [("cmd", "python")],
    "putty": [("path", PF + "\\PuTTY\\putty.exe")],
    "winrar": [("path", PF + "\\WinRAR\\WinRAR.exe"), ("cmd", "winrar")],
    "7zip": [("path", PF + "\\7-Zip\\7zFM.exe"), ("cmd", "7zFM")],
    "github desktop": [("path", LOCAL + "\\GitHubDesktop\\GitHubDesktop.exe")],
    # ---- design / utilities ----
    "gimp": [("glob", PF + "\\GIMP*\\bin\\gimp*.exe")],
    "blender": [("glob", PF + "\\Blender Foundation\\Blender*\\blender.exe")],
    "photoshop": [("glob", PF + "\\Adobe\\Adobe Photoshop*\\Photoshop.exe")],
    "acrobat": [("path", PF + "\\Adobe\\Acrobat DC\\Acrobat\\Acrobat.exe")],
    "obsidian": [("path", LOCAL + "\\Obsidian\\Obsidian.exe")],
    "powertoys": [("path", LOCAL + "\\Microsoft\\PowerToys\\PowerToys.exe")],
}

_ALIASES = {
    "calc": "calculator", "calculator": "calculator",
    "text editor": "notepad", "notepad": "notepad",
    "mspaint": "paint", "paint": "paint",
    "command prompt": "cmd", "command line": "cmd", "cmd": "cmd",
    "windows terminal": "terminal", "console": "terminal", "terminal": "terminal",
    "file explorer": "explorer", "files": "explorer", "explorer": "explorer",
    "task manager": "task manager", "taskmgr": "task manager",
    "control panel": "control panel",
    "settings": "settings",
    "snip": "snipping tool", "snipping tool": "snipping tool", "screenshot tool": "snipping tool",
    "microsoft word": "word", "word": "word",
    "microsoft excel": "excel", "excel": "excel",
    "powerpoint": "powerpoint", "ppt": "powerpoint",
    "ms access": "access", "access": "access",
    "publisher": "publisher",
    "one note": "onenote", "onenote": "onenote",
    "ms outlook": "outlook", "outlook": "outlook",
    "microsoft edge": "edge", "edge": "edge",
    "google chrome": "chrome", "chrome": "chrome",
    "mozilla firefox": "firefox", "firefox": "firefox",
    "internet explorer": "internet explorer", "ie": "internet explorer",
    "google meet": "zoom", "zoom": "zoom",
    "skype": "skype", "signal": "signal",
    "teams": "teams", "ms teams": "teams",
    "microsoft store": "microsoft store", "store": "microsoft store",
    "store app": "microsoft store",
    "media player": "movies and tv", "movies and tv": "movies and tv",
    "groove music": "music", "music": "music",
    "camera": "camera", "camera app": "camera",
    "photos": "photos", "photo viewer": "photos",
    "alarms": "clock", "clock": "clock", "alarm": "clock", "timer": "clock", "stopwatch": "clock",
    "weather": "weather",
    "maps": "maps",
    "mail": "mail", "email": "mail",
    "calendar": "calendar",
    "edge browser": "edge",
    "browser": "chrome",
    "vs code": "vscode", "visual studio code": "vscode", "code editor": "vscode", "vscode": "vscode",
    "notepad plus plus": "notepad++", "notepad++": "notepad++", "npp": "notepad++",
    "git bash": "git bash", "bash": "git bash",
    "git": "git bash",
    "python": "python",
    "putty": "putty",
    "winrar": "winrar",
    "7 zip": "7zip", "7zip": "7zip", "seven zip": "7zip",
    "steam": "steam",
    "discord": "discord",
    "telegram": "telegram",
    "whatsapp": "whatsapp",
    "slack": "slack",
    "spotify": "spotify",
    "vlc": "vlc", "vlc media player": "vlc",
    "obs": "obs", "obs studio": "obs",
    "epic": "epic games", "epic games": "epic games", "epic games launcher": "epic games",
    "gog galaxy": "gog", "gog": "gog",
    "battle.net": "battlenet", "battlenet": "battlenet", "battle net": "battlenet",
    "xbox": "xbox", "xbox app": "xbox",
    "roblox": "roblox",
    "minecraft": "minecraft",
    "game bar": "game bar",
    "github desktop": "github desktop",
    "gimp": "gimp",
    "blender": "blender",
    "photoshop": "photoshop", "adobe photoshop": "photoshop",
    "adobe acrobat": "acrobat", "acrobat": "acrobat", "pdf reader": "acrobat",
    "obsidian": "obsidian",
    "powertoys": "powertoys", "power toys": "powertoys",
}

_NAME_RE = re.compile(r"[\w&' .()-]+\Z")


def _canonical(name):
    text = re.sub(r"\.exe\Z", "", name.strip().lower())
    text = re.sub(r"\b(?:the|open|launch|start|me|please|app|application|website|site|program|browser|tool|launcher)\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _ALIASES.get(text, text)


def _store_appid(name):
    if not _NAME_RE.match(name or ""):
        return None
    script = "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    try:
        apps = json.loads(out) if out.strip() else []
    except json.JSONDecodeError:
        return None
    if isinstance(apps, dict):
        apps = [apps]
    target = name.strip().lower()
    for a in apps:
        if a.get("Name") and a["Name"].lower() == target:
            return a
    for a in apps:
        if a.get("Name") and target in a["Name"].lower():
            return a
    return None


def _launch(name):
    key = _canonical(name)
    error, ok = None, None
    for kind, value in _APPS.get(key, []):
        try:
            if kind == "uri":
                os.startfile(value)
            elif kind == "cmd":
                subprocess.Popen(["cmd", "/c", "start", "", value])
            elif kind == "path":
                path = os.path.expandvars(value)
                if not os.path.exists(path):
                    continue
                os.startfile(path)
            elif kind == "glob":
                hits = glob.glob(os.path.expandvars(value))
                if not hits:
                    continue
                os.startfile(hits[0])
        except Exception as exc:
            error = f"{kind}:{value} -> {exc}"
            continue
        return {"launched": key, "result": "ok", "method": kind}
    if not _APPS.get(key):
        store = _store_appid(key)
        if store:
            try:
                os.startfile("shell:AppsFolder\\" + store["AppID"])
            except Exception as exc:
                return {"error": f"could not open '{key}': {exc}"}
            return {"launched": store.get("Name", key), "result": "ok", "method": "start menu app"}
        known = ", ".join(sorted(_APPS)[:60])
        return {"error": f"unknown app '{name}'; I know how to open: {known}",
                "known_count": len(_APPS)}
    return {"error": f"could not launch '{key}'" + (f" ({error})" if error else "")}


_SITES = {
    "youtube": "youtube.com", "google": "google.com", "facebook": "facebook.com",
    "instagram": "instagram.com", "twitter": "x.com", "x": "x.com",
    "github": "github.com", "reddit": "reddit.com", "wikipedia": "en.wikipedia.org",
    "amazon": "amazon.com", "netflix": "netflix.com", "twitch": "twitch.tv",
    "gmail": "mail.google.com", "stack overflow": "stackoverflow.com",
    "linkedin": "linkedin.com", "bing": "bing.com", "duckduckgo": "duckduckgo.com",
    "yahoo": "yahoo.com", "ebay": "ebay.com",
    "youtube.com": "youtube.com", "wikipedia.org": "wikipedia.org",
    "google.com": "google.com", "facebook.com": "facebook.com",
    "instagram.com": "instagram.com", "reddit.com": "reddit.com",
    "twitter.com": "twitter.com", "github.com": "github.com",
    "gitlab": "gitlab.com", "pinterest": "pinterest.com",
    "tumblr": "tumblr.com", "imgur": "imgur.com", "medium": "medium.com",
    "quora": "quora.com", "spotify": "open.spotify.com", "soundcloud": "soundcloud.com",
    "bandcamp": "bandcamp.com", "vimeo": "vimeo.com", "dailymotion": "dailymotion.com",
    "hulu": "hulu.com", "disney plus": "disneyplus.com", "disneyplus.com": "disneyplus.com",
    "hbo max": "max.com", "max.com": "max.com", "prime video": "primevideo.com",
    "steam community": "steamcommunity.com", "steamcommunity.com": "steamcommunity.com",
    "dropbox": "dropbox.com", "drive": "drive.google.com", "google drive": "drive.google.com",
    "google docs": "docs.google.com", "google maps": "maps.google.com",
    "translate": "translate.google.com", "google translate": "translate.google.com",
    "youtube music": "music.youtube.com", "news": "news.google.com",
    "zoom web": "zoom.us", "canva": "canva.com", "figma": "figma.com",
    "notion": "notion.so", "trello": "trello.com", "discord web": "discord.com",
    "tiktok": "tiktok.com", "snapchat web": "web.snapchat.com", "whatsapp web": "web.whatsapp.com",
    "telegram web": "web.telegram.org", "outlook mail": "outlook.com", "hotmail": "outlook.com",
    "office": "office.com", "microsoft office": "office.com",
    "stackoverflow.com": "stackoverflow.com",
}


def _normalize_url(value):
    text = str(value).strip().strip("/")
    for prefix in ("http://", "https://"):
        if text.startswith(prefix):
            return text
    if text.startswith("www."):
        return "https://" + text
    return "https://" + text


def _resolve_site(name):
    low = name.strip().lower()
    domain = _SITES.get(low) or low
    if re.search(r"[a-z0-9-]+\.[a-z]{2,}(?:[/?].*)?\Z", domain):
        return _normalize_url(domain)
    return None


def _open_one(name):
    key = _canonical(name)
    if key in _APPS:
        result = _launch(key)
        if result.get("result") == "ok":
            return result
        return {"error": result.get("error", "could not launch")}
    store = _store_appid(key)
    if store:
        try:
            os.startfile("shell:AppsFolder\\" + store["AppID"])
        except Exception as exc:
            return {"error": f"could not open '{name}': {exc}"}
        return {"launched": store.get("Name", key), "result": "ok", "method": "start menu app"}
    site = _resolve_site(key)
    if site:
        webbrowser.open(site)
        return {"opened": site, "result": "ok", "method": "website"}
    known = ", ".join(sorted(_APPS)[:40])
    return {"error": f"could not open '{name}': it is neither a known program "
                     f"({known}, ...) nor a website"}


def _launch_or_open(name):
    parts = [p.strip() for p in re.split(r"\s+(?:and|&|,|;)\s+", str(name)) if p.strip()]
    if len(parts) == 1:
        return _open_one(parts[0])
    return [_open_one(p) for p in parts]


def launch_or_open(name):
    return _launch_or_open(name)


TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "launch_or_open",
        "description": "Open a program or a website on this PC by name. "
                       "Examples: steam, discord, notepad, spotify, epic games, "
                       "youtube, github, google, wikipedia. You may name several, "
                       "e.g. 'steam and youtube'. The tool decides whether each "
                       "name is an app or a website.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Name of the program or website to open."
                }
            },
            "required": ["name"]
        }
    }
}

def _safe_path(p):
    if p is None:
        p = ""
    text = str(p).strip()
    base = os.path.realpath(WORKDIR)
    if not text:
        return base
    cand = os.path.realpath(os.path.join(base, text)) if not os.path.isabs(text) else os.path.realpath(text)
    if cand == base or cand.startswith(base + os.sep):
        return cand
    raise ValueError(f"access denied: path outside workspace '{WORKDIR}'")


def _list_dir(path):
    target = _safe_path(path)
    if not os.path.isdir(target):
        return {"error": f"not a directory: {target}"}
    entries = []
    for e in sorted(os.listdir(target)):
        full = os.path.join(target, e)
        entries.append({"name": e, "type": "dir" if os.path.isdir(full) else "file"})
    return {"result": "ok", "path": target, "entries": entries}


def _read_file(path):
    target = _safe_path(path)
    if not os.path.isfile(target):
        return {"error": f"file not found: {target}"}
    if os.path.getsize(target) > MAX_FILE_BYTES:
        return {"error": f"file too large: {target} ({os.path.getsize(target)} bytes, max {MAX_FILE_BYTES})"}
    try:
        with open(target, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except Exception as exc:
        return {"error": f"could not read {target}: {exc}"}
    return {"result": "ok", "path": target, "content": content}


def _write_file(path, content):
    target = _safe_path(path)
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(str(content or ""))
    return {"result": "ok", "path": target, "bytes": os.path.getsize(target)}


def _edit_file(path, old_text, new_text, replace_all):
    target = _safe_path(path)
    if not os.path.isfile(target):
        return {"error": f"file not found: {target}"}
    with open(target, "r", encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    if old_text not in content:
        return {"error": f"text not found in {target}: {old_text[:60]!r}"}
    count = content.count(old_text)
    if replace_all:
        content = content.replace(old_text, new_text)
    else:
        content = content.replace(old_text, new_text, 1)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    return {"result": "ok", "path": target, "replaced": count}


def _search_files(pattern, path):
    target = _safe_path(path) if path else os.path.realpath(WORKDIR)
    if not os.path.isdir(target):
        return {"error": f"not a directory: {target}"}
    rx = re.compile(pattern, re.IGNORECASE | re.UNICODE)
    hits = []
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            if os.path.getsize(full) > MAX_FILE_BYTES:
                continue
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if rx.search(line):
                            rel = os.path.relpath(full, WORKDIR).replace("\\", "/")
                            hits.append({"file": rel, "line": lineno,
                                        "text": line.rstrip()[:200]})
                            if len(hits) >= MAX_SEARCH_RESULTS:
                                return {"result": "ok", "matches": hits,
                                        "truncated": True, "path": target}
            except Exception:
                continue
    return {"result": "ok", "matches": hits, "truncated": False, "path": target}


_WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BonsaiAsistent/1.0"


def _fetch_html(url, max_bytes=400000):
    req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read(max_bytes)
        enc = (resp.headers.get_content_charset() or "utf-8")
    return data.decode(enc, errors="replace")


def _html_to_text(markup):
    body = re.sub(r"(?is)<(script|style|noscript|svg|head)>.*?</\1>", " ", markup)
    body = re.sub(r"(?is)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"[ \t\r\x0b\x0c]+", " ", body)
    body = re.sub(r"\n\s*\n+", "\n", body)
    return body.strip()


def _parse_bing(markup, limit):
    results = []
    blocks = re.findall(r'(?is)<li class="b_algo".*?</li>', markup)
    for block in blocks[:limit]:
        am = re.search(r'(?is)<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block)
        if not am:
            continue
        href, atext = am.group(1), am.group(2)
        title = html.unescape(re.sub(r"(?is)<[^>]+>", "", atext)).strip()
        if not title:
            continue
        sm = re.search(r'(?is)<p[^>]*>(.*?)</p>', block)
        snip = html.unescape(re.sub(r"(?is)<[^>]+>", "", sm.group(1))).strip() if sm else ""
        if href.startswith("http"):
            results.append({"title": title, "url": href, "snippet": snip})
    return results


def _parse_wikipedia_search(query, limit):
    url = ("https://en.wikipedia.org/w/api.php?action=opensearch&format=json&limit="
           + str(limit) + "&origin=*&search=" + urllib.parse.quote(str(query)))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return []
    titles = data[1] or []
    urls = data[3] or []
    descs = data[2] or []
    results = []
    for i, title in enumerate(titles):
        results.append({
            "title": title,
            "url": urls[i] if i < len(urls) else "",
            "snippet": (descs[i] or "") if i < len(descs) else "",
        })
    return results


def _web_search(query, max_results=6):
    if not str(query or "").strip():
        return {"error": "query is required"}
    results = _parse_wikipedia_search(query, max_results)
    if results:
        return {"result": "ok", "query": str(query), "results": results,
                "note": "from Wikipedia"}
    urls = [
        "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(str(query)),
        "https://www.bing.com/search?q=" + urllib.parse.quote(str(query)) + "&count=10&setlang=en",
    ]
    for target in urls:
        try:
            markup = _fetch_html(target)
        except Exception:
            continue
        if "duckduckgo.com" in target:
            anchors = re.findall(r'(?is)<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', markup)
            snippets = re.findall(r'(?is)<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', markup)
            for i, (href, atext) in enumerate(anchors[:max_results]):
                title = html.unescape(re.sub(r"(?is)<[^>]+>", "", atext)).strip()
                m = re.search(r"uddg=([^&]+)", href)
                real = urllib.parse.unquote(m.group(1)) if m else href
                snip = ""
                if i < len(snippets):
                    snip = html.unescape(re.sub(r"(?is)<[^>]+>", "", snippets[i])).strip()
                if real.startswith("http") and title:
                    results.append({"title": title, "url": real, "snippet": snip})
        else:
            results = _parse_bing(markup, max_results)
        if results:
            return {"result": "ok", "query": str(query), "results": results}
    return {"result": "ok", "query": str(query), "results": [],
            "note": "no results found"}


def _wiki_read(title):
    clean = urllib.parse.unquote(str(title)).replace("_", " ").strip()
    url = ("https://en.wikipedia.org/w/api.php?action=query&prop=extracts&explaintext=1"
           "&format=json&redirects=1&origin=*&titles=" + urllib.parse.quote(clean))
    req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    pages = ((data.get("query") or {}).get("pages") or {})
    for pid, page in pages.items():
        if pid == "-1":
            return None, None
        return page.get("title"), page.get("extract") or ""
    return None, None


def _web_fetch(url, max_chars=6000):
    target = str(url or "").strip()
    if not target:
        return {"error": "url is required"}
    try:
        low = target.lower()
        if "wikipedia.org" in low and "/wiki/" in low:
            title, text = _wiki_read(urllib.parse.urlparse(target).path.rsplit("/", 1)[-1])
            if text:
                return {"result": "ok", "url": target, "title": title,
                        "content": text[:max_chars]}
        markup = _fetch_html(target, max_bytes=max_chars * 8)
    except Exception as exc:
        return {"error": f"could not fetch {target}: {exc}"}
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", markup)
    title = html.unescape(tm.group(1).strip()) if tm else ""
    text = _html_to_text(markup)
    return {"result": "ok", "url": target, "title": title,
            "content": text[:max_chars]}


WEB_TOOLS = {
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the internet when you need up-to-date information, "
                           "facts, news, documentation or anything you are not sure "
                           "about. Returns a short list of titles, links and snippets. "
                           "Then read details with web_fetch on the most relevant link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'latest node.js LTS version'."
                    }
                },
                "required": ["query"]
            }
        }
    },
    "web_fetch": {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read the full text of a webpage by URL. Use after "
                           "web_search to read an article, docs page or answer in "
                           "detail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full URL, e.g. 'https://en.wikipedia.org/wiki/Rocket'."
                    }
                },
                "required": ["url"]
            }
        }
    },
}


FILE_TOOLS = {
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List files and folders inside the workspace folder "
                           "(paths are relative to it). Use before reading files "
                           "so you know what exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path inside the workspace, "
                                       "e.g. 'src', '' (default = workspace root)."
                    }
                },
                "required": []
            }
        }
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file inside the workspace folder "
                           "(relative path). Returns its full content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file, e.g. 'notes.txt'."
                    }
                },
                "required": ["path"]
            }
        }
    },
    "search_files": {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Regex search across files in the workspace folder "
                           "and returns file + line matches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression, e.g. 'def main'."
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional relative folder to search inside "
                                       "(default = whole workspace)."
                    }
                },
                "required": ["pattern"]
            }
        }
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a text file inside the workspace "
                           "folder (relative path). Creates parent folders if "
                           "missing. Use only when the user asks you to write "
                           "or create a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path, e.g. 'tasklist.txt'."
                    },
                    "content": {
                        "type": "string",
                        "description": "Full text content to write."
                    }
                },
                "required": ["path", "content"]
            }
        }
    },
    "edit_file": {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace text inside an existing file in the workspace "
                           "folder (relative path). Replaces ALL occurrences of "
                           "old_text with new_text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path of the file."
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text to find."
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text."
                    }
                },
                "required": ["path", "old_text", "new_text"]
            }
        }
    }
}


def _run_command(command, workdir=None, timeout=45):
    cmd = str(command or "").strip()
    if not cmd:
        return {"error": "command is required"}
    cwd = os.path.realpath(WORKDIR)
    if workdir:
        wd = str(workdir).strip()
        cwd = os.path.realpath(wd) if os.path.isabs(wd) else _safe_path(wd)
    if not os.path.isdir(cwd):
        return {"error": f"not a directory: {cwd}"}
    try:
        proc = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                              text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW)
        pieces = [proc.stdout or ""]
        if proc.stderr:
            pieces.append(proc.stderr)
        out = "\n".join(pieces).strip()
        if len(out) > 8000:
            out = out[:8000] + "\n[... output truncated ...]"
        return {"result": "ok", "command": cmd, "workdir": cwd,
                "exit_code": proc.returncode, "output": out}
    except subprocess.TimeoutExpired:
        return {"error": f"command timed out after {timeout}s", "command": cmd}
    except Exception as exc:
        return {"error": f"could not run command: {exc}", "command": cmd}


_CODE_TIMEOUT = 30
_CODE_CAP = 8000
_CODE_RUNNERS = {
    "python": {"cmd": [sys.executable], "ext": ".py"},
    "py": {"cmd": [sys.executable], "ext": ".py"},
    "node": {"cmd": None, "ext": ".js"},
    "js": {"cmd": None, "ext": ".js"},
    "javascript": {"cmd": None, "ext": ".js"},
}


def _code_runner(lang):
    key = str(lang or "python").strip().lower()
    spec = _CODE_RUNNERS.get(key)
    if not spec:
        return None, None
    cmd = spec["cmd"]
    if cmd is None:
        found = shutil.which("node")
        if not found:
            return None, None
        cmd = [found]
    return list(cmd), spec["ext"]


def _run_code(language, code, timeout=_CODE_TIMEOUT):
    runner, ext = _code_runner(language)
    if not runner:
        return {"error": f"code runner not available for '{language}' "
                         "(I can run python and node)"}
    snippet = str(code or "").strip()
    if not snippet:
        return {"error": "code is required"}
    sandbox = tempfile.mkdtemp(prefix="bonsai_sandbox_")
    script = os.path.join(sandbox, "snippet" + ext)
    try:
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(snippet)
        proc = subprocess.run(runner + [script], capture_output=True,
                              text=True, timeout=timeout, cwd=sandbox,
                              creationflags=CREATE_NO_WINDOW)
        pieces = [proc.stdout or ""]
        if proc.stderr:
            pieces.append("STDERR:\n" + proc.stderr)
        out = "\n".join(pieces).strip()
        if len(out) > _CODE_CAP:
            out = out[:_CODE_CAP] + "\n[... output truncated ...]"
        return {"result": "ok", "language": str(language or "python"),
                "exit_code": proc.returncode, "output": out}
    except subprocess.TimeoutExpired:
        return {"error": f"code timed out after {timeout}s"}
    except Exception as exc:
        return {"error": f"could not run code: {exc}"}
    finally:
        try:
            shutil.rmtree(sandbox, ignore_errors=True)
        except Exception:
            pass


def _clip_result(res):
    if isinstance(res, dict) and res.get("content") and isinstance(res["content"], str):
        if len(res["content"]) > 12000:
            res = dict(res)
            res["content"] = res["content"][:12000] + "\n[... response content truncated ...]"
    return res


def _exec_file_tool(name, args):
    try:
        if name == "list_dir":
            return _list_dir(args.get("path"))
        if name == "read_file":
            return _read_file(args.get("path"))
        if name == "search_files":
            return _search_files(args.get("pattern"), args.get("path"))
        if name == "write_file":
            return _write_file(args.get("path"), args.get("content"))
        if name == "edit_file":
            return _edit_file(args.get("path"), args.get("old_text"),
                              args.get("new_text"), replace_all=True)
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"{name} failed: {exc}"}
    return {"error": f"unknown tool {name}"}


# ---- Blender MCP bridge (mcp-for-blender: stdio server -> Blender addon) ----
BLENDER_MCP_ALLOW = (
    "get_addon_status", "get_scene_info", "get_object_info",
    "get_viewport_screenshot", "execute_blender_code",
    "bpy_api_lookup", "describe_node_type", "export_scene",
)
BLENDER_OFFLINE_MSG = ("Blender tools are not reachable. Open Blender and make sure the "
                       "'MCP for Blender' addon is enabled (Edit > Preferences > "
                       "Add-ons > MCP for Blender), then the blender tools reconnect.")
_BLENDER_MCP = None
_BLENDER_GUARD = threading.Lock()
_BLENDER_PROBE = {"at": 0.0, "state": "unknown"}


class _BlenderMcp:
    def __init__(self):
        self._lock = threading.Lock()
        self._loop = None
        self._session = None
        self._tools = {}
        self._error = None
        self._settled = threading.Event()
        self._restart_at = 0.0

    def ensure(self, timeout=40):
        with self._lock:
            if self._session is not None:
                return True
            if self._error is not None and time.time() < self._restart_at:
                return False
            if self._loop is None or not self._loop.is_running():
                self._error = None
                self._tools = {}
                loop = asyncio.new_event_loop()
                self._loop = loop
                self._settled.clear()
                threading.Thread(target=self._run, args=(loop,), daemon=True).start()
        self._settled.wait(timeout)
        with self._lock:
            return self._session is not None

    def _run(self, loop):
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._supervise())
        except Exception as exc:
            self._fail(f"bridge crashed: {exc}")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._settled.set()

    async def _supervise(self):
        env = dict(os.environ)
        env["BLENDER_MCP_DISABLE_TELEMETRY"] = "1"
        env["DISABLE_TELEMETRY"] = "1"
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "blender_mcp.server"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                with self._lock:
                    self._session = session
                    self._tools = {t.name: t for t in tools.tools
                                   if t.name in BLENDER_MCP_ALLOW}
                self._settled.set()
                while True:
                    await asyncio.sleep(0.5)

    def _fail(self, msg):
        self._restart_at = time.time() + 5
        with self._lock:
            self._error = msg
            self._session = None

    def call(self, name, args):
        with self._lock:
            session, loop = self._session, self._loop
            tools = set(self._tools)
        if session is None or loop is None:
            return {"error": "Blender MCP bridge is not connected"}
        if name not in tools:
            return {"error": f"unknown blender tool '{name}'"}
        try:
            fut = asyncio.run_coroutine_threadsafe(
                session.call_tool(name, args or {}), loop)
            res = fut.result(timeout=600)
        except Exception as exc:
            return {"error": f"blender tool '{name}' failed: {exc}"}
        if getattr(res, "isError", False):
            text = "".join(getattr(p, "text", "") or "" for p in res.content)
            return {"error": text.strip() or f"blender tool '{name}' returned an error"}
        text, image = [], None
        for p in res.content:
            typ = getattr(p, "type", "")
            if typ == "text":
                text.append(getattr(p, "text", "") or "")
            elif typ == "image":
                data = getattr(p, "data", None) or ""
                mime = getattr(p, "mimeType", "") or "image/png"
                if isinstance(data, (bytes, bytearray)):
                    data = base64.b64encode(bytes(data)).decode("ascii")
                elif isinstance(data, str):
                    data = data.strip()
                    if data.startswith("data:") and "," in data:
                        data = data.split(",", 1)[1]
                if data and image is None:
                    image = "data:" + mime + ";base64," + "".join(data.split())
            elif getattr(p, "text", None):
                text.append(str(p.text))
        out = {}
        body = "\n".join(t for t in text if t.strip()).strip()
        if body:
            out["result"] = body
        if image:
            out["image_data"] = image
        if not body and not image:
            out["result"] = "ok"
        return out


def _blender_mcp_bridge():
    global _BLENDER_MCP
    with _BLENDER_GUARD:
        if _BLENDER_MCP is None:
            _BLENDER_MCP = _BlenderMcp()
        return _BLENDER_MCP


def _blender_tool_schemas():
    if not MCP_AVAILABLE:
        return []
    bridge = _blender_mcp_bridge()
    with bridge._lock:
        items = list(bridge._tools.values())
    out = []
    for tool in items:
        out.append({"type": "function",
                    "function": {"name": tool.name,
                                 "description": getattr(tool, "description", "") or "",
                                 "parameters": getattr(tool, "inputSchema", None)
                                               or {"type": "object", "properties": {}}}})
    return out


def _blender_tool_names():
    if not MCP_AVAILABLE:
        return set()
    return set(_blender_mcp_bridge()._tools)


def _blender_tool_call(name, args):
    try:
        bridge = _blender_mcp_bridge()
        if not bridge.ensure(timeout=45):
            return None
        return bridge.call(name, args)
    except Exception as exc:
        return {"error": f"blender tool '{name}' failed: {exc}"}


def _blender_kickoff():
    try:
        _blender_mcp_bridge().ensure(timeout=60)
    except Exception as exc:
        print(f"WARN: blender bridge start failed: {exc}", flush=True)


def _blender_status_payload():
    if not MCP_AVAILABLE:
        return {"ok": False, "state": "missing", "tool_count": 0,
                "detail": "Python package 'mcp-for-blender' is not installed"}
    bridge = _blender_mcp_bridge()
    try:
        started = bridge.ensure(timeout=25)
    except Exception:
        started = False
    names = sorted(bridge._tools)
    if started:
        now = time.time()
        if now - _BLENDER_PROBE["at"] >= 8:
            try:
                r = bridge.call("get_addon_status", {})
                if r.get("error") or "error" in str(r.get("result", ""))[:120].lower():
                    state = "bridge"
                    detail = "Blender addon not reachable on port 9876"
                else:
                    state = "ok"
                    detail = ""
            except Exception:
                state = "bridge"
                detail = ""
            _BLENDER_PROBE.update(at=time.time(), state=state)
        else:
            state = _BLENDER_PROBE["state"]
            detail = ""
    else:
        state = "down"
        detail = bridge._error or "bridge not started"
    return {"ok": started, "state": state, "tool_count": len(names),
            "tools": names, "detail": detail}


SYSTEM = ("You are the friendly assistant living on the user's Windows PC. Reply "
          "concisely and naturally, in the same language the user writes in. "
          "You have vision: if the user "
          "attaches one or more images or text files - or asks you to inspect "
          "the screen - look at the images carefully and read the file "
          "contents to answer their question about them. You can capture the "
          "screen (take_screenshot) and actually see what is displayed: error "
          "dialogs, app windows, web pages, terminal output. You can control "
          "the PC like a human: move the mouse, click, drag and type "
          "(control_input), read or write the system clipboard (clipboard) and "
          "download files from the web (download_file). You can also work on "
          "files inside the workspace folder and on archives (archive). When "
          "you are not sure about something, are asked for recent/current "
          "information, or want to double-check a fact, you may search the web "
          "(web_search) and read pages (web_fetch) to document yourself. When "
          "the user asks you to build, test, install or inspect something on "
          "the PC, you can run shell commands (run_command) and read their "
          "output to actually do it instead of only describing it. If you need "
          "a choice, a password or a confirmation from the user, ask them "
          "politely with ask_user instead of assuming. For longer tasks use "
          "todo_write to keep a visible list of the steps you are working on. "
          "When the user asks you to create or edit 3D content and Blender is "
          "running (check the Blender indicator in the header), use the blender "
          "tools (get_scene_info, get_object_info, execute_blender_code, "
          "get_viewport_screenshot) to work inside Blender: build and move "
          "objects with code, inspect the scene, and confirm your results with "
          "a viewport screenshot.")

PLAN_MODE_SYSTEM = ("\nMODE: PLAN. The user only wants a PLAN right now - do NOT "
                    "write, edit, create or delete any files, and do NOT launch "
                    "programs. Use the read-only tools to explore the workspace, "
                    "then present a clear step-by-step plan in the conversation. "
                    "Never call write_file or edit_file in this mode. If the user "
                    "asks to actually create or modify something, ask them to "
                    "switch to BUILD mode.")

def _bonsai_ready():
    try:
        h = urllib.request.urlopen(BONSAI_BASE + "/health", timeout=3)
        return json.loads(h.read().decode("utf-8")).get("status") == "ok"
    except Exception:
        return False


def _start_bonsai():
    log_out = os.path.join(BONSAI_DIR, "bonsai-server.out.log")
    log_err = os.path.join(BONSAI_DIR, "bonsai-server.err.log")
    if not os.path.exists(LLAMA_EXE):
        print(f"llama-server not found at {LLAMA_EXE}", flush=True)
        return False
    if not os.path.exists(BONSAI_MODEL):
        print(f"model not found at {BONSAI_MODEL}", flush=True)
        return False
    args = [LLAMA_EXE, "-m", BONSAI_MODEL,
            "--mmproj", BONSAI_MMPROJ,
            "--alias", BONSAI_MODEL_ID,
            "--port", "8080", "--ctx-size", "32768", "-ngl", "99",
            "--flash-attn", "on", "--temp", "1.0", "--top-p", "0.95",
            "--top-k", "20"]
    with open(log_out, "ab") as o, open(log_err, "ab") as e:
        subprocess.Popen(args, stdout=o, stderr=e, creationflags=CREATE_NO_WINDOW)
    for _ in range(100):
        if _bonsai_ready():
            return True
        time.sleep(2)
    return False


def _ensure_bonsai():
    if _bonsai_ready():
        return True
    with _BONSAI_LOCK:
        if _bonsai_ready():
            return True
        print("Bonsai 2 server not running - starting it...", flush=True)
        return _start_bonsai()


def _touch_activity():
    global _LAST_ACTIVITY
    with _ACTIVITY_LOCK:
        _LAST_ACTIVITY = time.time()


def _unload_bonsai():
    try:
        _http_json(BONSAI_BASE + "/v1/chat/completions",
                   {"model": BONSAI_MODEL_ID,
                    "messages": [{"role": "user", "content": "unload"}],
                    "max_tokens": 1, "keep_alive": 0}, timeout=60)
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def set_bonsai_effort(effort):
    global _BONSAI_EFFORT
    val = str(effort or "high").strip().lower()
    _BONSAI_EFFORT = {"off": "off", "low": "low", "med": "medium",
                      "medium": "medium", "high": "xhigh"}.get(val, "xhigh")
    return _BONSAI_EFFORT


def _effort_params():
    with _EFFORT_LOCK:
        eff = _BONSAI_EFFORT
    if eff == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"chat_template_kwargs": {"enable_thinking": True,
                                     "reasoning_effort": eff}}


def _http_json(url, payload, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


WEB_SPECS = [WEB_TOOLS["web_search"], WEB_TOOLS["web_fetch"]]

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "run_command",
        "description": "Run a shell command on this Windows PC and return its "
                       "output. Use it to build, test, install, debug or check "
                       "system info: python, pip, git, npm, node, dir, "
                       "tasklist, systeminfo, ping, etc. Runs in the workspace "
                       "folder by default. Max 45 seconds by default - raise "
                       "'timeout' for long-running commands (up to 600s). "
                       "Output truncated to ~8 KB. In PLAN mode this tool is "
                       "disabled.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to run, e.g. 'python tools/screenshot.py'."
                },
                "workdir": {
                    "type": "string",
                    "description": "Optional folder to run in: absolute path or "
                                   "relative to the workspace. Default = workspace root."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Optional timeout in seconds (1-600). Use a "
                                   "higher value for long downloads, builds or "
                                   "tests. Default 45."
                }
            },
            "required": ["command"]
        }
    }
}


CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "run_code",
        "description": "Run a small snippet of Python or Node.js in a sandboxed "
                       "environment and return its output. Use it to test functions, "
                       "verify logic, parse data or prototype before touching real "
                       "files. The sandbox has no network and a 30s timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "'python' or 'node'."
                },
                "code": {
                    "type": "string",
                    "description": "The source code to run."
                }
            },
            "required": ["language", "code"]
        }
    }
}


SHOT_TOOL = {
    "type": "function",
    "function": {
        "name": "take_screenshot",
        "description": "Capture the screen (or a region of it) and SEE it - you "
                       "have vision, so the image is fed directly to your eyes. "
                       "Use this whenever you need to inspect what is displayed "
                       "on the PC: error dialogs, app windows, web pages, "
                       "terminal output, a GUI, or to check the result of your "
                       "own mouse/keyboard actions. Also saves the PNG inside "
                       "the workspace so the user has a copy.",
        "parameters": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "object",
                    "description": "Optional region to capture: {x, y, width, "
                                   "height} in screen pixels. Omit for the full "
                                   "screen (all monitors)."
                }
            },
            "required": []
        }
    }
}


INPUT_TOOL = {
    "type": "function",
    "function": {
        "name": "control_input",
        "description": "Control the mouse and keyboard like a human: move the "
                       "cursor, click, double-click, right-click, drag, scroll, "
                       "type text or send key combinations. Pair it with "
                       "take_screenshot to see the result. Coordinates are "
                       "screen pixels (0,0 = top-left of the primary monitor).",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["move", "click", "double_click", "right_click",
                             "drag", "scroll", "type", "keys", "hotkey"],
                    "description": "What to do. 'move' only moves the cursor. "
                                   "'click'/'double_click'/'right_click' move "
                                   "then click. 'drag' moves then drags the "
                                   "mouse button. 'scroll' scrolls. 'type' "
                                   "writes text. 'keys' presses each key. "
                                   "'hotkey' presses keys together."
                },
                "x": {"type": "number", "description": "Cursor X (screen pixel). Not needed for 'type'/'keys'/'hotkey'."},
                "y": {"type": "number", "description": "Cursor Y (screen pixel). Not needed for 'type'/'keys'/'hotkey'."},
                "to_x": {"type": "number", "description": "For 'drag': end X position."},
                "to_y": {"type": "number", "description": "For 'drag': end Y position."},
                "clicks": {"type": "integer", "description": "For 'click': number of clicks."},
                "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button for 'click'/'drag'."},
                "amount": {"type": "integer", "description": "For 'scroll': number of scroll steps (negative = down)."},
                "text": {"type": "string", "description": "For 'type': the text to type."},
                "keys": {"type": "array", "items": {"type": "string"}, "description": "Key names, e.g. ['enter'], ['ctrl','s']. Use for 'keys'/'hotkey'."}
            },
            "required": ["action"]
        }
    }
}


CLIPBOARD_TOOL = {
    "type": "function",
    "function": {
        "name": "clipboard",
        "description": "Read or write the system clipboard. Use 'get' to read "
                       "what the user copied, and 'set' to put text on the "
                       "clipboard so the user can paste it anywhere.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set"],
                    "description": "'get' reads the current clipboard text; "
                                   "'set' writes 'text' to the clipboard."
                },
                "text": {
                    "type": "string",
                    "description": "Required for 'set': the text to copy."
                }
            },
            "required": ["action"]
        }
    }
}


DOWNLOAD_TOOL = {
    "type": "function",
    "function": {
        "name": "download_file",
        "description": "Download a file from a web URL and save it inside the "
                       "workspace folder. Returns the saved path, the byte size "
                       "and (for text files) the beginning of the content so "
                       "you can see what you got.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Full http(s) URL to download."
                },
                "path": {
                    "type": "string",
                    "description": "Relative destination inside the workspace, "
                                   "e.g. 'Downloads/installer.exe'. Defaults to "
                                   "the file name from the URL."
                }
            },
            "required": ["url"]
        }
    }
}


ARCHIVE_TOOL = {
    "type": "function",
    "function": {
        "name": "archive",
        "description": "Create or extract archives (zip / tar / tar.gz / tgz) "
                       "inside the workspace folder. Use 'extract' to unpack a "
                       "file into a folder, or 'create' to pack files/folders "
                       "into a new zip.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["extract", "create"],
                    "description": "'extract' unpacks 'archive' into 'dest'. "
                                   "'create' packs 'files' into a new archive."
                },
                "archive": {
                    "type": "string",
                    "description": "Relative path of the archive (for extract: "
                                   "the file to unpack; for create: the file to "
                                   "produce)."
                },
                "dest": {
                    "type": "string",
                    "description": "For 'extract': relative folder to unpack into. "
                                   "For 'create': optional destination file."
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "For 'create': relative files/folders to include."
                }
            },
            "required": ["action", "archive"]
        }
    }
}


ASK_TOOL = {
    "type": "function",
    "function": {
        "name": "ask_user",
        "description": "Ask the user a question and wait for their answer, like "
                       "a human would. Use it when you need a choice, extra "
                       "information, a password, or confirmation before doing "
                       "something important. The user can pick one of the given "
                       "options or type a free answer. The rest of your work is "
                       "paused until they reply.",
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The question to ask, phrased clearly."
                },
                "options": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional short answer options the user can "
                                   "pick with one click."
                }
            },
            "required": ["question"]
        }
    }
}


TODO_TOOL = {
    "type": "function",
    "function": {
        "name": "todo_write",
        "description": "Replace the visible TODO list with the given items and "
                       "statuses. Use it at the start of a longer task to show "
                       "the user the plan, and update it as you make progress. "
                       "Status can be 'pending', 'in_progress' or 'completed'.",
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]}
                        },
                        "required": ["description"]
                    },
                    "description": "The full new list of todo items."
                }
            },
            "required": ["todos"]
        }
    }
}


PC_TOOLS = [SHOT_TOOL, INPUT_TOOL, CLIPBOARD_TOOL,
            DOWNLOAD_TOOL, ARCHIVE_TOOL, ASK_TOOL, TODO_TOOL]


def _tools_for(mode):
    if mode == MODE_PLAN:
        return [FILE_TOOLS["list_dir"], FILE_TOOLS["read_file"],
                FILE_TOOLS["search_files"]] + WEB_SPECS
    return ([TOOL_SPEC] + list(FILE_TOOLS.values()) + WEB_SPECS +
            [SHELL_TOOL, CODE_TOOL] + PC_TOOLS + _blender_tool_schemas())


def system_prompt(mode):
    text = SYSTEM
    if mode == MODE_PLAN:
        text += PLAN_MODE_SYSTEM
    return text


def _bonsai_chat(messages, tools):
    payload = {"model": BONSAI_MODEL_ID, "messages": messages,
               "tools": tools, "stream": False, "keep_alive": -1}
    payload.update(_effort_params())
    data = _http_json(BONSAI_BASE + "/v1/chat/completions", payload)
    msg = data.get("choices", [{}])[0].get("message", {})
    msg["_usage"] = data.get("usage") or {}
    msg["_timings"] = data.get("timings") or {}
    return msg


def _bonsai_stream(messages, tools):
    payload = {"model": BONSAI_MODEL_ID, "messages": messages,
               "tools": tools, "stream": True, "keep_alive": -1}
    payload.update(_effort_params())
    req = urllib.request.Request(BONSAI_BASE + "/v1/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        calls = {}
        finish = None
        usage = {}
        timings = {}
        first_content_ts = None
        t_start = time.monotonic()
        for raw in resp:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except Exception:
                continue
            if chunk.get("usage"):
                usage = chunk["usage"]
            if chunk.get("timings"):
                timings = chunk["timings"]
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason") or finish
            if delta.get("reasoning_content"):
                yield {"kind": "reason", "text": delta["reasoning_content"]}
            elif delta.get("content"):
                if first_content_ts is None:
                    first_content_ts = time.monotonic()
                    yield {"kind": "think_end", "t_ms": int((first_content_ts - t_start) * 1000)}
                yield {"kind": "delta", "text": delta["content"]}
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                slot = calls.setdefault(idx, {
                    "id": tc.get("id") or f"call_{idx}",
                    "type": "function",
                    "function": {"name": fn.get("name") or "", "arguments": ""}})
                if fn.get("name"):
                    slot["function"]["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["function"]["arguments"] += fn["arguments"]
            if finish in ("stop", "tool_calls"):
                break
        ordered = [calls[i] for i in sorted(calls)]
        t_end = time.monotonic()
        think_ms = int((first_content_ts - t_start) * 1000) if first_content_ts else None
        total_ms = int((t_end - t_start) * 1000)
        respond_ms = total_ms - think_ms if think_ms is not None else total_ms
        prompt_tok = (usage.get("prompt_tokens")
                      or (timings.get("prompt_n") or 0) + (timings.get("cache_n") or 0))
        comp_tok = (usage.get("completion_tokens")
                    or timings.get("predicted_n") or 0)
        cached_tok = ((usage.get("prompt_tokens_details") or {}).get("cached_tokens")
                      or timings.get("cache_n") or 0)
        stats = {
            "think_ms": think_ms,
            "respond_ms": respond_ms,
            "total_ms": total_ms,
            "prompt_tokens": prompt_tok,
            "completion_tokens": comp_tok,
            "total_tokens": prompt_tok + comp_tok,
            "cached_tokens": cached_tok,
            "tok_s": timings.get("predicted_per_second") or 0,
            "ctx_used": prompt_tok + comp_tok,
            "ctx_left": BONSAI_CTX - prompt_tok - comp_tok,
        }
        yield {"kind": "end", "tool_calls": ordered, "finish": finish, "stats": stats}


def build_messages(raw_messages, mode=MODE_BUILD):
    msgs = [{"role": "system", "content": system_prompt(mode)}]
    for m in raw_messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant"):
            continue
        if isinstance(content, list):
            texts = []
            images = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    texts.append(str(part["text"]))
                elif part.get("type") == "file_att":
                    fname = str(part.get("name", "file"))
                    ftext = str(part.get("text", ""))
                    texts.append(f"[Fișier atașat: {fname}]\n{ftext}")
                elif part.get("type") == "image_url":
                    images.append(part)
            clean = [{"type": "text", "text": t} for t in texts if t]
            clean.extend(images)
            if clean:
                msgs.append({"role": role, "content": clean})
        elif isinstance(content, str) and content.strip():
            msgs.append({"role": role, "content": content})
    if not any(m.get("role") == "user" for m in msgs):
        msgs.append({"role": "user", "content": "..."})
    return msgs


def parse_arguments(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def public_result(result):
    if isinstance(result, list):
        return [public_result(r) for r in result]
    if isinstance(result, dict) and result.get("result") == "ok":
        if "launched" in result or "opened" in result:
            return {"result": "ok", "launched": result.get("launched"),
                    "opened": result.get("opened"), "method": result.get("method")}
        return result
    if isinstance(result, dict) and result.get("error"):
        return {"error": result["error"]}
    return result


def _take_screenshot(args):
    try:
        from PIL import Image, ImageGrab
    except Exception as exc:
        return {"error": f"PIL is not installed: {exc}"}
    bbox = None
    reg = args.get("region")
    if isinstance(reg, dict):
        try:
            bx = int(reg.get("x") or 0)
            by = int(reg.get("y") or 0)
            bw = int(reg.get("width") or reg.get("w") or 0)
            bh = int(reg.get("height") or reg.get("h") or 0)
            if bw > 0 and bh > 0:
                bbox = (bx, by, bx + bw, by + bh)
        except Exception:
            bbox = None
    try:
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
    except Exception as exc:
        return {"error": f"screenshot capture failed: {exc}"}
    out_dir = os.path.join(WORKDIR, "screenshots")
    try:
        os.makedirs(out_dir, exist_ok=True)
        png = os.path.join(out_dir, "screen_" +
                           time.strftime("%Y%m%d_%H%M%S") + ".png")
        img.save(png, "PNG")
    except Exception:
        png = None
    w, h = img.size
    view = img
    if max(w, h) > 1280:
        r = 1280.0 / max(w, h)
        try:
            view = img.resize((int(w * r), int(h * r)), Image.Resampling.LANCZOS)
        except Exception:
            view = img.resize((int(w * r), int(h * r)))
    if view.mode != "RGB":
        view = view.convert("RGB")
    buf = io.BytesIO()
    view.save(buf, "JPEG", quality=82)
    data_uri = "data:image/jpeg;base64," + \
        base64.b64encode(buf.getvalue()).decode("ascii")
    info = {"result": "ok", "width": w, "height": h}
    if png:
        info["saved"] = png.replace("\\", "/")
    info["image"] = data_uri
    return info


def _control_input(args):
    try:
        import pyautogui
    except Exception as exc:
        return {"error": f"pyautogui is not installed: {exc}"}
    action = str(args.get("action") or "click").strip()
    try:
        if action == "move":
            pyautogui.moveTo(int(args.get("x") or 0),
                             int(args.get("y") or 0), duration=0.15)
        elif action == "click":
            pyautogui.moveTo(int(args.get("x") or 0),
                             int(args.get("y") or 0), duration=0.15)
            pyautogui.click(clicks=int(args.get("clicks") or 1),
                            button=str(args.get("button") or "left"))
        elif action == "double_click":
            pyautogui.moveTo(int(args.get("x") or 0),
                             int(args.get("y") or 0), duration=0.15)
            pyautogui.doubleClick()
        elif action == "right_click":
            pyautogui.moveTo(int(args.get("x") or 0),
                             int(args.get("y") or 0), duration=0.15)
            pyautogui.rightClick()
        elif action == "drag":
            sx = int(args.get("x") or 0)
            sy = int(args.get("y") or 0)
            tx = int(args.get("to_x") or sx)
            ty = int(args.get("to_y") or sy)
            pyautogui.moveTo(sx, sy, duration=0.2)
            pyautogui.dragRel(tx - sx, ty - sy, duration=0.4,
                              button=str(args.get("button") or "left"))
        elif action == "scroll":
            pyautogui.scroll(int(args.get("amount") or -400))
        elif action == "type":
            pyautogui.write(str(args.get("text") or ""), interval=0.02)
        elif action == "keys":
            for key in (args.get("keys") or []):
                pyautogui.press(str(key))
        elif action == "hotkey":
            keys = [str(k) for k in (args.get("keys") or []) if k]
            if keys:
                pyautogui.hotkey(*keys)
        else:
            return {"error": f"unknown action '{action}'"}
    except Exception as exc:
        return {"error": f"input action '{action}' failed: {exc}"}
    result = {"result": "ok", "action": action}
    try:
        result["mouse"] = list(pyautogui.position())
    except Exception:
        pass
    return result


def _clipboard(args):
    action = str(args.get("action") or "get").strip().lower()
    try:
        import pyperclip
    except Exception as exc:
        return {"error": f"pyperclip is not installed: {exc}"}
    try:
        if action == "set":
            pyperclip.copy(str(args.get("text") or ""))
            return {"result": "ok", "action": "set"}
        if action in ("get", "read"):
            return {"result": "ok", "action": "get",
                    "text": str(pyperclip.paste() or "")}
    except Exception as exc:
        return {"error": f"clipboard {action} failed: {exc}"}
    return {"error": "action must be 'get' or 'set'"}


def _download_file(args):
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return {"error": "url must start with http:// or https://"}
    raw_path = str(args.get("path") or "").strip()
    if not raw_path:
        raw_path = url.split("/")[-1].split("?")[0] or "download.bin"
    try:
        dest = _safe_path(raw_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    except Exception as exc:
        return {"error": f"bad destination path: {exc}"}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = resp.read()
        with open(dest, "wb") as fh:
            fh.write(data)
    except Exception as exc:
        return {"error": f"download failed: {exc}"}
    info = {"result": "ok", "url": url,
            "saved": dest.replace("\\", "/"), "bytes": len(data)}
    if len(data) <= MAX_TEXT_FILE_BYTES and _looks_text(data):
        try:
            text = data.decode("utf-8", errors="replace")
            if len(text) > 2000:
                text = text[:2000] + "\n[... preview truncated ...]"
            info["preview"] = text
        except Exception:
            pass
    return info


def _looks_text(data):
    try:
        data.decode("utf-8")
        return True
    except Exception:
        return False


def _archive(args):
    import zipfile
    import tarfile
    action = str(args.get("action") or "extract").strip().lower()
    try:
        arc = _safe_path(str(args.get("archive") or ""))
    except Exception as exc:
        return {"error": f"bad archive path: {exc}"}
    if action == "extract":
        try:
            dest = _safe_path(str(args.get("dest") or os.path.basename(arc) + "_extracted"))
            os.makedirs(dest, exist_ok=True)
        except Exception as exc:
            return {"error": f"bad destination path: {exc}"}
        lower = arc.lower()
        try:
            if lower.endswith(".zip"):
                with zipfile.ZipFile(arc) as z:
                    names = z.namelist()
                    z.extractall(dest)
            elif lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")):
                with tarfile.open(arc) as t:
                    names = t.getnames()
                    t.extractall(dest)
            else:
                return {"error": "only zip / tar / tar.gz / tgz are supported "
                                 "directly - for other formats unpack with a "
                                 "run_command (e.g. 7z)"}
        except Exception as exc:
            return {"error": f"extract failed: {exc}"}
        return {"result": "ok", "extracted_to": dest.replace("\\", "/"),
                "entries": len(names),
                "first_files": [n.replace("\\", "/") for n in names[:15]]}
    if action == "create":
        dest = _safe_path(str(args.get("dest") or args.get("archive") or "archive.zip"))
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
        except Exception:
            pass
        srcs = [str(f) for f in (args.get("files") or []) if f]
        if not srcs:
            return {"error": "files is required for 'create'"}
        try:
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
                count = [0]
                for src in srcs:
                    p = os.path.abspath(_safe_path(src))
                    if os.path.isdir(p):
                        for root, _, files in os.walk(p):
                            for f in files:
                                fp = os.path.join(root, f)
                                z.write(fp, os.path.relpath(fp, os.path.dirname(p)))
                                count[0] += 1
                    elif os.path.isfile(p):
                        z.write(p, os.path.basename(p))
                        count[0] += 1
        except Exception as exc:
            return {"error": f"create failed: {exc}"}
        return {"result": "ok", "created": dest.replace("\\", "/"),
                "entries": len(srcs)}
    return {"error": "action must be 'extract' or 'create'"}


def execute_tool_call(tc, hooks=None):
    global _TODOS
    fn = tc.get("function") or {}
    name = fn.get("name") or "launch_or_open"
    args = parse_arguments(fn.get("arguments"))
    image_uri = None
    if name == "launch_or_open":
        raw_result = launch_or_open(args.get("name", ""))
    elif name == "web_search":
        raw_result = _web_search(args.get("query"), 6)
    elif name == "web_fetch":
        raw_result = _web_fetch(args.get("url"))
    elif name == "run_command":
        timeout = min(max(int(args.get("timeout") or 45), 1), 600)
        raw_result = _run_command(args.get("command"), args.get("workdir"),
                                  timeout=timeout)
    elif name == "take_screenshot":
        shot = _take_screenshot(args)
        if shot.get("error"):
            raw_result = {"error": shot["error"]}
        else:
            image_uri = shot.pop("image", None)
            raw_result = shot
    elif name == "control_input":
        raw_result = _control_input(args)
    elif name == "clipboard":
        raw_result = _clipboard(args)
    elif name == "download_file":
        raw_result = _download_file(args)
    elif name == "archive":
        raw_result = _archive(args)
    elif name == "ask_user":
        on_ask = (hooks or {}).get("on_ask")
        if on_ask:
            raw_result = {"result": "ok",
                          "answer": on_ask(args) or "(no answer)"}
        else:
            raw_result = {"error": "ask_user is only available in the live UI"}
    elif name == "todo_write":
        items = []
        for it in (args.get("todos") or []):
            if isinstance(it, dict):
                status = str(it.get("status") or "pending").strip().lower()
                if status not in ("pending", "in_progress", "completed"):
                    status = "pending"
                items.append({"description": str(it.get("description") or "").strip(),
                              "status": status})
            elif isinstance(it, str):
                items.append({"description": it.strip(), "status": "pending"})
        with _TODOS_LOCK:
            _TODOS = items
        on_todo = (hooks or {}).get("on_todo")
        if on_todo:
            on_todo(items)
        raw_result = {"result": "ok", "todos": items}
    elif name == "run_code":
        raw_result = _run_code(args.get("language"), args.get("code"))
    elif name in _blender_tool_names():
        if not MCP_AVAILABLE:
            raw_result = {"error": "Blender MCP is not installed (pip install mcp-for-blender)"}
        else:
            res = _blender_tool_call(name, args)
            if res is None:
                raw_result = {"error": BLENDER_OFFLINE_MSG}
            else:
                if res.get("image_data"):
                    image_uri = res["image_data"]
                body = res.get("result")
                err = res.get("error")
                if err:
                    raw_result = {"error": err}
                elif body:
                    raw_result = {"result": body}
                else:
                    raw_result = {"result": "done (see capture)"}
    else:
        raw_result = _exec_file_tool(name, args)
    result = public_result(_clip_result(raw_result))
    full = _clip_result(raw_result)
    record = {"name": name, "arguments": args, "result": result,
              "full_result": full,
              "tool_call_id": tc.get("id") or "call_" + str(len(name))}
    if image_uri:
        record["image_data"] = image_uri
    return record


def _pick_folder():
    code = ("import tkinter as tk; from tkinter import filedialog; "
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "p = filedialog.askdirectory(title='Choose workspace folder'); "
            "r.destroy(); print(p or '')")
    try:
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=180)
        path = (out.stdout or "").strip()
        return os.path.realpath(path) if path else None
    except Exception:
        return None


def _confirm_pick_workdir():
    global WORKDIR
    chosen = _pick_folder()
    if not chosen:
        return {"cancelled": True}
    if not os.path.isdir(chosen):
        os.makedirs(chosen, exist_ok=True)
    WORKDIR = chosen
    return {"workdir": WORKDIR}


def _empty_stats():
    return {"think_ms": 0, "respond_ms": 0, "total_ms": 0,
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cached_tokens": 0, "tok_s": 0, "ctx_used": 0, "ctx_left": BONSAI_CTX}


def _stats_from(usage, timings):
    prompt = usage.get("prompt_tokens") or 0
    comp = usage.get("completion_tokens") or 0
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return {"think_ms": int(timings.get("prompt_ms") or 0),
            "respond_ms": int(timings.get("predicted_ms") or 0),
            "total_ms": int((timings.get("prompt_ms") or 0) + (timings.get("predicted_ms") or 0)),
            "prompt_tokens": prompt, "completion_tokens": comp,
            "total_tokens": usage.get("total_tokens") or 0, "cached_tokens": cached,
            "tok_s": timings.get("predicted_per_second") or 0,
            "ctx_used": prompt + comp, "ctx_left": BONSAI_CTX - prompt - comp}


def _merge_stats(a, b):
    out = dict(a)
    for k in ("think_ms", "respond_ms", "total_ms", "prompt_tokens",
              "completion_tokens", "total_tokens", "cached_tokens"):
        out[k] = (a.get(k) or 0) + (b.get(k) or 0)
    if (b.get("completion_tokens") or 0) > 0:
        a_tok = a.get("completion_tokens") or 0
        b_tok = b.get("completion_tokens") or 0
        out["tok_s"] = round(((a.get("tok_s") or 0) * a_tok + (b.get("tok_s") or 0) * b_tok)
                             / (a_tok + b_tok), 1)
    out["ctx_used"] = b.get("ctx_used") or a.get("ctx_used") or 0
    out["ctx_left"] = BONSAI_CTX - out["ctx_used"]
    return out


def run_agent(messages, on_tool=None, mode=MODE_BUILD, on_stats=None, hooks=None):
    calls = []
    tools = _tools_for(mode)
    msgs = list(messages)
    total = _empty_stats()
    for _ in range(MAX_TOOL_ROUNDS):
        message = _bonsai_chat(msgs, tools)
        total = _merge_stats(total, _stats_from(message.get("_usage") or {},
                                                message.get("_timings") or {}))
        tool_calls = message.get("tool_calls") or []
        content = message.get("content")
        if not tool_calls:
            if on_stats:
                on_stats(total)
            return {"reply": (content or "").strip(), "calls": calls, "_stats": total}
        msgs.append({"role": "assistant", "content": content,
                     "tool_calls": [{"id": tc.get("id"),
                                     "type": "function",
                                     "function": tc.get("function")}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            record = execute_tool_call(tc, hooks=hooks)
            image_uri = record.pop("image_data", None)
            calls.append(record)
            if on_tool:
                on_tool(record)
            msgs.append({"role": "tool", "tool_call_id": record["tool_call_id"],
                         "content": json.dumps(record["full_result"],
                                               ensure_ascii=False)})
            if image_uri:
                msgs.append({"role": "user",
                             "content": [{"type": "text",
                                          "text": "[I just captured this screenshot - inspect it carefully.]"},
                                         {"type": "image_url",
                                          "image_url": {"url": image_uri}}]})
    if on_stats:
        on_stats(total)
    return {"reply": "Done - completed the steps that could be executed.", "calls": calls, "_stats": total}


def _append_shot_image(msgs, record):
    image_uri = record.get("preview")
    if image_uri:
        msgs.append({"role": "user",
                     "content": [{"type": "text",
                                  "text": "[I just captured this screenshot - inspect it carefully.]"},
                                 {"type": "image_url",
                                  "image_url": {"url": image_uri}}]})


def stream_agent(messages, on_reason=None, on_delta=None, on_tool=None,
                 mode=MODE_BUILD, on_stats=None, hooks=None):
    calls = []
    tools = _tools_for(mode)
    msgs = list(messages)
    total = _empty_stats()
    for _round in range(MAX_TOOL_ROUNDS):
        tool_calls = None
        for ev in _bonsai_stream(msgs, tools):
            if ev["kind"] == "reason":
                if on_reason:
                    on_reason(ev["text"])
            elif ev["kind"] == "delta":
                if on_delta:
                    on_delta(ev["text"])
            elif ev["kind"] == "end":
                tool_calls = ev["tool_calls"]
                total = _merge_stats(total, ev.get("stats") or {})
                if on_stats:
                    on_stats(total)
        if not tool_calls:
            return {"calls": calls, "_stats": total}
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": tc.get("id"),
                                     "type": "function",
                                     "function": tc.get("function")}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            record = execute_tool_call(tc, hooks=hooks)
            if record.get("image_data"):
                record["preview"] = record.pop("image_data")
            calls.append(record)
            if on_tool:
                on_tool(record)
            msgs.append({"role": "tool", "tool_call_id": record["tool_call_id"],
                         "content": json.dumps(record["full_result"],
                                               ensure_ascii=False)})
            _append_shot_image(msgs, record)
    return {"calls": calls}


def handle_chat(messages, on_reason=None, on_delta=None, on_tool=None,
                stream=False, mode=MODE_BUILD, on_stats=None, hooks=None):
    msgs = build_messages(messages, mode=mode)
    if stream:
        return stream_agent(msgs, on_reason=on_reason, on_delta=on_delta,
                            on_tool=on_tool, mode=mode, on_stats=on_stats,
                            hooks=hooks)
    return run_agent(msgs, on_tool=on_tool, mode=mode, on_stats=on_stats,
                     hooks=hooks)


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BONSAI - PC Assistant</title>
<style>
  :root {
    --cyan-glow: #00f0ff;
    --blue-glow: #0072ff;
    --gold-glow: #ffd700;
    --red-glow: #ff0055;
    --hud-bg: rgba(6, 15, 25, 0.88);
    --panel-border: rgba(0, 240, 255, 0.3);
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: "Segoe UI", system-ui, sans-serif;
    background-color: #02060d; color: #00f0ff; overflow-x: hidden;
    background-image:
      radial-gradient(circle at 50% 50%, rgba(0, 114, 255, 0.15) 0%, transparent 70%),
      linear-gradient(rgba(0,240,255,.03) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,240,255,.03) 1px, transparent 1px);
    background-size: 100% 100%, 30px 30px, 30px 30px;
  }
  .mono { font-family: Consolas, "Courier New", monospace; }
  ::-webkit-scrollbar { width: 5px; height: 5px; }
  ::-webkit-scrollbar-track { background: rgba(2,6,13,.8); }
  ::-webkit-scrollbar-thumb { background: rgba(0,240,255,.4); border-radius: 3px; }
  ::-webkit-scrollbar-thumb:hover { background: rgba(0,240,255,.8); }

  .hud-border {
    border: 1px solid var(--panel-border);
    box-shadow: 0 0 15px rgba(0,240,255,.15), inset 0 0 15px rgba(0,240,255,.05);
    backdrop-filter: blur(12px); background: var(--hud-bg); position: relative;
  }
  .hud-border::before, .hud-border::after { content: ''; position: absolute; width: 10px; height: 10px; pointer-events: none; }
  .hud-border::before { top: -2px; left: -2px; border-top: 2px solid #00f0ff; border-left: 2px solid #00f0ff; }
  .hud-border::after { bottom: -2px; right: -2px; border-bottom: 2px solid #00f0ff; border-right: 2px solid #00f0ff; }

  .app { display: flex; flex-direction: column; height: 100vh; padding: 10px; }
  header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; padding: 10px 14px; border-radius: 10px; }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand .chip-ic {
    width: 34px; height: 34px; border-radius: 50%; border: 1px solid #00f0ff;
    display: flex; align-items: center; justify-content: center; background: rgba(0,240,255,.08); font-size: 16px;
  }
  .brand h1 { font-size: 19px; margin: 0; letter-spacing: 3px; color: #67e8f9; font-family: Consolas, monospace; font-weight: 700; }
  .brand p { margin: 0; font-size: 11px; color: #0e7490; letter-spacing: 2px; font-family: Consolas, monospace; }
  .hstatus { display: flex; gap: 16px; font-size: 13px; color: #9ca3af; font-family: Consolas, monospace; }
  .hstatus b { color: inherit; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #34d399; margin-right: 4px; }
  .hclock { text-align: right; font-family: Consolas, monospace; }
  .hclock .t { font-size: 18px; font-weight: 700; color: #67e8f9; }
  .hclock .d { font-size: 11px; color: #0e7490; }
  .hbtn {
    background: rgba(0,240,255,.05); border: 1px solid rgba(0,240,255,.3); color: #67e8f9;
    padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; transition: all .2s;
  }
  .hbtn:hover { background: rgba(0,240,255,.2); border-color: #00f0ff; box-shadow: 0 0 12px rgba(0,240,255,.4); }

  main.grid {
    display: grid; grid-template-columns: 250px 1fr 1.35fr; gap: 10px;
    flex: 1; min-height: 0; margin: 10px 0;
  }
  @media (max-width: 1180px) {
    main.grid { grid-template-columns: 220px 1fr; }
    .center { display: none; }
  }
  @media (max-width: 860px) {
    main.grid { grid-template-columns: 1fr; }
    .left { display: none; }
  }

  .panel { border-radius: 10px; display: flex; flex-direction: column; min-height: 0; }
  .center { align-items: center; justify-content: center; padding: 20px; }
  .right { min-height: 0; }

  .ptitle { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #164e63; padding: 10px 12px; font-size: 11px; letter-spacing: 2px; color: #22d3ee; font-family: Consolas, monospace; }

  /* left: chat history */
  .left .new { margin: 10px 12px; }
  #chatlist { flex: 1; overflow-y: auto; padding: 4px 8px; }
  .chat-item { display: flex; align-items: center; gap: 8px; padding: 9px 10px; border-radius: 8px; cursor: pointer; font-size: 14px; color: #99f6e4; }
  .chat-item:hover { background: rgba(0,240,255,.08); }
  .chat-item.active { background: rgba(0,240,255,.16); border: 1px solid rgba(0,240,255,.35); }
  .chat-item .tit { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-item .del { background: none; border: none; color: #0e7490; cursor: pointer; font-size: 14px; }
  .chat-item .del:hover { color: #ff0055; }

  /* center: arc reactor */
  .arc-container { position: relative; width: 210px; height: 210px; display: flex; align-items: center; justify-content: center; }
  .arc-container { --ring: rgba(0,240,255,.55); --core1: #00f0ff; --core2: rgba(0,114,255,.8); --glow1: #00f0ff; --glow2: #0072ff; }
  .arc-ring-outer { position: absolute; width: 100%; height: 100%; border-radius: 50%; border: 2px dashed var(--ring); animation: rotC 20s linear infinite; transition: border-color .3s; }
  .arc-ring-mid { position: absolute; width: 78%; height: 78%; border-radius: 50%; border: 2px solid transparent; border-top-color: var(--glow1); border-bottom-color: var(--glow2); animation: rotCC 8s linear infinite; transition: border-color .3s; }
  .arc-ring-inner { position: absolute; width: 58%; height: 58%; border-radius: 50%; border: 3px dotted var(--ring); animation: rotC 12s linear infinite; transition: border-color .3s; }
  .arc-core {
    position: absolute; width: 38%; height: 38%; border-radius: 50%;
    background: radial-gradient(circle, #fff 0%, var(--core1) 40%, var(--core2) 70%, transparent 100%);
    box-shadow: 0 0 35px var(--glow1), 0 0 60px var(--glow2); transition: all .3s ease;
  }
  @keyframes rotC { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  @keyframes rotCC { from { transform: rotate(360deg); } to { transform: rotate(0deg); } }

  .arc-container.thinking { --ring: rgba(255,215,0,.55); --core1: #ffd700; --core2: rgba(255,136,0,.8); --glow1: #ffd700; --glow2: #ff8800; }
  .arc-container.tools { --ring: rgba(57,255,20,.55); --core1: #39ff14; --core2: rgba(0,255,136,.8); --glow1: #39ff14; --glow2: #00e676; }

  .arc-container.thinking .arc-core { animation: pulseFast .3s infinite alternate; }
  .arc-container.tools .arc-core { animation: pulseGreen .4s infinite alternate; }
  .arc-container.thinking .arc-ring-outer { animation-duration: 6s; }
  .arc-container.tools .arc-ring-outer { animation-duration: 4s; }
  .arc-container.thinking .arc-ring-inner { animation-duration: 5s; }
  .arc-container.tools .arc-ring-inner { animation-duration: 3.5s; }
  @keyframes pulseFast { 0% { transform: scale(.95); opacity: .8; } 100% { transform: scale(1.1); opacity: 1; } }
  @keyframes pulseGreen { 0% { transform: scale(.92); box-shadow: 0 0 20px var(--glow1); } 100% { transform: scale(1.18); box-shadow: 0 0 55px var(--glow1), 0 0 85px var(--glow2); } }
  #waveform { display: flex; align-items: center; justify-content: center; gap: 6px; height: 40px; margin: 16px 0; }
  .wave-bar { width: 3px; height: 14px; border-radius: 2px; background-color: #00f0ff; transition: background-color .3s; }
  body.thinking .wave-bar { background-color: #ffd700; }
  body.tools .wave-bar { background-color: #39ff14; }
  .active-wave .wave-bar { animation: waveAnim .8s infinite ease-in-out alternate; }
  .wave-bar:nth-child(2) { animation-delay: .1s; } .wave-bar:nth-child(3) { animation-delay: .2s; }
  .wave-bar:nth-child(4) { animation-delay: .3s; } .wave-bar:nth-child(5) { animation-delay: .4s; }
  .wave-bar:nth-child(6) { animation-delay: .5s; } .wave-bar:nth-child(7) { animation-delay: .6s; }
  .wave-bar:nth-child(8) { animation-delay: .7s; } .wave-bar:nth-child(9) { animation-delay: .8s; }
  .wave-bar:nth-child(10) { animation-delay: .9s; }
  @keyframes waveAnim { 0% { height: 6px; } 100% { height: 35px; } }
  #bonsai-state-label { font-size: 13px; letter-spacing: 3px; color: #22d3ee; text-align: center; font-family: Consolas, monospace; text-transform: uppercase; margin: 0; }
  .sub { font-size: 11px; color: #0e7490; margin: 6px 0 0; text-align: center; font-family: Consolas, monospace; }

  /* meters for metrics */
  .meterrow { padding: 10px 12px; }
  .meter { margin-bottom: 12px; }
  .meter:last-child { margin-bottom: 0; }
  .meter .lab { display: flex; justify-content: space-between; font-size: 13px; margin-bottom: 5px; color: #9ca3af; font-family: Consolas, monospace; }
  .meter .lab b { color: #67e8f9; }
  .meter .bar { height: 6px; background: #0b1522; border: 1px solid #164e63; border-radius: 4px; overflow: hidden; }
  .meter .fill { height: 100%; border-radius: 4px; width: 0%; transition: width .5s; background: linear-gradient(90deg, #0072ff, #00f0ff); }
  .fill.gold { background: linear-gradient(90deg, #b45309, #ffd700); }

  /* right: chat console */
  .right .chat-wrap { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 10px; }
  #chat-container { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 4px 6px; }
  .msgrow { display: flex; gap: 10px; padding: 12px 0; }
  .msgrow.user { flex-direction: row-reverse; }
  .av { width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; font-family: Consolas, monospace; }
  .av.bonsai { background: rgba(0,240,255,.15); border: 1px solid rgba(0,240,255,.4); color: #67e8f9; }
  .av.me { background: rgba(255,215,0,.12); border: 1px solid rgba(255,215,0,.4); color: #fde047; }
  .bubble { max-width: 82%; padding: 10px 14px; border-radius: 12px; font-size: 16px; line-height: 1.6; overflow-wrap: anywhere; color: #d1f5f7; }
  .msgrow.user .bubble { background: rgba(0,240,255,.06); border: 1px solid rgba(0,240,255,.25); }
  .msgrow.bonsai .bubble { background: rgba(15,23,42,.55); border: 1px solid rgba(0,240,255,.2); box-shadow: 0 0 15px rgba(0,240,255,.06); }
  .bubble p { margin: 0 0 10px; } .bubble p:last-child { margin-bottom: 0; }
  .bubble code { background: rgba(0,240,255,.1); border-radius: 4px; padding: 1px 5px; font-family: Consolas, monospace; font-size: 14px; color: #a5f3fc; }
  .bubble pre { background: #041018; border: 1px solid #164e63; border-radius: 8px; padding: 12px; overflow-x: auto; font-size: 14px; color: #a5f3fc; }
  .bubble a { color: #22d3ee; }
  .toolchip { display: inline-flex; align-items: center; gap: 6px; background: rgba(0,240,255,.08); border: 1px solid rgba(0,240,255,.4); color: #67e8f9; border-radius: 999px; padding: 4px 12px; margin: 4px 6px 4px 0; font-size: 13px; font-family: Consolas, monospace; }
  .statschip { display: inline-block; background: rgba(255,215,0,.08); border: 1px solid rgba(255,215,0,.35); color: #fcd34d; border-radius: 6px; padding: 3px 10px; margin: 6px 0 2px; font-size: 11px; font-family: Consolas, monospace; }
  .toolchip.err { background: rgba(255,0,85,.08); border-color: rgba(255,0,85,.5); color: #ff859b; }
  .think { color: #0e7490; font-style: italic; font-size: 14px; display: flex; align-items: center; gap: 8px; font-family: Consolas, monospace; }
  .dots { display: inline-flex; gap: 3px; } .dots i { width: 5px; height: 5px; border-radius: 50%; background: #0e7490; animation: bl 1.2s infinite; } .dots i:nth-child(2){animation-delay:.2s} .dots i:nth-child(3){animation-delay:.4s}
  @keyframes bl { 0%,60%,100%{opacity:.25} 30%{opacity:1} }
  .caret::after { content: "\\258C"; color: #00f0ff; margin-left: 2px; animation: blink 1s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  .imggrid { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .imggrid img { width: 90px; height: 90px; object-fit: cover; border-radius: 10px; border: 1px solid #164e63; }
  .filechip { display: inline-flex; align-items: center; gap: 6px; background: rgba(0,240,255,.06); border: 1px solid rgba(0,240,255,.35); color: #67e8f9; border-radius: 8px; padding: 5px 10px; margin: 2px 6px 2px 0; font-size: 13px; font-family: Consolas, monospace; }
  .filechip .fname { font-weight: 700; }

  .composer { display: flex; align-items: flex-end; gap: 8px; padding: 8px 0 2px; }
  .field {
    flex: 1; position: relative; display: flex; align-items: flex-end; gap: 6px;
    background: rgba(2,10,20,.7); border: 1px solid rgba(0,240,255,.3); border-radius: 12px; padding: 8px; min-height: 52px;
  }
  .field:focus-within { border-color: #00f0ff; box-shadow: 0 0 12px rgba(0,240,255,.3); }
  #filein { display: none; }
  #user-input {
    flex: 1; background: transparent; border: none; outline: none; resize: none; color: #d1f5f7;
    font: inherit; font-size: 15px; padding: 8px 6px; max-height: 160px; min-width: 0;
  }
  #user-input::placeholder { color: #0e7490; }
  .iconbtn { background: none; border: none; cursor: pointer; font-size: 17px; padding: 8px; border-radius: 8px; color: #22d3ee; }
  .iconbtn:hover { background: rgba(0,240,255,.12); }
  #send {
    background: linear-gradient(135deg, #00f0ff, #0072ff); color: #02060d; border: none; border-radius: 12px;
    padding: 13px 16px; cursor: pointer; font-size: 14px; font-weight: 700; font-family: Consolas, monospace; transition: all .2s;
  }
  #send:hover { box-shadow: 0 0 18px rgba(0,240,255,.6); }
  #send:disabled { opacity: .4; cursor: default; box-shadow: none; }
  #send.stop { background: linear-gradient(135deg, #ff4d6d, #ff0055); color: #fff; box-shadow: 0 0 15px rgba(255,0,85,.5); }
  #queue {
    background: transparent; border: 1px solid rgba(0,240,255,.4); color: #22d3ee; border-radius: 12px;
    padding: 13px 12px; cursor: pointer; font-size: 13px; font-weight: 700; font-family: Consolas, monospace; transition: all .2s; white-space: nowrap;
  }
  #queue:hover { background: rgba(0,240,255,.12); box-shadow: 0 0 12px rgba(0,240,255,.4); }
  #queue.hasq { background: rgba(0,240,255,.2); border-color: #00f0ff; color: #67e8f9; }

  #preview { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .thumb { position: relative; }
  .thumb img { width: 62px; height: 62px; object-fit: cover; border-radius: 8px; border: 1px solid #164e63; }
  .thumb .x { position: absolute; top: -6px; right: -6px; background: #ff0055; color: #fff; border: none; border-radius: 50%; width: 20px; height: 20px; cursor: pointer; font-size: 12px; line-height: 1; }
  .pf { display: inline-flex; align-items: center; gap: 6px; background: rgba(0,240,255,.07); border: 1px solid rgba(0,240,255,.35); color: #67e8f9; border-radius: 8px; padding: 5px 10px; font-size: 13px; font-family: Consolas, monospace; }
  .pf .x { background: none; border: none; color: #ff859b; cursor: pointer; font-size: 13px; }

  /* reason / thinking box */
  .reasonbox { border: 1px solid rgba(180,120,255,.35); border-radius: 10px; background: rgba(40,15,70,.25); margin-bottom: 8px; overflow: hidden; }
  .reasonbox summary { cursor: pointer; padding: 6px 10px; font-size: 13px; font-family: Consolas, monospace; color: #c4b5fd; list-style: none; display: flex; align-items: center; gap: 6px; user-select: none; }
  .reasonbox summary::-webkit-details-marker { display: none; }
  .reasonbox summary::before { content: '\\25B8'; transition: transform .15s; }
  .reasonbox[open] summary::before { transform: rotate(90deg); }
  .reasonbox .rc { padding: 2px 10px 8px; max-height: 220px; overflow-y: auto; font-size: 12.5px; line-height: 1.55; color: #d6c7ff; white-space: pre-wrap; font-family: Consolas, monospace; }
  .reasonbox.live summary::after { content: '\\25CF'; color: #e879f9; animation: bl 1.2s infinite; margin-left: 4px; }
  .reasonbox.errtitle summary { color: #ff859b; }

  /* tool log */
  .toollog { border: 1px solid rgba(0,240,255,.3); border-radius: 10px; background: rgba(0,25,40,.25); margin-bottom: 8px; overflow: hidden; }
  .toollog summary { cursor: pointer; padding: 6px 10px; font-size: 13px; font-family: Consolas, monospace; color: #67e8f9; list-style: none; display: flex; align-items: center; gap: 6px; user-select: none; }
  .toollog summary::-webkit-details-marker { display: none; }
  .toollog summary::before { content: '\\25B8'; transition: transform .15s; }
  .toollog[open] summary::before { transform: rotate(90deg); }
  .toollog .tl { padding: 2px 10px 8px; max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .tlitem { border: 1px solid rgba(0,240,255,.18); border-radius: 8px; background: rgba(2,10,20,.6); padding: 6px 8px; font-size: 13px; font-family: Consolas, monospace; }
  .tlitem .nm { color: #00f0ff; font-weight: 700; }
  .tlitem .rs { color: #7dd3fc; white-space: pre-wrap; margin-top: 4px; }
  .tlitem .ar { color: #9ca3af; white-space: pre-wrap; margin-top: 2px; }
  .tlitem.err { border-color: rgba(255,0,85,.4); } .tlitem.err .nm { color: #ff859b; }

  /* mode toggle + workdir */
  .modebtn { background: rgba(255,215,0,.08); border: 1px solid rgba(255,215,0,.45); color: #fde047; padding: 7px 12px; border-radius: 8px; cursor: pointer; font-size: 13px; font-family: Consolas, monospace; font-weight: 700; letter-spacing: 1px; transition: all .2s; }
  .modebtn:hover { background: rgba(255,215,0,.2); box-shadow: 0 0 12px rgba(255,215,0,.4); }
  .modebtn.plan { border-color: rgba(103,232,249,.5); background: rgba(0,240,255,.1); color: #67e8f9; }
  .wbtn { background: rgba(0,240,255,.05); border: 1px solid rgba(0,240,255,.3); color: #67e8f9; padding: 7px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; font-family: Consolas, monospace; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wbtn:hover { background: rgba(0,240,255,.18); }
  .blstatus { color: #9ca3af; font-size: 12px; font-family: Consolas, monospace; padding: 7px 6px; letter-spacing: .5px; white-space: nowrap; }
  .blstatus.on { color: #34d399; text-shadow: 0 0 8px rgba(52,211,153,.5); }
  .blstatus.mid { color: #fbbf24; }
  .blstatus.off { color: #f87171; }

  footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 6px 14px; border-radius: 10px; font-size: 13px; color: #0e7490; font-family: Consolas, monospace; letter-spacing: 1px; }
  footer b { color: #22d3ee; }
  .fine { text-align: center; color: #0e7490; font-size: 12px; margin-top: 6px; font-family: Consolas, monospace; }
  .statsline { min-height: 16px; padding: 2px 4px 0; font-size: 11px; color: #67e8f9; font-family: Consolas, monospace; letter-spacing: 0; opacity: .85; }
  .statsline b { color: #00f0ff; font-weight: 600; }
  .statsline .sdim { color: #0e7490; }

  /* thinking effort selector */
  #effort-sel {
    background: rgba(0,240,255,.05); border: 1px solid rgba(0,240,255,.3); color: #67e8f9;
    border-radius: 12px; padding: 13px 8px; cursor: pointer; font-size: 12px; font-family: Consolas, monospace;
    font-weight: 700; outline: none; transition: all .2s;
  }
  #effort-sel option { background: #031018; color: #99f6e4; }
  #effort-sel:hover { border-color: #00f0ff; box-shadow: 0 0 12px rgba(0,240,255,.3); }

  /* todo panel */
  #todopanel { display: none; padding: 4px 8px; }
  #todopanel.on { display: block; }
  #todopanel .todo { display: flex; align-items: flex-start; gap: 8px; padding: 5px 6px; border-radius: 6px; font-size: 13px; font-family: Consolas, monospace; }
  #todopanel .todo .st { width: 14px; flex: 0 0 14px; }
  #todopanel .todo.pending { color: #94a3b8; }
  #todopanel .todo.in_progress { color: #fde047; }
  #todopanel .todo.completed { color: #4ade80; text-decoration: line-through; opacity: .75; }

  /* ask_user dialog */
  .askov { position: fixed; inset: 0; background: rgba(2,6,16,.72); backdrop-filter: blur(3px); display: flex; align-items: center; justify-content: center; z-index: 60; }
  .askbox { width: min(540px, 92vw); background: #031018; border: 1px solid rgba(0,240,255,.5); border-radius: 14px; padding: 18px; box-shadow: 0 0 30px rgba(0,240,255,.35); font-family: Consolas, monospace; }
  .askbox h4 { margin: 0 0 10px; color: #00f0ff; font-size: 14px; letter-spacing: 1px; }
  .askbox .aq { color: #d1f5f7; font-size: 15px; line-height: 1.5; white-space: pre-wrap; }
  .askbox .opts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .askbox .opt { background: rgba(0,240,255,.08); border: 1px solid rgba(0,240,255,.45); color: #67e8f9; border-radius: 10px; padding: 8px 14px; cursor: pointer; font-size: 13px; font-family: Consolas, monospace; }
  .askbox .opt:hover { background: rgba(0,240,255,.2); box-shadow: 0 0 12px rgba(0,240,255,.4); }
  .askbox .afree { display: flex; gap: 8px; margin-top: 14px; }
  .askbox .afree input { flex: 1; min-width: 0; background: rgba(2,10,20,.8); border: 1px solid rgba(0,240,255,.35); border-radius: 10px; padding: 10px; color: #d1f5f7; font-size: 14px; font-family: Consolas, monospace; outline: none; }
  .askbox .afree input:focus { border-color: #00f0ff; }
  .askbox .afree button { background: linear-gradient(135deg, #00f0ff, #0072ff); color: #02060d; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 700; cursor: pointer; font-size: 14px; font-family: Consolas, monospace; }

  /* screenshot thumb in tool log */
  .tlitem .tthumb { max-width: 220px; border-radius: 6px; border: 1px solid #164e63; margin-top: 6px; display: block; }

  /* drag & drop attach overlay */
  .chat-wrap { position: relative; }
  .dropov { position: absolute; inset: 0; z-index: 70; display: none; align-items: center; justify-content: center;
    background: rgba(0,240,255,.07); border: 2px dashed #00f0ff; border-radius: 14px;
    font-family: Consolas, monospace; color: #67e8f9; font-size: 15px; letter-spacing: 1px;
    text-shadow: 0 0 10px rgba(0,240,255,.8); pointer-events: none; }
  .dropov.on { display: flex; }
  .dropov b { color: #00f0ff; }
</style>
</head>
<body>
<div class="app">
  <header class="hud-border">
    <div class="brand">
      <div class="chip-ic">&#9679;</div>
      <div>
        <h1>BONSAI</h1>
        <p>BONSAI 2 &middot; PC ASSISTANT</p>
      </div>
    </div>
    <div class="hstatus">
      <span><span class="dot" id="sdot"></span>STATUS: <b id="system-status-text">ONLINE / OPTIMAL</b></span>
      <span>MODEL: <b id="modelbadge">Bonsai 2 &middot; 27B</b></span>
      <span>VISION: <b>mmproj ON</b></span>
    </div>
    <div class="hstatus" style="gap:10px; align-items:center;">
      <div class="hclock">
        <div class="t" id="clock-time">00:00:00</div>
        <div class="d" id="clock-date"></div>
      </div>
      <button class="modebtn" id="modebtn" title="Switch Plan / Build mode">BUILD</button>
      <button class="wbtn" id="workbtn" title="Click to choose the workspace folder">\\WORKSPACE</button>
      <button class="hbtn" id="ejectbtn" title="Unload the model from RAM/VRAM now (it loads back on the next message)">EJECT</button>
      <span class="blstatus off" id="blstatus" title="Blender MCP status">BLENDER: CHECKING</span>
    </div>
  </header>

  <main class="grid">
    <section class="left panel hud-border">
      <div class="ptitle"><span>CHAT HISTORY</span><span>LOCAL</span></div>
      <button class="hbtn new" id="newchat2">+ NEW CHAT</button>
      <div id="chatlist"></div>
      <div class="ptitle" style="margin-top:8px; border-top:1px solid #164e63; padding-top:8px;"><span>TODO</span><span id="todocount"></span></div>
      <div id="todopanel"></div>
    </section>

    <section class="center panel hud-border">
      <div id="arc-reactor" class="arc-container" title="PC Assistant">
        <div class="arc-ring-outer"></div>
        <div class="arc-ring-mid"></div>
        <div class="arc-ring-inner"></div>
        <div class="arc-core"></div>
      </div>
      <div id="waveform">
        <div class="wave-bar"></div><div class="wave-bar"></div><div class="wave-bar"></div>
        <div class="wave-bar"></div><div class="wave-bar"></div><div class="wave-bar"></div>
        <div class="wave-bar"></div><div class="wave-bar"></div><div class="wave-bar"></div>
        <div class="wave-bar"></div>
      </div>
      <p id="bonsai-state-label">BONSAI READY</p>
      <p class="sub">Awaiting your command</p>
      <div class="meterrow" style="width:100%; margin-top:8px;">
        <div class="meter"><div class="lab"><span>CPU</span><b id="cpu-val">0%</b></div><div class="bar"><div class="fill" id="cpu-bar"></div></div></div>
        <div class="meter"><div class="lab"><span>RAM</span><b id="ram-val">0%</b></div><div class="bar"><div class="fill" id="ram-bar"></div></div></div>
        <div class="meter"><div class="lab"><span>GPU</span><b id="gpu-val">0%</b></div><div class="bar"><div class="fill gold" id="gpu-bar"></div></div></div>
      </div>
    </section>

    <section class="right panel hud-border">
      <div class="ptitle"><span>CONVERSATION CONSOLE</span></div>
      <div class="chat-wrap">
        <div id="chat-container"></div>
        <div id="preview"></div>
        <form id="chat-form" class="composer" onsubmit="handleSubmit(event)">
          <div class="field">
            <textarea id="user-input" rows="1" placeholder="Type a command..."></textarea>
            <button type="button" class="iconbtn" id="attach" title="Attach images / files">&#128206;</button>
            <button type="button" class="iconbtn" id="mic-btn" title="Microphone">&#127908;</button>
            <button type="button" id="queue" title="Queue this message - I will answer it after the current reply">QUEUE</button>
            <select id="effort-sel" title="Thinking effort - how deeply BONSAI reasons (this can change the response quality)">
              <option value="off">THINK: OFF</option>
              <option value="low">THINK: LOW</option>
              <option value="med" selected>THINK: MED</option>
              <option value="high">THINK: HIGH</option>
            </select>
            <button type="submit" id="send">SEND</button>
          </div>
        </form>
        <div class="statsline" id="statsline"></div>
      </div>
    </section>
  </main>

  <footer class="hud-border">
    <div>SYSTEM STATUS: <b>100% OPERATIONAL</b></div>
    <div>MODEL: <b>BONSAI 2 27B</b> &middot; VISION+TOOLS</div>
    <div>BONSAI &middot; PC ASSISTANT</div>
  </footer>
</div>
<input type="file" id="filein" accept="image/*,.txt,.md,.py,.js,.ts,.json,.csv,.log,.ini,.cfg,.xml,.html,.css,.bat,.ps1,.sh,.yml,.yaml,.sql,.java,.cpp,.c,.h,.cs,.go,.rb,.php,.toml,.env,.gitignore" multiple>
<script>
const BONSAI_CTX = 32768;
let chats = load();
let cur = null;
let busy = false;
let pendingAtt = [];
let started = false;
let abortCtrl = null;
let msgQueue = [];
let chatMode = localStorage.getItem('jarvis_mode') === 'plan' ? 'plan' : 'build';
let workdir = '';

function load() {
  try { return JSON.parse(localStorage.getItem('jarvis_chats') || '[]'); } catch (e) { return []; }
}
function save() {
  try {
    const c = chats.slice(-200).map(function (ch) {
      const copy = JSON.parse(JSON.stringify(ch));
      (copy.messages || []).forEach(function (m) {
        if (m.calls) (m.calls).forEach(function (cl) { if (cl.preview) delete cl.preview; });
      });
      return copy;
    });
    localStorage.setItem('jarvis_chats', JSON.stringify(c));
    fetch('/api/chats', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ chats: c }) }).catch(function () {});
  } catch (e) {}
}
async function serverLoad() {
  try {
    const r = await fetch('/api/chats');
    const j = await r.json();
    if (j && Array.isArray(j.chats) && j.chats.length) {
      chats = j.chats;
      if (!cur || !chats.some(function (c) { return c.id === cur.id; })) cur = chats[chats.length - 1];
      renderAll();
      return true;
    }
  } catch (e) {}
  return false;
}
function newChat() {
  if (cur && chats.indexOf(cur) !== -1 && cur.messages.length === 0 && cur.title === 'New chat') {
    started = false;
    renderAll();
    return;
  }
  if (cur && cur.messages.length > 0 && chats.indexOf(cur) === -1) chats.push(cur);
  cur = { id: 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
          title: 'New chat', ts: Date.now(), messages: [] };
  chats.push(cur);
  started = false;
  save();
  renderAll();
}
function init() {
  bindModeBtn();
  loadWorkdir();
  if (!chats.length) newChat();
  else cur = chats[chats.length - 1];
  renderAll();
  setSendUI();
  setQueueUI();
  serverLoad();
  fetch('/api/todos').then(function (r) { return r.json(); }).then(function (j) {
    if (j && j.todos) renderTodo(j.todos);
  }).catch(function () {});
}
function bindModeBtn() {
  const btn = document.getElementById('modebtn');
  const update = function () {
    btn.textContent = chatMode === 'plan' ? 'PLAN' : 'BUILD';
    btn.classList.toggle('plan', chatMode === 'plan');
  };
  update();
  btn.onclick = function () {
    chatMode = chatMode === 'plan' ? 'build' : 'plan';
    localStorage.setItem('jarvis_mode', chatMode);
    update();
  };
}
async function loadWorkdir() {
  try {
    const r = await fetch('/api/workdir');
    const j = await r.json();
    if (j.workdir) setWorkdirInUI(j.workdir);
  } catch (e) { /* offline */ }
}
function renderAll() { renderList(); renderConv(); }
function renderList() {
  const el = document.getElementById('chatlist');
  el.innerHTML = '';
  chats.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).forEach(function (c) {
    const row = document.createElement('div');
    row.className = 'chat-item' + (cur && c.id === cur.id ? ' active' : '');
    const t = document.createElement('span'); t.className = 'tit'; t.textContent = c.title; t.title = c.title;
    const d = document.createElement('button'); d.className = 'del'; d.textContent = '\u00d7';
    d.onclick = function (e) { e.stopPropagation(); chats = chats.filter(function (x) { return x.id !== c.id; }); if (!chats.length) { cur = null; newChat(); } else { if (cur && cur.id === c.id) cur = chats[chats.length - 1]; save(); renderAll(); } };
    row.onclick = function () { cur = c; started = cur.messages.length > 0; renderAll(); setBonsaiState('idle'); };
    row.appendChild(t); row.appendChild(d);
    el.appendChild(row);
  });
}
function convEl() { return document.getElementById('chat-container'); }
function scrollBottom() { const c = convEl(); c.scrollTop = c.scrollHeight; }
function renderConv() {
  const conv = convEl();
  conv.innerHTML = '';
  if (cur) {
    cur.messages.forEach(function (m) {
      if (m.role === 'user') addUser(m.content);
      else if (m.role === 'assistant') addAsst(m.content, m.calls || [], m.reason, m.stats);
    });
  }
  busy = false;
}
function esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function fmt(s) {
  let t = esc(s);
  t = t.replace(/```([\\s\\S]*?)```/g, '<pre>$1</pre>');
  t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
  t = t.replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>');
  t = t.replace(/\\*([^*]+)\\*/g, '<i>$1</i>');
  t = t.replace(/(https?:\\/\\/[^\\s<]+)/g, '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
  t = t.replace(/\\n/g, '<br>');
  return t;
}
function fileChipDom(name) {
  const c = document.createElement('span'); c.className = 'filechip';
  c.textContent = '\\ud83d\\udcc4 ' + name;
  return c;
}
function addUser(content) {
  const conv = convEl();
  const row = document.createElement('div'); row.className = 'msgrow user';
  const av = document.createElement('div'); av.className = 'av me'; av.textContent = 'U';
  const b = document.createElement('div'); b.className = 'bubble';
  const parts = typeof content === 'string' ? [{ type: 'text', text: content }] : content;
  const arr = parts || [];
  (arr.filter(function (p) { return p.type === 'image_url'; })).forEach(function (p) {
    const g = document.createElement('div'); g.className = 'imggrid';
    const im = document.createElement('img'); im.src = p.image_url.url; g.appendChild(im);
    b.appendChild(g);
  });
  (arr.filter(function (p) { return p.type === 'file_att'; })).forEach(function (p) {
    b.appendChild(fileChipDom(p.name));
  });
  const text = arr.filter(function (p) { return p.type === 'text'; }).map(function (p) { return p.text; }).join(' ');
  const hasImgs = arr.some(function (p) { return p.type === 'image_url'; });
  if (text) { const sp = document.createElement('div'); sp.innerHTML = fmt(text); b.appendChild(sp); }
  else if (hasImgs && !arr.some(function (p) { return p.type === 'file_att'; })) { const sp = document.createElement('div'); sp.textContent = '[Image attached]'; b.appendChild(sp); }
  row.appendChild(b); row.appendChild(av);
  conv.appendChild(row);
  scrollBottom();
}
function addAsst(text, calls, reason, stats) {
  const row = document.createElement('div'); row.className = 'msgrow bonsai';
  const av = document.createElement('div'); av.className = 'av bonsai'; av.textContent = 'B';
  const b = document.createElement('div'); b.className = 'bubble';
  if (reason) addReasonBox(b, reason);
  (calls || []).forEach(function (c) { toolChip(b, c); });
  if (calls && calls.length) addToolLog(b, calls);
  const inner = document.createElement('div'); inner.className = 'abody'; inner.innerHTML = fmt(text);
  b.appendChild(inner);
  if (stats) addStatsChip(b, stats);
  row.appendChild(av); row.appendChild(b);
  convEl().appendChild(row);
  scrollBottom();
  return inner;
}
function addStatsChip(container, s) {
  const c = document.createElement('span');
  c.className = 'statschip';
  const think = (s.think_ms || 0) / 1000;
  const speak = (s.respond_ms || 0) / 1000;
  let txt = 'THINK ' + think.toFixed(1) + 's &middot; SPEAK ' + speak.toFixed(1) + 's';
  if (s.tok_s) txt += ' &middot; ' + s.tok_s.toFixed(1) + ' tok/s';
  if (s.completion_tokens) txt += ' &middot; ' + s.completion_tokens + ' tok';
  if (s.ctx_used) txt += ' &middot; ctx ' + s.ctx_used + '/' + (s.ctx_used + (s.ctx_left || 0));
  c.innerHTML = '&#9202; ' + txt;
  container.appendChild(c);
}
function addReasonBox(container, text) {
  const d = document.createElement('details'); d.className = 'reasonbox';
  const s = document.createElement('summary'); s.textContent = 'Thinking';
  const c = document.createElement('div'); c.className = 'rc'; c.textContent = text;
  d.appendChild(s); d.appendChild(c);
  container.appendChild(d);
}
function addToolLog(container, calls) {
  const d = document.createElement('details'); d.className = 'toollog';
  const s = document.createElement('summary');
  s.textContent = 'Tool calls: ' + calls.length;
  const l = document.createElement('div'); l.className = 'tl';
  calls.forEach(function (c) {
    const it = document.createElement('div');
    it.className = 'tlitem' + (c.result && c.result.error ? ' err' : '');
    const nm = document.createElement('div'); nm.className = 'nm';
    nm.textContent = '\u2699 ' + c.name + ' ' + JSON.stringify(c.arguments || {});
    const rs = document.createElement('div'); rs.className = 'rs';
    rs.textContent = toolResultText(c.result);
    it.appendChild(nm); it.appendChild(rs);
    if (c.preview) {
      const im = document.createElement('img'); im.className = 'tthumb';
      im.src = c.preview; im.title = 'screenshot - click to enlarge';
      im.onclick = function () { window.open(c.preview); };
      it.appendChild(im);
    }
    l.appendChild(it);
  });
  d.appendChild(s); d.appendChild(l);
  container.appendChild(d);
}
function toolChip(container, c) {
  const chip = document.createElement('span');
  const res = c.result;
  const ok = res && res.result === 'ok';
  let label;
  if (ok) { const what = res.launched || res.opened || ''; label = '\u2699\ufe0f ' + (what || c.name); }
  else if (res && res.error) label = '\u26a0 ' + res.error.slice(0, 90);
  else label = '\u2699\ufe0f ' + c.name;
  chip.className = 'toolchip' + (res && res.error ? ' err' : '');
  chip.textContent = label;
  const chipData = JSON.parse(JSON.stringify(c));
  if (chipData.preview) delete chipData.preview;
  chip.title = JSON.stringify(chipData, null, 2);
  container.appendChild(chip);
}

function buildUserMsg() {
  const text = document.getElementById('user-input').value.trim();
  if (!text && !pendingAtt.length) return null;
  const parts = [];
  if (text) parts.push({ type: 'text', text: text });
  pendingAtt.forEach(function (a) {
    if (a.type === 'img') parts.push({ type: 'image_url', image_url: { url: a.src } });
    else parts.push({ type: 'file_att', name: a.name, text: a.text });
  });
  return parts;
}

function handleSubmit(e) {
  e.preventDefault();
  if (busy) { stopRun(); return; }
  go();
}

function setSendUI() {
  const b = document.getElementById('send');
  if (busy) { b.textContent = 'STOP'; b.disabled = false; b.classList.add('stop'); }
  else { b.textContent = 'SEND'; b.classList.remove('stop'); }
}
function setQueueUI() {
  const q = document.getElementById('queue');
  q.textContent = msgQueue.length ? ('QUEUE (' + msgQueue.length + ')') : 'QUEUE';
  q.classList.toggle('hasq', msgQueue.length > 0);
}
function stopRun() {
  if (abortCtrl) { const a = abortCtrl; abortCtrl = null; try { a.abort(); } catch (e) {} }
  if (thinkingRow) doneThinking('(stopped by user)');
  setBonsaiState('idle');
}
function drainQueue() {
  if (busy) return;
  if (!msgQueue.length) return;
  const next = msgQueue.shift();
  setQueueUI();
  go(next.parts, next.chat);
}

async function go(forcedParts, chat) {
  if (busy) return;
  if (!cur) newChat();
  if (chat && chat !== cur && chats.indexOf(chat) !== -1) { cur = chat; renderAll(); }
  const parts = forcedParts || buildUserMsg();
  if (!parts) return;
  busy = true;
  setSendUI();

  const target = cur;

  const textOf = parts.filter(function (p) { return p.type === 'text'; }).map(function (p) { return p.text; }).join(' ');
  const hasFile = parts.some(function (p) { return p.type === 'file_att'; });
  const hasImg = parts.some(function (p) { return p.type === 'image_url'; });
  const label = [textOf, hasFile ? 'files' : '', hasImg ? 'images' : ''].filter(Boolean).join(' + ').slice(0, 34);
  if (!started) {
    started = true;
    cur.title = label || 'Conversation';
    cur.title = cur.title.replace(/&#\\d+;/g, '');
    if (chats.indexOf(cur) === -1) chats.push(cur);
  }

  const raw = parts.length === 1 && parts[0].type === 'text' ? parts[0].text : parts;
  target.messages.push({ role: 'user', content: raw });
  addUser(parts);

  addThinking();
  setBonsaiState('thinking');
  pendingAtt = [];
  renderPreview();
  document.getElementById('user-input').value = '';
  try { await streamRun(target.messages.slice(), target); }
  catch (err) { doneThinking('Error: ' + err.message); setBonsaiState('idle'); }
  busy = false;
  setSendUI();
  document.getElementById('user-input').focus();
  target.ts = Date.now();
  save();
  renderList();
  drainQueue();
}

let thinkingRow = null;
let liveCalls = [];
let statsTimer = null;
let statsVals = { think_ms: 0, respond_ms: 0, tok_s: 0, ctx_used: 0, ctx_left: 0, prompt_tokens: 0, completion_tokens: 0 };
function statLine() { return document.getElementById('statsline'); }
function fmtMs(ms) {
  if (!ms && ms !== 0) return '--';
  return (ms / 1000).toFixed(1) + 's';
}
function runningStats() {
  const el = statLine();
  if (!el) return;
  const v = statsVals;
  const think = fmtMs(v.think_ms || 0);
  const respond = fmtMs(v.respond_ms || 0);
  const ts = v.tok_s ? v.tok_s.toFixed(1) : '--';
  const ctx = v.ctx_left ? (v.ctx_used) + ' / ' + (v.ctx_used + v.ctx_left) : '--';
  el.innerHTML = 'THINK <b>' + think + '</b> &middot; SPEAK <b>' + respond + '</b> &middot; <b>' + ts + '</b> tok/s &middot; tokens <b>' + (v.completion_tokens || 0) + '</b> &middot; ctx <span class="sdim">' + ctx + '</span>';
}
function startStats() {
  statsVals = { think_ms: 0, respond_ms: 0, tok_s: 0, ctx_used: 0, ctx_left: 0, prompt_tokens: 0, completion_tokens: 0 };
  const el = statLine();
  if (el) el.innerHTML = 'THINK <b>--</b> &middot; SPEAK <b>--</b> &middot; <b>--</b> tok/s &middot; tokens <b>0</b> &middot; ctx <span class="sdim">--</span>';
  if (statsTimer) clearInterval(statsTimer);
  statsTimer = setInterval(runningStats, 250);
}
function stopStats() {
  if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  runningStats();
}
function clearStats() {
  if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
  const el = statLine();
  if (el) el.innerHTML = '';
}
function onStats(j) {
  if (!j) return;
  statsVals.think_ms = j.think_ms || 0;
  statsVals.respond_ms = j.respond_ms || 0;
  statsVals.tok_s = j.tok_s || 0;
  statsVals.ctx_used = j.ctx_used || 0;
  statsVals.ctx_left = j.ctx_left || (BONSAI_CTX - statsVals.ctx_used);
  statsVals.prompt_tokens = j.prompt_tokens || 0;
  statsVals.completion_tokens = j.completion_tokens || 0;
  runningStats();
}
function addThinking() {
  liveCalls = [];
  const row = document.createElement('div'); row.className = 'msgrow bonsai';
  const av = document.createElement('div'); av.className = 'av bonsai'; av.textContent = 'B';
  const b = document.createElement('div'); b.className = 'bubble';
  const t = document.createElement('div'); t.className = 'think';
  t.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> processing...';
  b.appendChild(t);
  row.appendChild(av); row.appendChild(b);
  convEl().appendChild(row);
  scrollBottom();
  thinkingRow = { row: row, b: b, body: null, reasonD: null, reasonC: null, toolEl: null };
}
function onDelta(txt) {
  if (!thinkingRow) return;
  if (!thinkingRow.body) {
    const t = thinkingRow.row.querySelector('.think');
    if (t) t.remove();
    thinkingRow.body = document.createElement('div'); thinkingRow.body.className = 'abody caret';
    thinkingRow.b.appendChild(thinkingRow.body);
  }
  thinkingRow.body.innerText += txt;
  scrollBottom();
}
function onReason(txt) {
  if (!thinkingRow) return;
  if (!thinkingRow.reasonD) {
    thinkingRow.reasonD = document.createElement('details');
    thinkingRow.reasonD.className = 'reasonbox live';
    const s = document.createElement('summary'); s.textContent = 'Thinking...';
    thinkingRow.reasonC = document.createElement('div'); thinkingRow.reasonC.className = 'rc';
    thinkingRow.reasonD.appendChild(s); thinkingRow.reasonD.appendChild(thinkingRow.reasonC);
    thinkingRow.b.appendChild(thinkingRow.reasonD);
  }
  thinkingRow.reasonC.textContent += txt;
  thinkingRow.reasonC.scrollTop = thinkingRow.reasonC.scrollHeight;
  scrollBottom();
}
function toolResultText(r) {
  if (typeof r === 'string') return r;
  if (!r || typeof r !== 'object') return String(r);
  const copy = {};
  Object.keys(r).forEach(function (k) { if (k !== 'preview') copy[k] = r[k]; });
  return JSON.stringify(copy, null, 2);
}
function toolItemDom(call) {
  const it = document.createElement('div');
  it.className = 'tlitem' + (call.result && call.result.error ? ' err' : '');
  const nm = document.createElement('div'); nm.className = 'nm';
  nm.textContent = '\u2699 ' + call.name + ' ' + JSON.stringify(call.arguments || {});
  const rs = document.createElement('div'); rs.className = 'rs';
  rs.textContent = toolResultText(call.result);
  it.appendChild(nm); it.appendChild(rs);
  if (call.preview) {
    const im = document.createElement('img'); im.className = 'tthumb';
    im.src = call.preview; im.title = 'screenshot - click to enlarge';
    im.onclick = function () { window.open(call.preview); };
    it.appendChild(im);
  }
  return it;
}
function onTool(call) {
  if (thinkingRow && thinkingRow.body) thinkingRow.body.classList.remove('caret');
  toolChip(thinkingRow.b, call);
  liveCalls.push(call);
  if (!thinkingRow.toolEl) {
    thinkingRow.toolEl = document.createElement('details');
    thinkingRow.toolEl.className = 'toollog';
    const s = document.createElement('summary'); s.textContent = 'Tools: ' + liveCalls.length;
    const l = document.createElement('div'); l.className = 'tl';
    thinkingRow.toolEl.appendChild(s); thinkingRow.toolEl.appendChild(l);
    thinkingRow.b.appendChild(thinkingRow.toolEl);
  }
  const sum = thinkingRow.toolEl.querySelector('summary');
  sum.textContent = 'Tools: ' + liveCalls.length;
  thinkingRow.toolEl.querySelector('.tl').appendChild(toolItemDom(call));
  scrollBottom();
}
function doneThinking(errMsg) {
  if (!thinkingRow) return;
  if (thinkingRow.reasonD) {
    thinkingRow.reasonD.classList.remove('live');
    const s = thinkingRow.reasonD.querySelector('summary');
    s.textContent = 'Thinking (BONSAI) \u2014 ' + (thinkingRow.reasonC.textContent.trim() ? thinkingRow.reasonC.textContent.length + ' chars' : 'empty');
  }
  if (thinkingRow.toolEl) {
    const s = thinkingRow.toolEl.querySelector('summary');
    s.textContent = 'Tool calls: ' + liveCalls.length;
  }
  if (thinkingRow.body) thinkingRow.body.classList.remove('caret');
  if (errMsg) { const e = document.createElement('div'); e.style.color = '#ff859b'; e.textContent = errMsg; thinkingRow.b.appendChild(e); }
  thinkingRow = null;
}

function effortValue() {
  const el = document.getElementById('effort-sel');
  return el ? el.value : 'med';
}

function onAsk(j) {
  const old = document.getElementById('askov');
  if (old) old.remove();
  const ov = document.createElement('div'); ov.className = 'askov'; ov.id = 'askov';
  const box = document.createElement('div'); box.className = 'askbox';
  const h = document.createElement('h4'); h.textContent = 'BONSAI is asking you';
  const q = document.createElement('div'); q.className = 'aq';
  q.textContent = j.question || 'What should I do?';
  box.appendChild(h); box.appendChild(q);
  const say = function (ans) {
    ov.remove();
    fetch('/api/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: j.id, answer: ans }) }).catch(function () {});
  };
  if (j.options && j.options.length) {
    const opts = document.createElement('div'); opts.className = 'opts';
    (j.options).forEach(function (o) {
      const b = document.createElement('button'); b.className = 'opt'; b.textContent = o;
      b.onclick = function () { say(o); };
      opts.appendChild(b);
    });
    box.appendChild(opts);
  }
  const free = document.createElement('div'); free.className = 'afree';
  const inp = document.createElement('input'); inp.placeholder = 'Type your answer...';
  const btn = document.createElement('button'); btn.textContent = 'SEND';
  const go = function () { const v = inp.value.trim(); if (v) say(v); };
  btn.onclick = go;
  inp.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); go(); } };
  free.appendChild(inp); free.appendChild(btn);
  box.appendChild(free);
  ov.appendChild(box);
  document.body.appendChild(ov);
  inp.focus();
}

function renderTodo(items) {
  const el = document.getElementById('todopanel');
  const cnt = document.getElementById('todocount');
  if (!el) return;
  if (!items || !items.length) {
    el.classList.remove('on'); el.innerHTML = '';
    if (cnt) cnt.textContent = '';
    return;
  }
  el.classList.add('on'); el.innerHTML = '';
  const done = items.filter(function (t) { return t.status === 'completed'; }).length;
  if (cnt) cnt.textContent = done + '/' + items.length;
  items.forEach(function (t) {
    const d = document.createElement('div');
    d.className = 'todo ' + (t.status || 'pending');
    const st = document.createElement('span'); st.className = 'st';
    st.textContent = t.status === 'completed' ? '\u2713' : (t.status === 'in_progress' ? '\u25CF' : '\u25CB');
    const tx = document.createElement('span'); tx.textContent = t.description || '';
    d.appendChild(st); d.appendChild(tx);
    el.appendChild(d);
  });
}

async function streamRun(messages, chat) {
  chat = chat || cur;
  abortCtrl = new AbortController();
  startStats();
  let resp;
  try {
    resp = await fetch('/api/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages: messages, mode: chatMode, effort: effortValue() }), signal: abortCtrl.signal });
  } catch (e) { stopStats(); throw new Error('stopped'); }
  if (!resp.ok || !resp.body) { stopStats(); const j = await resp.json().catch(function () { return {}; }); throw new Error(j.error || 'stream unavailable'); }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '', reply = '', calls = [], reason = '';
  let aborted = false;
  let startedAt = Date.now();
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf('\\n\\n')) >= 0) {
        const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
        let ev = 'message', data = '';
        block.split('\\n').forEach(function (line) {
          if (line.startsWith('event:')) ev = line.slice(6).trim();
          else if (line.startsWith('data:')) data += line.slice(5).trim();
        });
        if (!data) continue;
        let j; try { j = JSON.parse(data); } catch (e) { continue; }
        if (ev === 'delta') { reply += j.text; onDelta(j.text); statsVals.respond_ms = Date.now() - startedAt; }
        else if (ev === 'reason') { reason += j.text; onReason(j.text); setBonsaiState('thinking'); statsVals.think_ms = Date.now() - startedAt; }
        else if (ev === 'tool') { calls.push(j.call); onTool(j.call); setBonsaiState('tools'); }
        else if (ev === 'stats') { onStats(j); }
        else if (ev === 'ask') { onAsk(j); setBonsaiState('thinking'); }
        else if (ev === 'todo') { renderTodo(j.todos); }
        else if (ev === 'error') { doneThinking('Error: ' + j.text); setBonsaiState('idle'); stopStats(); throw new Error(j.text); }
        else if (ev === 'done') { doneThinking(); setBonsaiState('idle'); }
      }
    }
  } catch (e) {
    if (e.name === 'AbortError') aborted = true;
    else throw e;
  } finally {
    abortCtrl = null;
    stopStats();
  }
  if (aborted) throw new Error('stopped');
  const last = chat.messages[chat.messages.length - 1];
  const savedStats = statsVals && (statsVals.think_ms || statsVals.respond_ms || statsVals.completion_tokens)
      ? JSON.parse(JSON.stringify(statsVals)) : null;
  if (last && last.role === 'user') {
    chat.messages.push({ role: 'assistant', content: reply, calls: calls, reason: reason.trim() ? reason : undefined, stats: savedStats });
  } else if (last && last.role === 'assistant' && !last.calls && reply) {
    last.content = reply;
    last.reason = reason.trim() ? reason : undefined;
    last.stats = savedStats;
  }
}

function renderPreview() {
  const el = document.getElementById('preview'); el.innerHTML = '';
  pendingAtt.forEach(function (a, i) {
    if (a.type === 'img') {
      const th = document.createElement('div'); th.className = 'thumb';
      const img = document.createElement('img'); img.src = a.src;
      const x = document.createElement('button'); x.className = 'x'; x.textContent = '\u00d7';
      x.onclick = function () { pendingAtt.splice(i, 1); renderPreview(); };
      th.appendChild(img); th.appendChild(x);
      el.appendChild(th);
    } else {
      const pf = document.createElement('span'); pf.className = 'pf';
      pf.textContent = '\\ud83d\\udcc4 ' + a.name + ' (' + Math.round(a.text.length / 1024) + 'k)';
      const x = document.createElement('button'); x.className = 'x'; x.textContent = '\u00d7';
      x.onclick = function () { pendingAtt.splice(i, 1); renderPreview(); };
      pf.appendChild(x);
      el.appendChild(pf);
    }
  });
}

function addFiles(files) {
  const arr = Array.from(files || []);
  if (!arr.length) return;
  const slots = 4 - pendingAtt.length;
  if (slots <= 0) { alert('Too many attachments (max 4) - remove one first'); return; }
  arr.slice(0, slots).forEach(function (f) {
    const isImg = (f.type || '').indexOf('image/') === 0;
    if (isImg) {
      if (f.size > 10 * 1024 * 1024) return;
      const r = new FileReader();
      r.onload = function () { pendingAtt.push({ type: 'img', src: r.result }); renderPreview(); };
      r.readAsDataURL(f);
    } else {
      if (f.size > 200 * 1024) { pendingAtt.push({ type: 'file', name: f.name, text: '[file too large for direct read]' }); renderPreview(); return; }
      const r = new FileReader();
      r.onload = function () {
        let text = String(r.result || '');
        if (text.length > 60000) text = text.slice(0, 60000) + '\\n[... file truncated ...]';
        pendingAtt.push({ type: 'file', name: f.name, text: text });
        renderPreview();
      };
      r.readAsText(f, 'utf-8');
    }
  });
}

document.getElementById('attach').onclick = function () { document.getElementById('filein').click(); };
document.getElementById('filein').onchange = function () { addFiles(this.files); this.value = ''; };

(function () {
  const wrap = document.querySelector('.chat-wrap');
  const ov = document.createElement('div');
  ov.className = 'dropov';
  ov.innerHTML = 'DROP FILES <b>&#128206;</b> TO ATTACH (images / text)';
  wrap.appendChild(ov);
  let depth = 0;
  window.addEventListener('dragover', function (e) { e.preventDefault(); });
  window.addEventListener('drop', function (e) { e.preventDefault(); });
  wrap.addEventListener('dragenter', function (e) { e.preventDefault(); depth++; ov.classList.add('on'); });
  wrap.addEventListener('dragleave', function (e) { e.preventDefault(); depth = Math.max(0, depth - 1); if (!depth) ov.classList.remove('on'); });
  wrap.addEventListener('drop', function (e) {
    e.preventDefault(); e.stopPropagation();
    depth = 0; ov.classList.remove('on');
    const files = e.dataTransfer ? e.dataTransfer.files : null;
    if (files && files.length) { addFiles(files); return; }
    const txt = e.dataTransfer ? e.dataTransfer.getData('text/plain') : '';
    if (txt) { const inp = document.getElementById('user-input'); if (inp) inp.value += txt; }
  });
})();

document.getElementById('newchat2').onclick = function () { newChat(); setBonsaiState('idle'); };
document.getElementById('queue').onclick = function () {
  const parts = buildUserMsg();
  if (!parts) return;
  if (busy) {
    msgQueue.push({ parts: parts, chat: cur });
    pendingAtt = [];
    renderPreview();
    document.getElementById('user-input').value = '';
    setQueueUI();
  } else {
    document.getElementById('user-input').value = '';
    pendingAtt = [];
    renderPreview();
    go(parts);
  }
};
function setWorkdirInUI(wd) {
  workdir = wd;
  const w = document.getElementById('workbtn');
  w.textContent = '\\ud83d\\udcc1 ' + workdir;
  w.title = 'Working folder: ' + workdir;
}
document.getElementById('workbtn').onclick = async function () {
  try {
    const r = await fetch('/api/pick_workdir', { method: 'POST' });
    const j = await r.json();
    if (j && j.workdir) { setWorkdirInUI(j.workdir); return; }
    if (j && j.cancelled) return;
  } catch (e) {}
  const v = prompt('Working folder (workspace for files):', workdir || '');
  if (!v) return;
  try {
    const r = await fetch('/api/workdir', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ workdir: v }) });
    const j = await r.json();
    if (j.workdir) setWorkdirInUI(j.workdir);
  } catch (e) { alert('Could not change folder: ' + e.message); }
};

/* ---------- Blender MCP status ---------- */
function renderBlenderStatus(j) {
  const el = document.getElementById('blstatus');
  if (!el) return;
  const map = {
    ok: ['on', 'BLENDER: CONNECTED'],
    bridge: ['mid', 'BLENDER: ADDON OFF'],
    down: ['off', 'BLENDER: OFF'],
    missing: ['off', 'BLENDER: NO MCP']
  };
  const m = map[j.state || 'down'] || map.down;
  el.className = 'blstatus ' + m[0];
  el.textContent = m[1];
  el.title = j.detail ? ('Blender MCP: ' + (j.state || 'down') + ' - ' + j.detail)
                      : ('Blender MCP: ' + (j.state || 'down'));
}
function refreshBlender() {
  fetch('/api/blender').then(function (r) { return r.json(); })
    .then(renderBlenderStatus)
    .catch(function () { renderBlenderStatus({ state: 'down' }); });
}
refreshBlender();
setInterval(refreshBlender, 8000);

/* ---------- EJECT: unload the model from RAM/VRAM ---------- */
document.getElementById('ejectbtn').onclick = function () {
  const btn = document.getElementById('ejectbtn');
  if (btn.disabled) return;
  btn.disabled = true;
  btn.style.opacity = '0.5';
  fetch('/api/eject', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      btn.textContent = j.ok ? 'EJECTED' : 'FAILED';
      setTimeout(function () {
        btn.textContent = 'EJECT';
        btn.disabled = false;
        btn.style.opacity = '1';
      }, 2500);
    })
    .catch(function () {
      btn.textContent = 'FAILED';
      setTimeout(function () {
        btn.textContent = 'EJECT';
        btn.disabled = false;
        btn.style.opacity = '1';
      }, 2500);
    });
};

const inp = document.getElementById('user-input');
inp.addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 160) + 'px'; });
inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); handleSubmit(ev); } });

/* ---------- BONSAI FX: clock & metrics ---------- */
function updateClock() {
  const now = new Date();
  document.getElementById('clock-time').textContent = now.toLocaleTimeString('ro-RO');
  document.getElementById('clock-date').textContent = now.toLocaleDateString('ro-RO');
}
function startMetrics() {
  setInterval(function () {
    const cpu = Math.floor(8 + Math.random() * 30);
    const ram = Math.floor(40 + Math.random() * 20);
    const gpu = Math.floor(10 + Math.random() * 35);
    document.getElementById('cpu-val').textContent = cpu + '%';
    document.getElementById('cpu-bar').style.width = cpu + '%';
    document.getElementById('ram-val').textContent = ram + '%';
    document.getElementById('ram-bar').style.width = ram + '%';
    document.getElementById('gpu-val').textContent = gpu + '%';
    document.getElementById('gpu-bar').style.width = gpu + '%';
  }, 2500);
}

const reactor = document.getElementById('arc-reactor');
const waveform = document.getElementById('waveform');
const stateLabel = document.getElementById('bonsai-state-label');
function setBonsaiState(state) {
  ['thinking', 'tools'].forEach(function (s) { reactor.classList.remove(s); document.body.classList.remove(s); });
  waveform.classList.remove('active-wave');
  if (state === 'thinking') {
    reactor.classList.add('thinking'); document.body.classList.add('thinking');
    stateLabel.textContent = 'PROCESSING COMMAND...';
  } else if (state === 'tools') {
    reactor.classList.add('tools'); document.body.classList.add('tools');
    waveform.classList.add('active-wave');
    stateLabel.textContent = 'EXECUTING TOOLS...';
  } else {
    stateLabel.textContent = 'BONSAI READY';
  }
}

/* voice input */
const micBtn = document.getElementById('mic-btn');
micBtn.onclick = function () {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    addAsst('Voice recognition is not supported by this browser.', []);
    return;
  }
  const rec = new SR();
  rec.lang = 'ro-RO';
  rec.interimResults = false;
  rec.onstart = function () { micBtn.classList.add('err'); };
  rec.onresult = function (ev) {
    const tx = ev.results[0][0].transcript;
    document.getElementById('user-input').value = tx;
    micBtn.classList.remove('err');
    handleSubmit(ev);
  };
  rec.onerror = function () { micBtn.classList.remove('err'); };
  rec.onend = function () { micBtn.classList.remove('err'); };
  rec.start();
};

document.querySelectorAll('.err, .micerr').forEach(function () {});
updateClock();
setInterval(updateClock, 1000);
startMetrics();
init();
</script>
</body>
</html>"""


def _strip_full(record):
    if isinstance(record, dict):
        out = dict(record)
        out.pop("full_result", None)
        return out
    return record


def sse(handler, event, obj):
    data = json.dumps(obj, ensure_ascii=False)
    handler.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
    handler.wfile.flush()


def _load_chats():
    try:
        with open(CHATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_chats(chats):
    try:
        os.makedirs(os.path.dirname(CHATS_FILE), exist_ok=True)
        with open(CHATS_FILE, "w", encoding="utf-8") as f:
            json.dump(chats[-200:] if isinstance(chats, list) else [],
                      f, ensure_ascii=False, default=str)
        return True
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _touch_activity()
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/workdir":
            self._send(200, json.dumps({"workdir": WORKDIR}))
        elif path == "/api/chats":
            self._send(200, json.dumps({"chats": _load_chats()}, default=str))
        elif path == "/api/todos":
            with _TODOS_LOCK:
                self._send(200, json.dumps({"todos": _TODOS}))
        elif path == "/api/blender":
            self._send(200, json.dumps(_blender_status_payload()))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        _touch_activity()
        try:
            path = self.path.split("?")[0]
            if path not in ("/api", "/api/stream", "/api/workdir",
                            "/api/pick_workdir", "/api/chats",
                            "/api/answer", "/api/effort", "/api/eject"):
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/eject":
                self._send(200, json.dumps(_unload_bonsai()))
                return
            if path == "/api/pick_workdir":
                self._send(200, json.dumps(_confirm_pick_workdir()))
                return
            if path == "/api/chats":
                _save_chats(body.get("chats"))
                self._send(200, json.dumps({"ok": True}))
                return
            if path == "/api/answer":
                ask_id = str(body.get("id") or "")
                answer = str(body.get("answer") or "")
                with _ASKS_LOCK:
                    pending = _ASKS.get(ask_id)
                    if pending:
                        pending["answer"] = answer
                        pending["event"].set()
                _touch_activity()
                self._send(200, json.dumps({"ok": True}))
                return
            if path == "/api/effort":
                effort = set_bonsai_effort(body.get("effort"))
                self._send(200, json.dumps({"ok": True, "effort": effort}))
                return
            if path == "/api/workdir":
                global WORKDIR
                wd = str(body.get("workdir") or "").strip()
                if not wd:
                    self._send(200, json.dumps({"workdir": WORKDIR}))
                    return
                cand = os.path.realpath(wd)
                if not os.path.isdir(cand):
                    os.makedirs(cand, exist_ok=True)
                WORKDIR = cand
                self._send(200, json.dumps({"workdir": WORKDIR}))
                return
            messages = body.get("messages")
            if not messages and body.get("input"):
                messages = [{"role": "user", "content": body["input"]}]
            mode = MODE_PLAN if str(body.get("mode", "")).lower() == MODE_PLAN else MODE_BUILD
            if body.get("effort"):
                set_bonsai_effort(body["effort"])
            if path == "/api/stream":

                def do_ask(q):
                    ask_id = uuid.uuid4().hex[:12]
                    event = threading.Event()
                    entry = {"event": event, "answer": None}
                    with _ASKS_LOCK:
                        _ASKS[ask_id] = entry
                    sse(self, "ask", {"id": ask_id,
                                      "question": q.get("question", ""),
                                      "options": q.get("options") or []})
                    _touch_activity()
                    try:
                        event.wait(BONSAI_KEEP_ALIVE)
                    except Exception:
                        pass
                    with _ASKS_LOCK:
                        answer = (entry.get("answer") or "").strip() or "(no answer)"
                        _ASKS.pop(ask_id, None)
                    return answer

                def do_todo(items):
                    sse(self, "todo", {"todos": items})

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sse(self, "start", {"ok": True, "workdir": WORKDIR,
                                    "model_ready": _bonsai_ready()})
                if not _ensure_bonsai():
                    sse(self, "error", {"text": "Bonsai 2 model server could not start."})
                    return
                handle_chat(messages or [], stream=True, mode=mode,
                            on_reason=lambda t: sse(self, "reason", {"text": t}),
                            on_delta=lambda t: sse(self, "delta", {"text": t}),
                            on_tool=lambda c: sse(self, "tool", {"call": _strip_full(c)}),
                            on_stats=lambda s: sse(self, "stats", s),
                            hooks={"on_ask": do_ask, "on_todo": do_todo})
                sse(self, "done", {"ok": True})
            else:
                if not _ensure_bonsai():
                    self._send(200, json.dumps({"reply": "Bonsai 2 model server could not start.",
                                                "calls": []}))
                    return
                result = handle_chat(messages or [], mode=mode)
                result.pop("_stats", None)
                self._send(200, json.dumps(result, default=str))
        except Exception as exc:
            if self.wfile.closed:
                return
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(str(exc)) + 30))
                self.end_headers()
            except Exception:
                pass
            try:
                sse(self, "error", {"text": str(exc)})
            except Exception:
                pass

    def log_message(self, *args):
        pass


def main():
    try:
        os.makedirs(WORKDIR, exist_ok=True)
    except Exception as exc:
        print(f"WARN: could not create workdir {WORKDIR}: {exc}", flush=True)

    threading.Thread(target=_ensure_bonsai, daemon=True).start()
    threading.Thread(target=_blender_kickoff, daemon=True).start()
    print("BONSAI is READY on http://127.0.0.1:8081", flush=True)
    webbrowser.open(f"http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()