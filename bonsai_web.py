import glob
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", 8081

# ---- model backends ----
BONSAI_DIR = os.environ.get("BONSAI_DIR", r"C:\Users\drago\Desktop\bonsai2")
LLAMA_EXE = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Programs", "prism-llama", "llama-server.exe")
BONSAI_MODEL = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-PQ2_0.gguf")
BONSAI_MMPROJ = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf")
BONSAI_BASE = "http://127.0.0.1:8080"
BONSAI_MODEL_ID = "bonsai2"
MAX_TOOL_ROUNDS = 5
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 200 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 200
MODE_BUILD = "build"
MODE_PLAN = "plan"
WORKDIR = os.path.realpath(os.environ.get("PC_WORKDIR", r"C:\Users\drago\Desktop\workspace"))

CREATE_NO_WINDOW = 0x08000000

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


SYSTEM = ("You are the friendly assistant living on the user's Windows PC. Reply "
          "concisely and naturally, in the same language the user writes in "
          "(Romanian if they write Romanian). If the user attaches one or more "
          "images or text files, look at the images carefully and read the file "
          "contents to answer their question about them. You can also work on "
          "files inside the workspace folder. When you are not sure about "
          "something, are asked for recent/current information, or want to "
          "double-check a fact, you may search the web (web_search) and read "
          "pages (web_fetch) to document yourself before answering.")

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
    print("Bonsai 2 server not running - starting it...", flush=True)
    return _start_bonsai()


def _http_json(url, payload, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


WEB_SPECS = [WEB_TOOLS["web_search"], WEB_TOOLS["web_fetch"]]


def _tools_for(mode):
    if mode == MODE_PLAN:
        return [FILE_TOOLS["list_dir"], FILE_TOOLS["read_file"],
                FILE_TOOLS["search_files"]] + WEB_SPECS
    return [TOOL_SPEC] + list(FILE_TOOLS.values()) + WEB_SPECS


def system_prompt(mode):
    text = SYSTEM
    if mode == MODE_PLAN:
        text += PLAN_MODE_SYSTEM
    return text


def _bonsai_chat(messages, tools):
    payload = {"model": BONSAI_MODEL_ID, "messages": messages,
               "tools": tools, "stream": False}
    data = _http_json(BONSAI_BASE + "/v1/chat/completions", payload)
    return data.get("choices", [{}])[0].get("message", {})


def _bonsai_stream(messages, tools):
    payload = {"model": BONSAI_MODEL_ID, "messages": messages,
               "tools": tools, "stream": True}
    req = urllib.request.Request(BONSAI_BASE + "/v1/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=600) as resp:
        calls = {}
        finish = None
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
            choice = (chunk.get("choices") or [{}])[0]
            delta = choice.get("delta") or {}
            finish = choice.get("finish_reason") or finish
            if delta.get("reasoning_content"):
                yield {"kind": "reason", "text": delta["reasoning_content"]}
            elif delta.get("content"):
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
        yield {"kind": "end", "tool_calls": ordered, "finish": finish}


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


def execute_tool_call(tc):
    fn = tc.get("function") or {}
    name = fn.get("name") or "launch_or_open"
    args = parse_arguments(fn.get("arguments"))
    if name == "launch_or_open":
        raw_result = launch_or_open(args.get("name", ""))
    elif name == "web_search":
        raw_result = _web_search(args.get("query"), 6)
    elif name == "web_fetch":
        raw_result = _web_fetch(args.get("url"))
    else:
        raw_result = _exec_file_tool(name, args)
    result = public_result(_clip_result(raw_result))
    full = _clip_result(raw_result)
    return {"name": name, "arguments": args, "result": result,
            "full_result": full,
            "tool_call_id": tc.get("id") or "call_" + str(len(name))}


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


def run_agent(messages, on_tool=None, mode=MODE_BUILD):
    calls = []
    tools = _tools_for(mode)
    msgs = list(messages)
    for _ in range(MAX_TOOL_ROUNDS):
        message = _bonsai_chat(msgs, tools)
        tool_calls = message.get("tool_calls") or []
        content = message.get("content")
        if not tool_calls:
            return {"reply": (content or "").strip(), "calls": calls}
        msgs.append({"role": "assistant", "content": content,
                     "tool_calls": [{"id": tc.get("id"),
                                     "type": "function",
                                     "function": tc.get("function")}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            record = execute_tool_call(tc)
            calls.append(record)
            if on_tool:
                on_tool(record)
            msgs.append({"role": "tool", "tool_call_id": record["tool_call_id"],
                         "content": json.dumps(record["full_result"],
                                               ensure_ascii=False)})
    return {"reply": "Am terminat ce am putut executa.", "calls": calls}


def stream_agent(messages, on_reason=None, on_delta=None, on_tool=None,
                 mode=MODE_BUILD):
    calls = []
    tools = _tools_for(mode)
    msgs = list(messages)
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
        if not tool_calls:
            return {"calls": calls}
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": tc.get("id"),
                                     "type": "function",
                                     "function": tc.get("function")}
                                    for tc in tool_calls]})
        for tc in tool_calls:
            record = execute_tool_call(tc)
            calls.append(record)
            if on_tool:
                on_tool(record)
            msgs.append({"role": "tool", "tool_call_id": record["tool_call_id"],
                         "content": json.dumps(record["full_result"],
                                               ensure_ascii=False)})
    return {"calls": calls}


def handle_chat(messages, on_reason=None, on_delta=None, on_tool=None,
                stream=False, mode=MODE_BUILD):
    msgs = build_messages(messages, mode=mode)
    if stream:
        return stream_agent(msgs, on_reason=on_reason, on_delta=on_delta,
                            on_tool=on_tool, mode=mode)
    return run_agent(msgs, on_tool=on_tool, mode=mode)


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
  .arc-container.speaking { --ring: rgba(0,240,255,.55); --core1: #00f0ff; --core2: rgba(0,114,255,.8); --glow1: #00f0ff; --glow2: #0072ff; }

  .arc-container.thinking .arc-core { animation: pulseFast .3s infinite alternate; }
  .arc-container.tools .arc-core { animation: pulseGreen .4s infinite alternate; }
  .arc-container.speaking .arc-core { animation: pulseGlow .6s infinite alternate; }
  .arc-container.thinking .arc-ring-outer { animation-duration: 6s; }
  .arc-container.tools .arc-ring-outer { animation-duration: 4s; }
  .arc-container.speaking .arc-ring-outer { animation-duration: 9s; }
  .arc-container.thinking .arc-ring-inner { animation-duration: 5s; }
  .arc-container.tools .arc-ring-inner { animation-duration: 3.5s; }
  @keyframes pulseGlow { 0% { transform: scale(.9); box-shadow: 0 0 25px var(--glow1); } 100% { transform: scale(1.15); box-shadow: 0 0 55px var(--glow1), 0 0 85px var(--glow2); } }
  @keyframes pulseFast { 0% { transform: scale(.95); opacity: .8; } 100% { transform: scale(1.1); opacity: 1; } }
  @keyframes pulseGreen { 0% { transform: scale(.92); box-shadow: 0 0 20px var(--glow1); } 100% { transform: scale(1.18); box-shadow: 0 0 55px var(--glow1), 0 0 85px var(--glow2); } }
  #waveform { display: flex; align-items: center; justify-content: center; gap: 6px; height: 40px; margin: 16px 0; }
  .wave-bar { width: 3px; height: 14px; border-radius: 2px; background-color: #00f0ff; transition: background-color .3s; }
  body.thinking .wave-bar { background-color: #ffd700; }
  body.tools .wave-bar { background-color: #39ff14; }
  body.speaking .wave-bar { background-color: #00f0ff; }
  .active-wave .wave-bar { animation: waveAnim .8s infinite ease-in-out alternate; }
  .wave-bar:nth-child(2) { animation-delay: .1s; } .wave-bar:nth-child(3) { animation-delay: .2s; }
  .wave-bar:nth-child(4) { animation-delay: .3s; } .wave-bar:nth-child(5) { animation-delay: .4s; }
  .wave-bar:nth-child(6) { animation-delay: .5s; } .wave-bar:nth-child(7) { animation-delay: .6s; }
  .wave-bar:nth-child(8) { animation-delay: .7s; } .wave-bar:nth-child(9) { animation-delay: .8s; }
  .wave-bar:nth-child(10) { animation-delay: .9s; }
  @keyframes waveAnim { 0% { height: 6px; } 100% { height: 35px; } }
  #bonsai-state-label { font-size: 13px; letter-spacing: 3px; color: #22d3ee; text-align: center; font-family: Consolas, monospace; text-transform: uppercase; margin: 0; }
  .sub { font-size: 11px; color: #0e7490; margin: 6px 0 0; text-align: center; font-family: Consolas, monospace; }
  #qty { display: flex; flex-direction: column; gap: 8px; margin-top: 18px; width: 100%; }
  .qbtn {
    background: rgba(0,240,255,.05); border: 1px solid rgba(0,240,255,.25); color: #67e8f9;
    border-radius: 8px; padding: 9px 12px; cursor: pointer; font: inherit; font-size: 13px; text-align: left;
    transition: all .2s;
  }
  .qbtn:hover { background: rgba(0,240,255,.2); border-color: #00f0ff; box-shadow: 0 0 12px rgba(0,240,255,.4); }

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

  .sugg { display: flex; gap: 8px; padding: 8px 0; overflow-x: auto; }
  .sg {
    background: rgba(0,240,255,.04); border: 1px solid rgba(0,240,255,.22); color: #67e8f9;
    border-radius: 999px; padding: 6px 14px; white-space: nowrap; cursor: pointer; font-size: 13px; font-family: Consolas, monospace; transition: all .2s;
  }
  .sg:hover { background: rgba(0,240,255,.18); border-color: #00f0ff; }

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

  #voice-sel {
    background: rgba(0,240,255,.05); border: 1px solid rgba(0,240,255,.3); color: #67e8f9;
    border-radius: 8px; padding: 7px 8px; font-size: 11px; font-family: Consolas, monospace; cursor: pointer;
    max-width: 200px; outline: none;
  }
  #voice-sel option { background: #031018; color: #99f6e4; }
  #voice-sel:hover { border-color: #00f0ff; }
  #voice-toggle-btn.onv { background: rgba(0,240,255,.25); border-color: #00f0ff; box-shadow: 0 0 12px rgba(0,240,255,.5); }

  footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 6px 14px; border-radius: 10px; font-size: 13px; color: #0e7490; font-family: Consolas, monospace; letter-spacing: 1px; }
  footer b { color: #22d3ee; }
  .fine { text-align: center; color: #0e7490; font-size: 12px; margin-top: 6px; font-family: Consolas, monospace; }
</style>
</head>
<body>
<div class="app">
  <header class="hud-border">
    <div class="brand">
      <div class="chip-ic">&#9679;</div>
      <div>
        <h1>BONSAI</h1>
        <p>BONSAI 2 &middot; PC ASSISTANT &middot; 100% LOCAL</p>
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
      <button class="hbtn" id="voice-toggle-btn" title="Talk back (voice replies)">&#128483;&#65039;</button>
      <button class="hbtn" id="audio-toggle-btn" title="Sound effects">&#128266;</button>
      <select id="voice-sel" title="TTS voice (Natural = most realistic)">
        <option value="">Voice: Auto</option>
      </select>
      <button class="modebtn" id="modebtn" title="Switch Plan / Build mode">BUILD</button>
      <button class="wbtn" id="workbtn" title="Click to choose the workspace folder">\\WORKSPACE</button>
    </div>
  </header>

  <main class="grid">
    <section class="left panel hud-border">
      <div class="ptitle"><span>CHAT HISTORY</span><span>LOCAL</span></div>
      <button class="hbtn new" id="newchat2">+ NEW CHAT</button>
      <div id="chatlist"></div>
      <div class="fine" style="padding:8px; border-top:1px solid #164e63;">data stays on your PC only</div>
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
      <div id="qty"></div>
      <div class="meterrow" style="width:100%; margin-top:8px;">
        <div class="meter"><div class="lab"><span>CPU</span><b id="cpu-val">0%</b></div><div class="bar"><div class="fill" id="cpu-bar"></div></div></div>
        <div class="meter"><div class="lab"><span>RAM</span><b id="ram-val">0%</b></div><div class="bar"><div class="fill" id="ram-bar"></div></div></div>
        <div class="meter"><div class="lab"><span>GPU</span><b id="gpu-val">0%</b></div><div class="bar"><div class="fill gold" id="gpu-bar"></div></div></div>
      </div>
    </section>

    <section class="right panel hud-border">
      <div class="ptitle"><span>CONVERSATION CONSOLE</span><span><button class="hbtn" style="padding:4px 10px; font-size:11px;" id="clearbtn">CLEAR</button></span></div>
      <div class="chat-wrap">
        <div id="chat-container"></div>
        <div class="sugg" id="sugg"></div>
        <div id="preview"></div>
        <form id="chat-form" class="composer" onsubmit="handleSubmit(event)">
          <div class="field">
            <textarea id="user-input" rows="1" placeholder="Type a command..."></textarea>
            <button type="button" class="iconbtn" id="attach" title="Attach images / files">&#128206;</button>
            <button type="button" class="iconbtn" id="mic-btn" title="Microphone">&#127908;</button>
            <button type="submit" id="send">SEND</button>
          </div>
        </form>
        <div class="fine">Bonsai 2 27B local &middot; opens apps &amp; websites, analyzes images &amp; files, works on your files</div>
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
const EXAMPLES = [
  'Open Steam', 'open Discord', 'open Notepad and YouTube',
  'tell me a joke', 'who are you?', 'open Google', 'what can you do?'
];
const QUICK = [
  ['&#129302; System Status', 'what is the system status and what is the CPU doing?'],
  ['&#128421; Open Notepad', 'open notepad'],
  ['&#127918; Start Steam', 'open steam'],
  ['&#128250; YouTube', 'open youtube'],
  ['&#127925; Spotify', 'open spotify'],
  ['&#128214; Wikipedia', 'open wikipedia']
];

let chats = load();
let cur = null;
let busy = false;
let pendingAtt = [];
let started = false;
let chatMode = localStorage.getItem('jarvis_mode') === 'plan' ? 'plan' : 'build';
let workdir = '';

function load() {
  try { return JSON.parse(localStorage.getItem('jarvis_chats') || '[]'); } catch (e) { return []; }
}
function save() { try { const c = chats.slice(-200); localStorage.setItem('jarvis_chats', JSON.stringify(c)); } catch (e) {} }
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
    d.onclick = function (e) { e.stopPropagation(); chats = chats.filter(function (x) { return x.id !== c.id; }); if (cur && cur.id === c.id) { cur = chats.length ? chats[chats.length - 1] : null; } save(); renderAll(); };
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
      else if (m.role === 'assistant') addAsst(m.content, m.calls || [], m.reason);
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
  const imgs = arr.filter(function (p) { return p.type === 'image_url'; }).map(function (p) { return '- imagine ata&#537;at&#259;'; }).join(' ');
  if (text) { const sp = document.createElement('div'); sp.innerHTML = fmt(text); b.appendChild(sp); }
  else if (imgs && !arr.some(function (p) { return p.type === 'file_att'; })) { const sp = document.createElement('div'); sp.textContent = 'imagine ata&#537;at&#259;'; b.appendChild(sp); }
  row.appendChild(b); row.appendChild(av);
  conv.appendChild(row);
  scrollBottom();
}
function addAsst(text, calls, reason) {
  const row = document.createElement('div'); row.className = 'msgrow bonsai';
  const av = document.createElement('div'); av.className = 'av bonsai'; av.textContent = 'B';
  const b = document.createElement('div'); b.className = 'bubble';
  if (reason) addReasonBox(b, reason);
  (calls || []).forEach(function (c) { toolChip(b, c); });
  if (calls && calls.length) addToolLog(b, calls);
  const inner = document.createElement('div'); inner.className = 'abody'; inner.innerHTML = fmt(text);
  b.appendChild(inner);
  row.appendChild(av); row.appendChild(b);
  convEl().appendChild(row);
  scrollBottom();
  return inner;
}
function addReasonBox(container, text) {
  const d = document.createElement('details'); d.className = 'reasonbox';
  const s = document.createElement('summary'); s.textContent = 'Thinking (BONSAI)';
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
    rs.textContent = typeof c.result === 'string' ? c.result : JSON.stringify(c.result, null, 2);
    it.appendChild(nm); it.appendChild(rs);
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
  chip.title = JSON.stringify(c, null, 2);
  container.appendChild(chip);
}
function suggRow() {
  const el = document.getElementById('sugg');
  el.innerHTML = '';
  if (cur && cur.messages.length) { el.innerHTML = ''; return; }
  EXAMPLES.forEach(function (ex) {
    const c = document.createElement('button'); c.className = 'sg'; c.textContent = ex;
    c.onclick = function () { document.getElementById('user-input').value = ex; handleSubmit(new Event('submit')); };
    el.appendChild(c);
  });
}
function qtyRow() {
  const el = document.getElementById('qty');
  el.innerHTML = '';
  QUICK.forEach(function (q) {
    const b = document.createElement('button'); b.className = 'qbtn';
    b.innerHTML = q[0];
    b.onclick = function () { document.getElementById('user-input').value = q[1]; handleSubmit(new Event('submit')); };
    el.appendChild(b);
  });
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
  go();
}

async function go() {
  if (busy) return;
  if (!cur) newChat();
  const parts = buildUserMsg();
  if (!parts) return;
  busy = true;
  document.getElementById('send').disabled = true;

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
  cur.messages.push({ role: 'user', content: raw });
  addUser(parts);

  addThinking();
  setBonsaiState('thinking');
  pendingAtt = [];
  renderPreview();
  document.getElementById('user-input').value = '';
  try { await streamRun(cur.messages.slice()); }
  catch (err) { doneThinking('Error: ' + err.message); setBonsaiState('idle'); }
  busy = false;
  document.getElementById('send').disabled = false;
  document.getElementById('user-input').focus();
  cur.ts = Date.now();
  save();
  renderList();
  suggRow();
}

let thinkingRow = null;
let liveCalls = [];
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
    const s = document.createElement('summary'); s.textContent = 'Thinking (BONSAI)...';
    thinkingRow.reasonC = document.createElement('div'); thinkingRow.reasonC.className = 'rc';
    thinkingRow.reasonD.appendChild(s); thinkingRow.reasonD.appendChild(thinkingRow.reasonC);
    thinkingRow.b.appendChild(thinkingRow.reasonD);
  }
  thinkingRow.reasonC.textContent += txt;
  thinkingRow.reasonC.scrollTop = thinkingRow.reasonC.scrollHeight;
  scrollBottom();
}
function toolItemDom(call) {
  const it = document.createElement('div');
  it.className = 'tlitem' + (call.result && call.result.error ? ' err' : '');
  const nm = document.createElement('div'); nm.className = 'nm';
  nm.textContent = '\u2699 ' + call.name + ' ' + JSON.stringify(call.arguments || {});
  const rs = document.createElement('div'); rs.className = 'rs';
  rs.textContent = typeof call.result === 'string' ? call.result : JSON.stringify(call.result, null, 2);
  it.appendChild(nm); it.appendChild(rs);
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

async function streamRun(messages) {
  const resp = await fetch('/api/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages: messages, mode: chatMode }) });
  if (!resp.ok || !resp.body) { const j = await resp.json().catch(function () { return {}; }); throw new Error(j.error || 'stream unavailable'); }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '', reply = '', calls = [], reason = '';
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
      if (ev === 'delta') { reply += j.text; onDelta(j.text); setBonsaiState('speaking'); }
      else if (ev === 'reason') { reason += j.text; onReason(j.text); setBonsaiState('thinking'); }
      else if (ev === 'tool') { calls.push(j.call); onTool(j.call); setBonsaiState('tools'); }
      else if (ev === 'error') { doneThinking('Error: ' + j.text); setBonsaiState('idle'); throw new Error(j.text); }
      else if (ev === 'done') { doneThinking(); setBonsaiState('idle'); }
    }
  }
  const last = cur.messages[cur.messages.length - 1];
  if (last && last.role === 'user') {
    cur.messages.push({ role: 'assistant', content: reply, calls: calls, reason: reason.trim() ? reason : undefined });
  } else if (last && last.role === 'assistant' && !last.calls && reply) {
    last.content = reply;
    last.reason = reason.trim() ? reason : undefined;
  }
  if (reply && sfx.enabled) speakBONSAI(reply);
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

document.getElementById('attach').onclick = function () { document.getElementById('filein').click(); };
document.getElementById('filein').onchange = function () {
  const files = Array.from(this.files);
  const slots = 4 - pendingAtt.length;
  files.slice(0, slots).forEach(function (f) {
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
  this.value = '';
};

document.getElementById('newchat2').onclick = function () { newChat(); setBonsaiState('idle'); };
document.getElementById('clearbtn').onclick = function () { if (cur) { cur.messages = []; started = false; } renderAll(); setBonsaiState('idle'); };
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

const inp = document.getElementById('user-input');
inp.addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 160) + 'px'; });
inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); handleSubmit(ev); } });

/* ---------- BONSAI FX: clock, metrics, sound, voice, TTS ---------- */
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
class SoundFX {
  constructor() { this.ctx = null; this.enabled = true; }
  init() {
    if (!this.ctx) {
      const C = window.AudioContext || window.webkitAudioContext;
      this.ctx = new C();
    }
  }
  beep(freq, dur) {
    if (!this.enabled) return;
    try {
      this.init();
      const o = this.ctx.createOscillator(), g = this.ctx.createGain();
      o.type = 'sine'; o.frequency.setValueAtTime(freq, this.ctx.currentTime);
      g.gain.setValueAtTime(0.04, this.ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + dur);
      o.connect(g); g.connect(this.ctx.destination);
      o.start(); o.stop(this.ctx.currentTime + dur);
    } catch (e) {}
  }
  resp() { this.beep(600, .05); setTimeout(() => this.beep(900, .08), 60); }
}
const sfx = new SoundFX();

let talkOn = localStorage.getItem('jarvis_talk') === '1';
const talkBtn = document.getElementById('voice-toggle-btn');
function updateTalkBtn() {
  talkBtn.textContent = talkOn ? '\\ud83d\\udd50' : '\\ud83d\\udd07';
  talkBtn.classList.toggle('onv', talkOn);
  talkBtn.title = talkOn ? 'Talk back ON - I reply out loud' : 'Talk back OFF - click to enable voice replies';
}
updateTalkBtn();
talkBtn.onclick = function () {
  talkOn = !talkOn;
  localStorage.setItem('jarvis_talk', talkOn ? '1' : '0');
  updateTalkBtn();
  if (talkOn && sfx.enabled) sfx.beep(1000, .1);
};

const audioToggleBtn = document.getElementById('audio-toggle-btn');
audioToggleBtn.onclick = function () {
  sfx.enabled = !sfx.enabled;
  this.textContent = sfx.enabled ? '\\ud83d\\udd0a' : '\\ud83d\\udd07';
  if (sfx.enabled) sfx.beep(1000, .1);
};

/* ---- TTS: pick best voice, chunk text, watchdog fallback ---- */
let ttsVoices = [];
let ttsBest = null;
let ttsFallback = null;
function voiceScore(v) {
  const n = (v.name || '');
  const lang = (v.lang || '').toLowerCase();
  let s = 0;
  if (lang.indexOf('ro') === 0) s += 400;
  if (/natural|online|neural/i.test(n)) s += 500;
  else if (/google/i.test(n)) s += 250;
  if (v.localService) s -= 100;
  if (/mobile|tablet/i.test(n)) s -= 50;
  return s;
}
function pickTTSVoices() {
  if (!('speechSynthesis' in window)) return;
  ttsVoices = window.speechSynthesis.getVoices() || [];
  const sorted = ttsVoices.slice().sort(function (a, b) { return voiceScore(b) - voiceScore(a); });
  ttsBest = sorted[0] || null;
  ttsFallback = sorted.find(function (v) { return v.localService && v !== ttsBest; }) || null;
  fillVoiceSel();
}
function fillVoiceSel() {
  const sel = document.getElementById('voice-sel');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = '';
  const def = document.createElement('option');
  def.value = '';
  def.textContent = 'Voice: Auto';
  sel.appendChild(def);
  const picked = localStorage.getItem('jarvis_voice');
  ttsVoices.slice().sort(function (a, b) { return voiceScore(b) - voiceScore(a); }).forEach(function (v) {
    const o = document.createElement('option');
    o.value = v.name;
    o.textContent = ((voiceScore(v) >= 300 && !v.localService) ? '[NATURAL] ' : '') + v.name + ' (' + v.lang + ')';
    sel.appendChild(o);
  });
  if (picked && Array.prototype.some.call(sel.options, function (o) { return o.value === picked; })) {
    sel.value = picked;
  } else if (current && Array.prototype.some.call(sel.options, function (o) { return o.value === current; })) {
    sel.value = current;
  }
}
function detectLang(text) {
  const t = (text || '').toLowerCase();
  let ro = 0, en = 0;
  if (/[ăâîșţ]/u.test(t)) ro += 4;
  const roWords = ['si ', 'și ', 'este ', 'sunt ', 'pentru', 'despre', ' dar ', 'mai ', 'asta ', 'acum ', 'vreau', 'mulțumesc', 'poti', 'poți', 'facut', 'făcut'];
  const enWords = [' the ', ' and ', ' is ', ' are ', ' for ', ' about ', ' you ', ' i ', ' me ', ' we ', ' want', 'please'];
  roWords.forEach(function (w) { if (t.indexOf(w) >= 0) ro += 1; });
  enWords.forEach(function (w) { if (t.indexOf(w) >= 0) en += 1; });
  if (ro > en) return 'ro';
  return 'en';
}
function voiceForLang(lang) {
  const pref = lang === 'ro' ? 'ro' : 'en';
  const ranked = ttsVoices.slice().sort(function (a, b) {
    const sameA = (a.lang || '').toLowerCase().indexOf(pref) === 0 ? 1 : 0;
    const sameB = (b.lang || '').toLowerCase().indexOf(pref) === 0 ? 1 : 0;
    if (sameA !== sameB) return sameB - sameA;
    return voiceScore(b) - voiceScore(a);
  });
  return ranked[0] || null;
}
function chosenVoice(text) {
  const picked = localStorage.getItem('jarvis_voice');
  if (picked) {
    const v = ttsVoices.find(function (x) { return (x.name || '') === picked; });
    if (v) return v;
  }
  return voiceForLang(detectLang(text)) || ttsBest;
}
function initVoices() {
  pickTTSVoices();
  const langs = ttsVoices.map(function (v) { return (v.lang || '') + ' / ' + (v.name || ''); });
  console.log('[TTS] voices:', JSON.stringify(langs));
}
if ('speechSynthesis' in window) {
  window.speechSynthesis.onvoiceschanged = initVoices;
  initVoices();
  setTimeout(pickTTSVoices, 300);
  setTimeout(pickTTSVoices, 1200);
}
(function () {
  const sel = document.getElementById('voice-sel');
  if (!sel) return;
  sel.addEventListener('change', function () {
    if (sel.value) localStorage.setItem('jarvis_voice', sel.value);
    else localStorage.removeItem('jarvis_voice');
    sfx.beep(720, .08);
    const v = chosenVoice('Astept comenzi, Dominic. Sunt BONSAI. Awaiting command, Dominic. I am BONSAI.');
    if (!v) return;
    const u = new SpeechSynthesisUtterance('Astept comenzi, Dominic. Sunt BONSAI. Awaiting command, Dominic. I am BONSAI.');
    u.voice = v; u.lang = v.lang || 'ro-RO'; u.rate = 1.0; u.pitch = 1.0;
    try { window.speechSynthesis.speak(u); } catch (e) {}
  });
})();
function splitTTS(text, max) {
  const parts = [];
  const sentences = text.replace(/\\s*\\n+\\s*/g, ' ').split(/(?<=[.!?…])\\s+/);
  let cur = '';
  sentences.forEach(function (s) {
    const piece = (cur ? cur + ' ' : '') + s;
    if (piece.length > max && cur) { parts.push(cur); cur = s; }
    else cur = piece;
  });
  if (cur) parts.push(cur);
  return parts.length ? parts : [''];
}
function speakBONSAI(text) {
  if (!talkOn || !('speechSynthesis' in window)) return;
  if (!ttsVoices.length) pickTTSVoices();
  const clean = String(text).replace(/```[\\s\\S]*?```/g, ' ').replace(/`([^`]+)`/g, '$1')
                .replace(/\\*\\*([^*]+)\\*\\*/g, '$1').replace(/\\*([^*]+)\\*/g, '$1')
                .replace(/[#_~]/g, '').replace(/\\s+/g, ' ').trim();
  if (!clean) return;
  const parts = splitTTS(clean, 220);
  const useVoice = chosenVoice(clean) || ttsFallback || null;
  const detected = detectLang(clean);
  const natural = useVoice && !useVoice.localService;
  let useLang = (useVoice && useVoice.lang) || (detected === 'ro' ? 'ro-RO' : 'en-US');
  let started = false;
  let watchdog = null;
  let ti = 0;
  function speakNext() {
    if (ti >= parts.length) { clearTimeout(watchdog); return; }
    const u = new SpeechSynthesisUtterance(parts[ti++]);
    if (useVoice) u.voice = useVoice;
    u.lang = useLang;
    if (natural) { u.rate = 1.02; u.pitch = 1.0; }
    else { u.rate = 0.98; u.pitch = 1.1; }
    u.onstart = function () { started = true; clearTimeout(watchdog); };
    u.onend = function () { speakNext(); };
    u.onerror = function () { speakNext(); };
    try { window.speechSynthesis.speak(u); } catch (e) { speakNext(); }
  }
  watchdog = setTimeout(function () {
    if (!started && window.speechSynthesis) {
      try { window.speechSynthesis.cancel(); } catch (e) {}
      if (ttsFallback) {
        const u2 = new SpeechSynthesisUtterance(clean);
        u2.voice = ttsFallback; u2.lang = ttsFallback.lang || 'ro-RO'; u2.rate = 1.0; u2.pitch = 1.1;
        try { window.speechSynthesis.speak(u2); } catch (e) {}
      }
    }
  }, 1400);
  speakNext();
}

const reactor = document.getElementById('arc-reactor');
const waveform = document.getElementById('waveform');
const stateLabel = document.getElementById('bonsai-state-label');
function setBonsaiState(state) {
  ['speaking', 'thinking', 'tools'].forEach(function (s) { reactor.classList.remove(s); document.body.classList.remove(s); });
  waveform.classList.remove('active-wave');
  if (state === 'speaking') {
    reactor.classList.add('speaking'); document.body.classList.add('speaking');
    waveform.classList.add('active-wave');
    stateLabel.textContent = 'BONSAI SPEAKING...';
  } else if (state === 'thinking') {
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
  rec.onstart = function () { micBtn.classList.add('err'); sfx.beep(900, .1); };
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
qtyRow();
suggRow();
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


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/workdir":
            self._send(200, json.dumps({"workdir": WORKDIR}))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        try:
            path = self.path.split("?")[0]
            if path not in ("/api", "/api/stream", "/api/workdir", "/api/pick_workdir"):
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/pick_workdir":
                self._send(200, json.dumps(_confirm_pick_workdir()))
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
            if path == "/api/stream":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                sse(self, "start", {"ok": True, "workdir": WORKDIR})
                handle_chat(messages or [], stream=True, mode=mode,
                            on_reason=lambda t: sse(self, "reason", {"text": t}),
                            on_delta=lambda t: sse(self, "delta", {"text": t}),
                            on_tool=lambda c: sse(self, "tool", {"call": _strip_full(c)}))
                sse(self, "done", {"ok": True})
            else:
                result = handle_chat(messages or [], mode=mode)
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
    if not _ensure_bonsai():
        print("ERROR: Bonsai 2 server could not start. Check bonsai folder / llama-server.", flush=True)
    print("PC Asistent is READY on http://127.0.0.1:8081", flush=True)
    webbrowser.open(f"http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()