import asyncio
import base64
import ctypes
import datetime
import glob
import html
import io
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
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

try:
    import win32gui
    import win32process
    import psutil
    _WIN_UI = True
except Exception:
    _WIN_UI = False

HOST, PORT = "127.0.0.1", 8081

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

# ---- model backends ----
BONSAI_DIR = os.environ.get("BONSAI_DIR", os.path.dirname(os.path.abspath(__file__)))

def _default_llama_exe():
    if IS_WINDOWS:
        return os.path.join(os.environ.get("LOCALAPPDATA", ""),
                            "Programs", "prism-llama", "llama-server.exe")
    return shutil.which("llama-server") or os.path.join(BONSAI_DIR, "llama-server")

LLAMA_EXE = os.environ.get("PC_LLAMA_SERVER") or _default_llama_exe()
BONSAI_MODEL = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-PQ2_0.gguf")
BONSAI_MMPROJ = os.path.join(BONSAI_DIR, "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf")
BONSAI_BASE = "http://127.0.0.1:8080"
BONSAI_MODEL_ID = "bonsai2"
BONSAI_CTX = 32768
BONSAI_KEEP_ALIVE = 120
if IS_WINDOWS:
    CHATS_FILE = os.path.join(os.path.expandvars(r"%APPDATA%"),
                              "BonsaiAsistent", "chats.json")
else:
    _cfg_dir = os.environ.get("XDG_CONFIG_HOME") or \
        os.path.expanduser("~/.config")
    CHATS_FILE = os.path.join(_cfg_dir, "BonsaiAsistent", "chats.json")
# Tool calls are effectively unlimited - the model calls tools for as long as it
# needs and the conversation context is the natural stop. The guard counter only
# exists to catch a pathological infinite loop; tune it with BONSAI_MAX_TOOL_ROUNDS
# (e.g. 1000, or 0 for no guard at all).
MAX_TOOL_ROUNDS = int(os.environ.get("BONSAI_MAX_TOOL_ROUNDS", "1000")) or 10 ** 9
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BYTES = 40 * 1024 * 1024
MAX_TEXT_FILE_BYTES = 200 * 1024
MAX_FILE_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 200
MODE_BUILD = "build"
MODE_PLAN = "plan"
_DEF_WORKDIR = (r"C:\Users\drago\Desktop\workspace" if IS_WINDOWS
                else os.path.expanduser("~/bonsai_workspace"))
WORKDIR = os.path.realpath(os.environ.get("PC_WORKDIR", _DEF_WORKDIR))
PIPER_DIR = os.environ.get("PC_PIPER_DIR", os.path.join(BONSAI_DIR, "piper"))
MODELS_FILE = os.path.join(BONSAI_DIR, "models.json")
MODELS_DIR = os.path.join(BONSAI_DIR, "models")

CREATE_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

try:
    os.makedirs(WORKDIR, exist_ok=True)
except Exception:
    pass

_BONSAI_LOCK = threading.Lock()
_BONSAI_PROC = None
_LOADING_MODEL = False
_MODELS = []
_ACTIVE_MODEL = None
_MODELS_LOCK = threading.RLock()
_ASKS = {}
_ASKS_LOCK = threading.Lock()
_TODOS = []
_TODOS_LOCK = threading.Lock()
_LAST_ACTIVITY = time.time()
_ACTIVITY_LOCK = threading.Lock()
_BONSAI_EFFORT = "xhigh"
_EFFORT_LOCK = threading.Lock()

_SCHED = {}
_SCHED_LOCK = threading.Lock()
_SCHED_THREAD_ON = False
_SCHED_LOADED = False
_SCHED_EVENTS = []
_SCHED_DONE_MAX = 25

# ---------------------------------------------------------------------------
# Path scope: how far outside the workspace the file tools may reach.
#   workspace - hard sandbox (old behaviour): anything else is refused
#   ask       - the UI pops ALLOW FOR THIS CONV / ALLOW ONCE / DENY
#   system    - trusted: no prompts, the whole PC is fair game
# ---------------------------------------------------------------------------
_PATH_POLICY = str(os.environ.get("PC_PATH_POLICY") or "ask").strip().lower()
if _PATH_POLICY not in ("workspace", "ask", "system"):
    _PATH_POLICY = "ask"
_PATH_LOCK = threading.RLock()
_PATH_GRANTS = {}
_PATH_DENIES = {}
_PATH_TLS = threading.local()
PATH_ASK_ONCE = "ALLOW ONCE"
PATH_ASK_CONV = "ALLOW FOR THIS CONV"
PATH_ASK_DENY = "DENY"

_TTS_ENABLED = False
_TTS_LOCK = threading.Lock()


def set_tts_enabled(enabled):
    global _TTS_ENABLED
    with _TTS_LOCK:
        _TTS_ENABLED = bool(enabled)
        return _TTS_ENABLED


def tts_enabled():
    with _TTS_LOCK:
        return _TTS_ENABLED

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


def _open_path(path):
    """Open a file or URI with the OS default handler (os.startfile on
    Windows, xdg-open / gio on Linux, browser as last resort)."""
    try:
        if IS_WINDOWS:
            os.startfile(path)
            return True
    except Exception:
        pass
    for opener in ("xdg-open", "gio", "kde-open"):
        exe = shutil.which(opener)
        if exe:
            try:
                subprocess.Popen([exe, str(path)],
                                 stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=CREATE_NO_WINDOW)
                return True
            except Exception:
                continue
    try:
        webbrowser.open(str(path))
        return True
    except Exception:
        return False


def _store_appid(name):
    if not IS_WINDOWS:
        return None
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
    error = None
    for kind, value in _APPS.get(key, []):
        try:
            if kind == "uri":
                _open_path(value)
            elif kind == "cmd":
                if IS_WINDOWS:
                    subprocess.Popen(["cmd", "/c", "start", "", value],
                                     creationflags=CREATE_NO_WINDOW)
                else:
                    exe = shutil.which(value)
                    if not exe:
                        continue
                    subprocess.Popen([exe], creationflags=CREATE_NO_WINDOW)
            elif kind == "path":
                path = os.path.expandvars(value)
                if not os.path.exists(path):
                    continue
                _open_path(path)
            elif kind == "glob":
                hits = glob.glob(os.path.expandvars(value))
                if not hits:
                    continue
                _open_path(hits[0])
        except Exception as exc:
            error = f"{kind}:{value} -> {exc}"
            continue
        return {"launched": key, "result": "ok", "method": kind}
    if not _APPS.get(key):
        store = _store_appid(key)
        if store:
            try:
                _open_path("shell:AppsFolder\\" + store["AppID"])
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


_LOCAL_FILE_EXT = (".html", ".htm", ".png", ".jpg", ".jpeg", ".gif", ".bmp",
                   ".svg", ".webp", ".pdf", ".txt", ".md", ".csv", ".json",
                   ".xml", ".py", ".js", ".css", ".mp3", ".mp4", ".wav",
                   ".zip", ".rar", ".docx", ".xlsx", ".pptx")


def _local_file_target(name):
    raw = str(name or "").strip().strip('"')
    if not raw:
        return None
    low = raw.lower()
    if low.startswith(("http://", "https://", "www.")):
        return None
    ext = os.path.splitext(raw)[1].lower()
    is_path_like = ("/" in raw or "\\" in raw or os.path.splitdrive(raw)[0] != "")
    if ext not in _LOCAL_FILE_EXT and not is_path_like:
        return None
    base = os.path.realpath(WORKDIR)
    cand = os.path.realpath(os.path.join(base, raw))
    if cand == base or cand.startswith(base + os.sep):
        return cand
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
            _open_path("shell:AppsFolder\\" + store["AppID"])
        except Exception as exc:
            return {"error": f"could not open '{name}': {exc}"}
        return {"launched": store.get("Name", key), "result": "ok", "method": "start menu app"}
    if not IS_WINDOWS:
        exe = shutil.which(key)
        if exe:
            try:
                subprocess.Popen([exe], creationflags=CREATE_NO_WINDOW)
            except Exception as exc:
                return {"error": f"could not open '{name}': {exc}"}
            return {"launched": key, "result": "ok", "method": "linux-bin"}
    local = _local_file_target(name)
    if local:
        if not os.path.isfile(local):
            return {"error": f"file not found: {local}"}
        try:
            _open_path(local)
        except Exception as exc:
            return {"error": f"could not open '{name}': {exc}"}
        return {"opened": local, "result": "ok", "method": "file"}
    if key.startswith(("http://", "https://", "www.")):
        site = _normalize_url(key)
        webbrowser.open(site)
        return {"opened": site, "result": "ok", "method": "website"}
    if "/" in key or "\\" in key or os.path.splitdrive(key)[0]:
        return {"error": f"could not open '{name}': not a known program, website "
                         f"or workspace file (paths outside the workspace are "
                         f"rejected)"}
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
        "description": "Open a program, a website or a FILE on this PC by name. "
                       "Examples: steam, discord, notepad, spotify, youtube, "
                       "github, google, wikipedia. You may name several, e.g. "
                       "'steam and youtube'. Files: give a workspace file like "
                       "'ad.html', 'out/report.pdf' or 'result.png' (absolute "
                       "paths also work) - it opens with the default program "
                       "(HTML/PDF/PNG open in the browser). The tool decides "
                       "whether each name is an app, a workspace file or a "
                       "website.",
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

def _path_state():
    with _PATH_LOCK:
        return {"policy": _PATH_POLICY,
                "workspace": os.path.realpath(WORKDIR),
                "grants": [{"path": p, "at": g.get("at")} for p, g in _PATH_GRANTS.items()],
                "denies": [{"path": p, "at": d.get("at")} for p, d in _PATH_DENIES.items()]}


def _path_policy_set(value):
    global _PATH_POLICY
    value = str(value or "").strip().lower()
    if value not in ("workspace", "ask", "system"):
        return _path_state()
    with _PATH_LOCK:
        _PATH_POLICY = value
    return _path_state()


def _path_grant(path, scope="conv"):
    with _PATH_LOCK:
        _PATH_DENIES.pop(path, None)
        _PATH_GRANTS[path] = {"scope": scope,
                              "at": datetime.datetime.now().isoformat(timespec="seconds")}
    if scope == "once":
        once = getattr(_PATH_TLS, "once", None)
        if once is None:
            once = set()
            _PATH_TLS.once = once
        once.add(path)


def _path_deny(path):
    with _PATH_LOCK:
        _PATH_GRANTS.pop(path, None)
        _PATH_DENIES[path] = {"at": datetime.datetime.now().isoformat(timespec="seconds")}


def _path_forget(path=None):
    """Drop every conversation grant/deny, or just one root."""
    with _PATH_LOCK:
        if path:
            _PATH_GRANTS.pop(os.path.realpath(str(path)), None)
            _PATH_DENIES.pop(os.path.realpath(str(path)), None)
        else:
            _PATH_GRANTS.clear()
            _PATH_DENIES.clear()
    return _path_state()


def _path_covered(path, table):
    """True when path sits inside one of the roots in table (longest match)."""
    best = None
    for root in table:
        if path == root or path.startswith(root + os.sep):
            if best is None or len(root) > len(best):
                best = root
    return best


def _path_approval(cand, what="access this path", tool=None):
    """Gate a path outside the workspace. Returns (allowed, message)."""
    cand = os.path.realpath(cand)
    base = os.path.realpath(WORKDIR)
    if cand == base or cand.startswith(base + os.sep):
        return True, ""
    with _PATH_LOCK:
        policy = _PATH_POLICY
        if policy == "system":
            return True, ""
        if policy == "workspace":
            return False, ("access denied: path outside workspace '%s'. The path "
                           "scope is set to WORKSPACE - switch it to ASK or SYSTEM "
                           "in the sidebar if you want Bonsai to reach other places."
                           % WORKDIR)
        granted = _path_covered(cand, _PATH_GRANTS)
        if granted and (_PATH_GRANTS[granted].get("scope") != "once"
                        or granted in (getattr(_PATH_TLS, "once", None) or ())):
            return True, ""
        denied = _path_covered(cand, _PATH_DENIES)
    if denied:
        return False, ("access denied: '%s' is inside '%s', which you denied earlier "
                       "in this conversation. Ask again if you changed your mind."
                       % (cand, denied))
    on_ask = getattr(_PATH_TLS, "on_ask", None)
    if not on_ask:
        return False, ("access denied: path outside workspace '%s' and no one is "
                       "around to approve it (this tool only asks for permission "
                       "while a reply is streaming in the UI)." % WORKDIR)
    answer = str(on_ask({
        "kind": "path_approval",
        "question": "Bonsai wants to %s outside the workspace:\n\n%s" % (what, cand),
        "path": cand,
        "tool": tool or getattr(_PATH_TLS, "tool", "") or "a file tool",
        "options": [PATH_ASK_CONV, PATH_ASK_ONCE, PATH_ASK_DENY],
    }) or "").strip().upper()
    if answer == PATH_ASK_CONV:
        _path_grant(cand, "conv")
        return True, ""
    if answer == PATH_ASK_ONCE:
        _path_grant(cand, "once")
        return True, ""
    _path_deny(cand)
    return False, ("access denied: the user chose DENY for '%s'. Do not try this path "
                   "again in this conversation - ask the user what to do instead."
                   % cand)


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
    ok, message = _path_approval(cand, "open a file or folder at")
    if ok:
        return cand
    raise PermissionError(message)


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
    rel = os.path.relpath(target, os.path.realpath(WORKDIR))
    result = {"result": "ok", "path": target, "bytes": os.path.getsize(target)}
    preview = _preview_url(rel)
    if preview:
        result["preview_url"] = preview
    return result


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


def _preview_url(rel):
    rel = str(rel or "").strip().replace("\\", "/").lstrip("/")
    if not rel.lower().endswith((".html", ".htm")):
        return None
    return "http://127.0.0.1:%d/preview?path=%s" % (PORT, urllib.parse.quote(rel))


def _preview_html(path):
    rel = str(path or "").strip()
    if not rel.lower().endswith((".html", ".htm")):
        return {"error": "preview_html only works with .html / .htm files"}
    try:
        target = _safe_path(rel)
    except ValueError as exc:
        return {"error": str(exc)}
    if not os.path.isfile(target):
        return {"error": f"file not found: {target}"}
    relpath = os.path.relpath(target, os.path.realpath(WORKDIR))
    return {"result": "ok", "path": target,
            "preview_url": _preview_url(relpath)}


_PREVIEW_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".ico": "image/x-icon", ".bmp": "image/bmp",
    ".woff": "font/woff", ".woff2": "font/woff2",
    ".ttf": "font/ttf", ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".xml": "text/xml; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm",
}


def _preview_base_href(target):
    """A <base> so relative assets inside a previewed page resolve to the
    /previewfile route instead of the server root."""
    rel = os.path.relpath(target, os.path.realpath(WORKDIR)).replace("\\", "/")
    folder = os.path.dirname(rel)
    quoted = "/".join(urllib.parse.quote(seg) for seg in folder.split("/") if seg)
    return "/previewfile/" + (quoted + "/" if quoted else "")


def _inject_base(html_text, href):
    """Insert <base href=...> into <head> unless the page already has one."""
    low = html_text.lower()
    if "<base" in low[:4000]:
        return html_text
    tag = '<base href="%s">' % href
    i = low.find("<head")
    if i != -1:
        j = low.find(">", i)
        if j != -1:
            return html_text[:j + 1] + tag + html_text[j + 1:]
    i = low.find("<html")
    if i != -1:
        j = low.find(">", i)
        if j != -1:
            return html_text[:j + 1] + "<head>" + tag + "</head>" + html_text[j + 1:]
    return tag + html_text


_SEARCH_SKIP_DIRS = {"windows", "program files", "program files (x86)", "programdata",
                     "$recycle.bin", "system volume information", "node_modules",
                     ".git", ".svn", "__pycache__", ".venv", "venv", "site-packages",
                     "appdata\\local\\temp", "appdata\\local\\microsoft\\windows\\inets"}
_SEARCH_FILE_BUDGET = 40000
_SEARCH_SECONDS = 45.0


def _search_files(pattern, path):
    if not str(pattern or "").strip():
        return {"error": "pattern is required (a regular expression to search for)"}
    target = _safe_path(path) if path else os.path.realpath(WORKDIR)
    if not os.path.isdir(target):
        return {"error": f"not a directory: {target}"}
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.UNICODE)
    except re.error as exc:
        return {"error": f"invalid regular expression: {exc}"}
    base = os.path.realpath(WORKDIR)
    outside = not (target == base or target.startswith(base + os.sep))
    started = time.time()
    hits = []
    scanned = 0
    truncated = False
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and d != "__pycache__"
                   and os.path.join(d.lower(), "").rstrip("\\/") not in _SEARCH_SKIP_DIRS
                   and not d.lower() in _SEARCH_SKIP_DIRS]
        for name in sorted(files):
            if name.startswith("."):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            scanned += 1
            if scanned > _SEARCH_FILE_BUDGET or time.time() - started > _SEARCH_SECONDS:
                truncated = True
                return {"result": "ok", "matches": hits, "truncated": True,
                        "path": target, "scanned_files": scanned,
                        "note": "stopped early after %d files / %.0fs - narrow the "
                                "path to get the rest" % (scanned, time.time() - started)}
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if rx.search(line):
                            shown = full if outside else os.path.relpath(full, base).replace("\\", "/")
                            hits.append({"file": shown, "line": lineno,
                                         "text": line.rstrip()[:200]})
                            if len(hits) >= MAX_SEARCH_RESULTS:
                                return {"result": "ok", "matches": hits,
                                        "truncated": True, "path": target,
                                        "scanned_files": scanned}
            except Exception:
                continue
    return {"result": "ok", "matches": hits, "truncated": truncated,
            "path": target, "scanned_files": scanned}


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


def _trim_results(results, limit=5, snippet=240):
    out = []
    seen = set()
    for r in (results or []):
        if len(out) >= limit:
            break
        url = (r.get("url") or "").strip()
        key = url.rstrip("/").lower() or (r.get("title") or "").lower()
        if key and key in seen:
            continue
        seen.add(key)
        snip = (r.get("snippet") or "").strip()
        if len(snip) > snippet:
            snip = snip[:snippet].rstrip() + "..."
        item = {"title": (r.get("title") or "").strip()[:180],
                "url": url,
                "snippet": snip,
                "source": r.get("source") or ""}
        try:
            if url:
                item["domain"] = urllib.parse.urlparse(url).netloc.lower()
        except Exception:
            pass
        out.append(item)
    return out


def _fetch_text(url, timeout=15):
    """Fetch a URL and return its body as text (for JSON APIs etc.)."""
    req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _coerce_suggestions(data):
    """Flatten an autocomplete payload into a list of strings.

    DuckDuckGo answers with a JSON list whose single element is itself a
    JSON-encoded list, e.g. ["[\"search console\",\"search\"]"]."""
    out = []
    if not isinstance(data, list):
        return out
    for item in data:
        if isinstance(item, list):
            out.extend(str(x) for x in item)
        elif isinstance(item, str) and item.strip().startswith("["):
            try:
                inner = json.loads(item)
            except Exception:
                inner = None
            if isinstance(inner, list):
                out.extend(str(x) for x in inner)
            else:
                out.append(item)
        elif item is not None:
            out.append(str(item))
    return out


def _search_suggestions(query, limit=6):
    """Autocomplete / 'did you mean' candidates for a query."""
    q = str(query or "").strip()
    if not q:
        return []
    out = []
    try:
        raw = _fetch_text("https://duckduckgo.com/ac/?q=" +
                          urllib.parse.quote(q) + "&type=list") or "[]"
        out = _coerce_suggestions(json.loads(raw))
    except Exception:
        out = []
    if not out:
        try:
            raw = _fetch_text("https://api.bing.com/osjson.aspx?query=" +
                              urllib.parse.quote(q)) or "[]"
            data = json.loads(raw)
            if isinstance(data, list) and len(data) > 1:
                out = _coerce_suggestions(data[1:2])
        except Exception:
            pass
    seen, uniq = {q.lower()}, []
    for s in out:
        s = s.strip()
        k = s.lower()
        if s and k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq[:limit]


def _did_you_mean(query, suggestions):
    """The most likely correction when the query looks like a typo."""
    q = str(query or "").strip()
    if not q or not suggestions:
        return None
    ql = q.lower()
    for s in suggestions:
        if s.lower() == ql:
            return None
    best, best_score = None, 0.0
    for s in suggestions:
        score = _similarity(ql, s.lower())
        if score > best_score:
            best, best_score = s, score
    if best and best_score >= 0.72:
        return best
    return None


def _similarity(a, b):
    """Rough 0..1 similarity (difflib ratio, stdlib only)."""
    try:
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio()
    except Exception:
        return 0.0


def _web_search(query, max_results=6, suggest_only=False):
    q = str(query or "").strip()
    if not q:
        return {"error": "query is required"}
    suggestions = _search_suggestions(q)
    correction = _did_you_mean(q, suggestions)
    base = {"result": "ok", "query": q,
            "did_you_mean": correction,
            "related_searches": [s for s in suggestions if s.lower() != q.lower()]}
    if suggest_only:
        base["suggestions"] = suggestions
        return base
    results = _parse_wikipedia_search(q, max_results)
    if results:
        out = dict(base)
        out.update({"results": _trim_results(results), "source": "wikipedia"})
        return out
    urls = [
        "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(q),
        "https://www.bing.com/search?q=" + urllib.parse.quote(q) + "&count=10&setlang=en",
    ]
    for target in urls:
        try:
            markup = _fetch_html(target)
        except Exception:
            continue
        if "duckduckgo.com" in target:
            results = []
            anchors = re.findall(r'(?is)<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>', markup)
            snippets = re.findall(r'(?is)<a[^>]*class="result__snippet"[^>]*>(.*?)</a>', markup)
            for i, (href, atext) in enumerate(anchors[:max_results]):
                title = html.unescape(re.sub(r"(?is)<[^>]+>", "", atext)).strip()
                mm = re.search(r"uddg=([^&]+)", href)
                real = urllib.parse.unquote(mm.group(1)) if mm else href
                snip = ""
                if i < len(snippets):
                    snip = html.unescape(re.sub(r"(?is)<[^>]+>", "", snippets[i])).strip()
                if real.startswith("http") and title:
                    results.append({"title": title, "url": real, "snippet": snip,
                                    "source": "duckduckgo"})
        else:
            results = _parse_bing(markup, max_results)
            for r in results:
                r.setdefault("source", "bing")
        if results:
            out = dict(base)
            out.update({"results": _trim_results(results, limit=max_results),
                        "source": results[0].get("source", "web")})
            if not out["results"] and correction:
                out["hint"] = ("no results for the query as typed; a likely "
                                "correction is %r - retry with it" % correction)
            return out
    out = dict(base)
    out.update({"results": [], "source": "none",
                "hint": ("no results found%s" %
                         (("; try %r instead" % correction) if correction else ""))})
    return out


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
                           "about. Returns STRUCTURED results - title, url, domain, "
                           "snippet, source - plus 'did_you_mean' when your query "
                           "looks like a typo and 'related_searches' for "
                           "autocomplete, so you can recover from a misspelling in a "
                           "single call. Use action='suggest' for the cheap "
                           "autocomplete check only (no page scraping), then retry "
                           "with the corrected query. Read details with web_fetch on "
                           "the most relevant link.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query, e.g. 'latest node.js LTS version'."
                    },
                    "action": {
                        "type": "string",
                        "enum": ["search", "suggest"],
                        "description": "'search' (default) returns full results; 'suggest' returns only autocomplete/'did you mean' candidates - use it to fix a typo cheaply."
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
                                       "e.g. 'src', '' (default = workspace root). "
                                       "An absolute path anywhere else on this PC "
                                       "also works - the user is asked to approve it."
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
            "description": "Read a text file (relative path inside the "
                           "workspace, or an absolute path anywhere on this PC "
                           "- the user approves out-of-workspace reads). "
                           "Returns its full content.",
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
            "description": "Regex search across files and returns file + line "
                           "matches. The folder defaults to the whole workspace, "
                           "but you can point it at any directory on this PC "
                           "('C:\\\\Users', 'C:\\\\', '/home/me') to search the "
                           "entire machine - the user is asked to approve paths "
                           "outside the workspace. Heavy system folders "
                           "(Windows, Program Files, node_modules) are skipped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression, e.g. 'def main'."
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional folder to search inside: relative "
                                       "to the workspace (default = whole "
                                       "workspace) or an absolute path anywhere on "
                                       "this PC (the user is asked to approve it)."
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
_CODE_TIMEOUT_MAX = 600
_CODE_CAP = 8000
_CODE_RUNNERS = {
    "python": {"ext": ".py", "exe": sys.executable, "args": ["{script}"]},
    "py": {"ext": ".py", "exe": sys.executable, "args": ["{script}"]},
    "node": {"ext": ".js", "exe": "node", "args": ["{script}"]},
    "js": {"ext": ".js", "exe": "node", "args": ["{script}"]},
    "javascript": {"ext": ".js", "exe": "node", "args": ["{script}"]},
    "go": {"ext": ".go", "exe": "go", "args": ["run", "{script}"]},
    "golang": {"ext": ".go", "exe": "go", "args": ["run", "{script}"]},
    "lua": {"ext": ".lua", "exe": "lua", "args": ["{script}"]},
    "php": {"ext": ".php", "exe": "php", "args": ["{script}"]},
    "ruby": {"ext": ".rb", "exe": "ruby", "args": ["{script}"]},
    "perl": {"ext": ".pl", "exe": "perl", "args": ["{script}"]},
    "bash": {"ext": ".sh", "exe": "bash", "args": ["{script}"]},
    "sh": {"ext": ".sh", "exe": "bash", "args": ["{script}"]},
    "shell": {"ext": ".sh", "exe": "bash", "args": ["{script}"]},
}


def _resolve_shell():
    for name in ("bash", "sh"):
        p = shutil.which(name)
        if p and "system32" not in p.lower():
            return p
    return None


def _code_runner(lang):
    key = str(lang or "python").strip().lower()
    spec = _CODE_RUNNERS.get(key)
    if not spec:
        return None, None, None
    exe = spec["exe"]
    if exe == sys.executable:
        resolved = exe
    elif exe == "bash":
        resolved = _resolve_shell()
    else:
        resolved = shutil.which(exe)
    if not resolved:
        return None, None, None
    return spec["args"], spec["ext"], resolved


def _available_languages():
    langs = []
    for lang, spec in _CODE_RUNNERS.items():
        if spec["exe"] == sys.executable:
            langs.append(lang)
        elif spec["exe"] == "bash":
            if _resolve_shell():
                langs.append(lang)
        elif shutil.which(spec["exe"]):
            langs.append(lang)
    return sorted(set(langs))


def _run_code(language, code, timeout=_CODE_TIMEOUT):
    args_tpl, ext, exe = _code_runner(language)
    if not args_tpl:
        avail = ", ".join(_available_languages()) or "none"
        return {"error": f"code runner for '{language}' is not installed on this "
                         f"PC (available: {avail})"}
    t = int(timeout or _CODE_TIMEOUT)
    t = min(max(t, 1), _CODE_TIMEOUT_MAX)
    snippet = str(code or "").strip()
    if not snippet:
        return {"error": "code is required"}
    sandbox = tempfile.mkdtemp(prefix="bonsai_sandbox_")
    script = os.path.join(sandbox, "snippet" + ext)
    try:
        with open(script, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(snippet)
        argv = [exe] + [a.replace("{script}", script) for a in args_tpl]
        proc = subprocess.run(argv, capture_output=True,
                              text=True, timeout=t, cwd=sandbox,
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
        return {"error": f"code timed out after {t}s (max {_CODE_TIMEOUT_MAX}s)"}
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


SYSTEM = ("You are the friendly assistant living on the user's "
          + ("Windows" if IS_WINDOWS else "Linux") + " PC. Reply "
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
          "files inside the workspace folder and on archives (archive). The "
          "workspace is your default folder, not a wall: when the user asks "
          "about a file, folder or search somewhere else on the PC, pass that "
          "absolute path to list_dir / read_file / search_files / write_file / "
          "edit_file and the user will be asked to approve it (ALLOW FOR THIS "
          "CONV, ALLOW ONCE or DENY). So do not refuse or guess - just try the "
          "real path, and if the access is denied, respect it and ask the user "
          "what to do instead. Prefer search_files with a path such as "
          "'C:\\\\Users' or 'C:\\\\' to look across the whole PC instead of "
          "guessing where a file might be. When "
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
    global _BONSAI_PROC, _LOADING_MODEL
    entry = _active_model()
    if not entry or entry.get("type") == "api":
        return _bonsai_ready()
    model = entry.get("path") or ""
    mmproj = entry.get("mmproj") or ""
    port = int(entry.get("port") or 8080)
    cfg = _entry_cfg(entry)
    log_out = os.path.join(BONSAI_DIR, "bonsai-server.out.log")
    log_err = os.path.join(BONSAI_DIR, "bonsai-server.err.log")
    if not os.path.exists(LLAMA_EXE):
        print(f"llama-server not found at {LLAMA_EXE}", flush=True)
        return False
    if not model or not os.path.exists(model):
        print(f"model not found at {model}", flush=True)
        return False
    args = [LLAMA_EXE, "-m", model]
    if mmproj and os.path.exists(mmproj):
        args += ["--mmproj", mmproj]
    args += ["--alias", entry["id"], "--port", str(port),
             "--ctx-size", str(int(cfg["ctx"])),
             "-ngl", str(int(cfg["ngl"])),
             "--flash-attn", "on",
             "--temp", str(float(cfg["temp"])),
             "--top-p", str(float(cfg["top_p"])),
             "--top-k", str(int(cfg["top_k"]))]
    _LOADING_MODEL = True
    try:
        with open(log_out, "ab") as o, open(log_err, "ab") as e:
            _BONSAI_PROC = subprocess.Popen(args, stdout=o, stderr=e,
                                            creationflags=CREATE_NO_WINDOW)
    except Exception as exc:
        _LOADING_MODEL = False
        print(f"could not start the model server: {exc}", flush=True)
        return False
    for _ in range(150):
        if _bonsai_ready():
            _LOADING_MODEL = False
            return True
        time.sleep(2)
    _LOADING_MODEL = False
    return False


def _ensure_bonsai():
    entry = _active_model()
    if entry and entry.get("type") == "api":
        return _bonsai_ready()
    if _bonsai_ready():
        return True
    with _BONSAI_LOCK:
        if _bonsai_ready():
            return True
        print("Model server not running - starting it...", flush=True)
        return _start_bonsai()


def _stop_local_server(port=8080):
    """Stop the locally-managed llama-server (tracked process, else by port)."""
    global _BONSAI_PROC
    proc = _BONSAI_PROC
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=20)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        _BONSAI_PROC = None
        return True
    if IS_WINDOWS:
        ps = ("$c = Get-NetTCPConnection -LocalPort %d -State Listen "
              "-ErrorAction SilentlyContinue; if ($c) { $c | ForEach-Object "
              "{ Stop-Process -Id $_.OwningProcess -Force "
              "-ErrorAction SilentlyContinue } }" % int(port))
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        return True
    for tool, argv in (("fuser", ["-k", "%d/tcp" % int(port)]),
                       ("lsof", None)):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            if tool == "lsof":
                out = subprocess.run([exe, "-ti", "tcp:%d" % int(port)],
                                     capture_output=True, text=True,
                                     timeout=15).stdout
                for pid in out.split():
                    subprocess.run(["kill", "-9", pid], capture_output=True,
                                   timeout=10)
            else:
                subprocess.run([exe] + argv, capture_output=True, timeout=15)
        except Exception:
            pass
        return True
    return False


# ---------------- Model registry (selector) ----------------

_MODEL_DEFAULTS = {"ctx": 32768, "temp": 1.0, "top_p": 0.95,
                   "top_k": 20, "ngl": 99}


def _model_sanitise_cfg(raw):
    raw = raw if isinstance(raw, dict) else {}

    def num(key, lo, hi, cast, default):
        try:
            val = cast(raw.get(key, default))
        except Exception:
            return default
        return max(lo, min(hi, val))

    return {"ctx": num("ctx", 512, 1048576, int, _MODEL_DEFAULTS["ctx"]),
            "temp": num("temp", 0.0, 2.0, float, _MODEL_DEFAULTS["temp"]),
            "top_p": num("top_p", 0.01, 1.0, float, _MODEL_DEFAULTS["top_p"]),
            "top_k": num("top_k", 0, 1000, int, _MODEL_DEFAULTS["top_k"]),
            "ngl": num("ngl", -1, 999, int, _MODEL_DEFAULTS["ngl"])}


def _entry_cfg(entry):
    cfg = dict(_MODEL_DEFAULTS)
    cfg.update({k: v for k, v in ((entry or {}).get("cfg") or {}).items()
                if k in _MODEL_DEFAULTS})
    return cfg


def _set_model_config(mid, raw):
    cfg = _model_sanitise_cfg(raw)
    with _MODELS_LOCK:
        entry = next((m for m in _MODELS if m["id"] == mid), None)
        if not entry:
            return {"ok": False, "error": "unknown model '%s'" % mid}
        if entry.get("type", "local") != "local":
            return {"ok": False,
                    "error": "sampling settings only apply to local models"}
        entry["cfg"] = cfg
    _save_models()
    return {"ok": True, "id": mid, "cfg": cfg, **_public_models()}


def _default_model_entry():
    return {"id": "bonsai2", "label": "Bonsai 2 27B", "type": "local",
            "path": BONSAI_MODEL, "mmproj": BONSAI_MMPROJ,
            "ctx": 32768, "port": 8080}


def _model_id_from(stem):
    return re.sub(r"[^a-z0-9]+", "-", str(stem).lower()).strip("-")[:48] or "model"


def _common_prefix_tokens(a, b):
    at = re.split(r"[-_.]+", str(a).lower())
    bt = re.split(r"[-_.]+", str(b).lower())
    n = 0
    for x, y in zip(at, bt):
        if x == y:
            n += 1
        else:
            break
    return n


def _discover_models(existing):
    found = []
    seen = set()
    for m in existing:
        if m.get("type", "local") == "local" and m.get("path"):
            seen.add(os.path.normcase(os.path.abspath(m["path"])))
    for folder in (BONSAI_DIR, MODELS_DIR):
        if not os.path.isdir(folder):
            continue
        names = sorted(os.listdir(folder))
        mmprojs = [n for n in names if n.lower().endswith(".gguf")
                   and "mmproj" in n.lower()]
        for name in names:
            if not name.lower().endswith(".gguf") or "mmproj" in name.lower():
                continue
            path = os.path.join(folder, name)
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            stem = os.path.splitext(name)[0]
            best, best_score = "", 1
            for cand in mmprojs:
                score = _common_prefix_tokens(stem, os.path.splitext(cand)[0])
                if score > best_score:
                    best, best_score = cand, score
            mmproj = os.path.join(folder, best) if best else ""
            found.append({"id": _model_id_from(stem), "label": stem,
                          "type": "local", "path": path, "mmproj": mmproj,
                          "ctx": 32768, "port": 8080})
            seen.add(key)
    return found


def _apply_model(entry):
    global BONSAI_BASE, BONSAI_MODEL_ID, BONSAI_CTX, BONSAI_MODEL, \
        BONSAI_MMPROJ, _ACTIVE_MODEL
    if not entry:
        return
    _ACTIVE_MODEL = entry["id"]
    if entry.get("type") == "api":
        BONSAI_BASE = (entry.get("base_url") or "http://127.0.0.1:8080").rstrip("/")
        BONSAI_MODEL_ID = entry.get("model") or entry["id"]
        BONSAI_CTX = int(entry.get("ctx") or BONSAI_CTX)
    else:
        BONSAI_BASE = "http://127.0.0.1:%d" % int(entry.get("port") or 8080)
        BONSAI_MODEL_ID = entry["id"]
        BONSAI_MODEL = entry.get("path") or BONSAI_MODEL
        BONSAI_MMPROJ = entry.get("mmproj") or ""
        BONSAI_CTX = int(entry.get("ctx") or 32768)


def _active_model():
    with _MODELS_LOCK:
        for m in _MODELS:
            if m.get("id") == _ACTIVE_MODEL:
                return dict(m)
        return dict(_MODELS[0]) if _MODELS else None


def _save_models():
    with _MODELS_LOCK:
        data = {"active": _ACTIVE_MODEL, "models": _MODELS}
    try:
        os.makedirs(os.path.dirname(MODELS_FILE), exist_ok=True)
        with open(MODELS_FILE, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def _load_models():
    global _MODELS, _ACTIVE_MODEL
    data = None
    try:
        with open(MODELS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = None
    models, active = [], None
    if isinstance(data, dict):
        models = [m for m in (data.get("models") or [])
                  if isinstance(m, dict) and m.get("id")]
        active = data.get("active")
    default = _default_model_entry()
    if not any(m.get("type", "local") == "local"
               and os.path.normcase(m.get("path") or "") == os.path.normcase(default["path"])
               for m in models):
        models.insert(0, default)
    models.extend(_discover_models(models))
    for m in models:
        _pair_mmproj(m)
    ids = [m["id"] for m in models]
    with _MODELS_LOCK:
        _MODELS = models
        _ACTIVE_MODEL = active if active in ids else models[0]["id"]
    _apply_model(_active_model())


def _pair_mmproj(entry):
    """Attach a vision projector to a local model when one can be matched
    (by filename prefix) and the entry does not have a working one already."""
    if not entry or entry.get("type", "local") != "local":
        return False
    path = entry.get("path") or ""
    if not path or not os.path.isfile(path):
        return False
    current = entry.get("mmproj") or ""
    if current and os.path.isfile(current):
        return False
    folder = os.path.dirname(path)
    found = _mmproj_for(folder, path)
    if found and os.path.isfile(found) and os.path.normcase(found) != \
            os.path.normcase(current):
        entry["mmproj"] = found
        return True
    return False


def _public_models():
    entry = _active_model()
    out = []
    with _MODELS_LOCK:
        models = [dict(m) for m in _MODELS]
    for m in models:
        mtype = m.get("type") or "local"
        item = {"id": m["id"], "label": m.get("label") or m["id"],
                "type": mtype}
        if mtype == "local":
            item["path"] = m.get("path")
            item["available"] = bool(m.get("path") and os.path.exists(m["path"]))
            item["ctx"] = _entry_cfg(m)["ctx"]
            item["cfg"] = _entry_cfg(m)
            item["mmproj"] = m.get("mmproj") or ""
            item["vision"] = bool(item["mmproj"]
                                  and os.path.isfile(item["mmproj"]))
        else:
            item["base_url"] = m.get("base_url")
            item["model"] = m.get("model")
        out.append(item)
    ready = _bonsai_ready()
    return {"active": entry["id"] if entry else None,
            "active_label": (entry or {}).get("label"),
            "managed": (entry or {}).get("type") == "local",
            "ready": ready,
            "loading": bool(_LOADING_MODEL and not ready),
            "defaults": dict(_MODEL_DEFAULTS),
            "vision": (entry or {}).get("type") == "local"
                      and bool((entry or {}).get("mmproj")
                               and os.path.isfile((entry or {}).get("mmproj"))),
            "models": out}


def _select_model(mid):
    with _MODELS_LOCK:
        entry = next((dict(m) for m in _MODELS if m["id"] == mid), None)
    if not entry:
        return {"ok": False, "error": "unknown model '%s'" % mid}
    if mid == _ACTIVE_MODEL and _bonsai_ready():
        return {"ok": True, "already": True, **_public_models()}
    if entry.get("type") == "api":
        _apply_model(entry)
        _save_models()
        return {"ok": True, "type": "api", "ready": _bonsai_ready(),
                **_public_models()}
    if not entry.get("path") or not os.path.exists(entry["path"]):
        return {"ok": False, "error": "model file not found: %s" % entry.get("path")}
    _apply_model(entry)
    # Stop whatever is running so the newly selected model is the one that
    # loads on the next message (and so the old weights free their VRAM now).
    _stop_local_server(int(entry.get("port") or 8080))
    _save_models()
    return {"ok": True, "type": "local", "ready": _bonsai_ready(),
            "detail": "model selected - it loads on your next message",
            **_public_models()}


def _gguf_files(folder, recursive=False, max_depth=3):
    """Every .gguf model file in a folder (mmproj files excluded)."""
    out = []
    folder = os.path.realpath(folder)
    if not os.path.isdir(folder):
        return out
    if not recursive:
        try:
            for n in sorted(os.listdir(folder)):
                p = os.path.join(folder, n)
                if (os.path.isfile(p) and n.lower().endswith(".gguf")
                        and "mmproj" not in n.lower()):
                    out.append(p)
        except Exception:
            pass
        return out
    for root, dirs, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        if rel != "." and rel.count(os.sep) + 1 >= max_depth:
            dirs[:] = []
        dirs[:] = [d for d in dirs
                   if not d.startswith(".") and d != "__pycache__"]
        for n in sorted(files):
            if n.lower().endswith(".gguf") and "mmproj" not in n.lower():
                out.append(os.path.join(root, n))
    return out


def _mmproj_for(folder, model_path):
    """Best-matching mmproj for a model file (by filename prefix)."""
    try:
        cands = [n for n in os.listdir(folder)
                 if n.lower().endswith(".gguf") and "mmproj" in n.lower()]
    except Exception:
        return ""
    stem = os.path.splitext(os.path.basename(model_path))[0]
    best, score = "", 1
    for cand in cands:
        s = _common_prefix_tokens(stem, os.path.splitext(cand)[0])
        if s > score:
            best, score = cand, s
    return os.path.join(folder, best) if best else ""


def _unique_model_id(base):
    with _MODELS_LOCK:
        taken = {m["id"] for m in _MODELS}
    mid = base
    n = 2
    while mid in taken:
        mid = "%s-%d" % (base, n)
        n += 1
    return mid


def _add_model_folder(folder):
    folder = os.path.realpath(str(folder or ""))
    if not os.path.isdir(folder):
        return {"ok": False, "error": "not a folder: %s" % folder}
    files = _gguf_files(folder, recursive=True)
    if not files:
        return {"ok": False,
                "error": "no .gguf model files found in that folder"}
    added, skipped = [], 0
    with _MODELS_LOCK:
        known = {os.path.normcase(os.path.abspath(m.get("path") or ""))
                 for m in _MODELS if m.get("type", "local") == "local"}
        for p in files:
            key = os.path.normcase(os.path.abspath(p))
            if key in known:
                skipped += 1
                continue
            stem = os.path.splitext(os.path.basename(p))[0]
            entry = {"id": _unique_model_id(_model_id_from(stem)),
                     "label": stem, "type": "local", "path": p,
                     "mmproj": _mmproj_for(os.path.dirname(p), p),
                     "ctx": 32768, "port": 8080}
            _MODELS.append(entry)
            known.add(key)
            added.append(entry)
    _save_models()
    return {"ok": True, "added": added, "skipped": skipped,
            "folder": folder, **_public_models()}


def _run_tk_picker(code):
    try:
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=300)
        path = (out.stdout or "").strip()
        return os.path.realpath(path) if path else None
    except Exception:
        return None


def _pick_model_file():
    init = json.dumps(BONSAI_DIR)
    code = ("import tkinter as tk; from tkinter import filedialog; "
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "p = filedialog.askopenfilename("
            "title='Choose a GGUF model file', initialdir=" + init + ", "
            "filetypes=[('GGUF models', '*.gguf'), ('All files', '*.*')]); "
            "r.destroy(); print(p or '')")
    return _run_tk_picker(code)


def _pick_model_folder():
    init = json.dumps(BONSAI_DIR)
    code = ("import tkinter as tk; from tkinter import filedialog; "
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "p = filedialog.askdirectory("
            "title='Choose a folder to scan for .gguf models', "
            "initialdir=" + init + "); r.destroy(); print(p or '')")
    return _run_tk_picker(code)


def _pick_vision_file():
    init = json.dumps(BONSAI_DIR)
    code = ("import tkinter as tk; from tkinter import filedialog; "
            "r = tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "p = filedialog.askopenfilename("
            "title='Choose a vision projector (mmproj .gguf)', "
            "initialdir=" + init + ", filetypes=["
            "('Vision projector (mmproj)', '*mmproj*.gguf'), "
            "('GGUF models', '*.gguf'), ('All files', '*.*')]); "
            "r.destroy(); print(p or '')")
    return _run_tk_picker(code)


def _add_model(body):
    mtype = str(body.get("type") or "local").strip().lower()
    label = str(body.get("label") or "").strip()
    mid = str(body.get("id") or "").strip()
    if mtype == "api":
        base = str(body.get("base_url") or "").strip().rstrip("/")
        model = str(body.get("model") or "").strip()
        if not base.startswith(("http://", "https://")):
            return {"ok": False, "error": "base_url must start with http:// or https://"}
        if not model:
            return {"ok": False, "error": "model id is required for an API endpoint"}
        mid = mid or _model_id_from(label or model)
        entry = {"id": mid, "label": label or model, "type": "api",
                 "base_url": base, "model": model}
    else:
        path = str(body.get("path") or "").strip()
        if not path:
            return {"ok": False, "error": "path is required for a local model"}
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            return {"ok": False, "error": "model file not found: %s" % path}
        mmproj = str(body.get("mmproj") or "").strip()
        try:
            ctx = int(body.get("ctx") or 32768)
        except Exception:
            ctx = 32768
        try:
            port = int(body.get("port") or 8080)
        except Exception:
            port = 8080
        stem = os.path.splitext(os.path.basename(path))[0]
        mid = mid or _model_id_from(stem)
        entry = {"id": mid, "label": label or stem, "type": "local",
                 "path": path,
                 "mmproj": os.path.abspath(mmproj) if mmproj else "",
                 "ctx": ctx, "port": port}
    with _MODELS_LOCK:
        if any(m["id"] == mid for m in _MODELS):
            return {"ok": False, "error": "a model with id '%s' already exists" % mid}
        _MODELS.append(entry)
    _save_models()
    return {"ok": True, "added": entry, **_public_models()}


def _remove_model(mid):
    with _MODELS_LOCK:
        if mid == _ACTIVE_MODEL:
            return {"ok": False, "error": "cannot remove the active model - switch first"}
        before = len(_MODELS)
        _MODELS[:] = [m for m in _MODELS if m["id"] != mid]
        if len(_MODELS) == before:
            return {"ok": False, "error": "unknown model '%s'" % mid}
    _save_models()
    return {"ok": True, **_public_models()}


def _set_model_mmproj(mid, mmproj):
    """Attach, replace or clear a model's vision projector."""
    mmproj = str(mmproj or "").strip()
    if mmproj:
        mmproj = os.path.abspath(mmproj)
        if not os.path.isfile(mmproj):
            return {"ok": False, "error": "projector not found: %s" % mmproj}
        if not mmproj.lower().endswith(".gguf"):
            return {"ok": False, "error": "a vision projector must be a .gguf file"}
    with _MODELS_LOCK:
        entry = next((m for m in _MODELS if m["id"] == mid), None)
        if not entry:
            return {"ok": False, "error": "unknown model '%s'" % mid}
        if entry.get("type", "local") != "local":
            return {"ok": False,
                    "error": "vision projectors only apply to local models"}
        entry["mmproj"] = mmproj
    _save_models()
    return {"ok": True, "id": mid, "mmproj": mmproj, **_public_models()}


def _scan_models():
    with _MODELS_LOCK:
        found = _discover_models(_MODELS)
        _MODELS.extend(found)
        paired = sum(1 for m in _MODELS if _pair_mmproj(m))
    if found or paired:
        _save_models()
    return {"ok": True, "added": len(found), "paired": paired,
            **_public_models()}


_load_models()


def _touch_activity():
    global _LAST_ACTIVITY
    with _ACTIVITY_LOCK:
        _LAST_ACTIVITY = time.time()


def _port_listening(port=8080, host="127.0.0.1", timeout=1.5):
    """True if something accepts connections on the port. Unlike /health this
    also sees a llama-server that is still loading the model."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _unload_bonsai():
    """Free the model's RAM/VRAM now. Local models: stop the llama-server
    process (it is restarted automatically on the next message). External API
    models: best-effort soft unload via keep_alive=0."""
    entry = _active_model()
    if entry and entry.get("type") == "api":
        try:
            _http_json(BONSAI_BASE + "/v1/chat/completions",
                       {"model": BONSAI_MODEL_ID,
                        "messages": [{"role": "user", "content": "unload"}],
                        "max_tokens": 1, "keep_alive": 0}, timeout=60)
            return {"ok": True, "method": "keep_alive",
                    "detail": "asked the external server to unload the model"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    port = int(entry.get("port") or 8080) if entry else 8080
    was_running = _port_listening(port)
    _stop_local_server(port)
    for _ in range(60):
        if not _port_listening(port):
            break
        time.sleep(0.5)
    stopped = not _port_listening(port)
    if not was_running:
        return {"ok": True, "method": "already stopped",
                "detail": "the model server was not running"}
    if stopped:
        return {"ok": True, "method": "stopped", "port": port,
                "detail": "model server stopped - RAM/VRAM freed; it "
                          "restarts automatically on the next message"}
    return {"ok": False, "port": port,
            "error": "the model server did not stop (port %d still "
                     "listening)" % port}


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
        "description": "Run a shell command on this PC and return its "
                       "output. Use it to build, test, install, debug or check "
                       "system info: python, pip, git, npm, node, ls, dir, "
                       "ps, ping, etc. Runs in the workspace "
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
        "description": "Run a snippet in a sandboxed environment and return its "
                       "output. Languages available on this PC are auto-detected - "
                       "typically: python, node (JavaScript). Also supported when "
                       "installed: go, lua, php, ruby, perl, bash. Use it to test "
                       "functions, verify logic, parse data or prototype before "
                       "touching real files. Runs in an isolated temp folder that "
                       "is deleted afterwards. Default timeout 30s, up to 600s.",
        "parameters": {
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "description": "Language to run: 'python', 'node' (or 'js'), "
                                   "'go', 'lua', 'php', 'ruby', 'perl', 'bash'. "
                                   "If the runtime is missing this returns the "
                                   "list actually available."
                },
                "code": {
                    "type": "string",
                    "description": "The source code to run."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Max seconds to run (1-600). Default 30. "
                                   "Raise it for slow work, e.g. simulations."
                }
            },
            "required": ["language", "code"]
        }
    }
}


PREVIEW_HTML_TOOL = {
    "type": "function",
    "function": {
        "name": "preview_html",
        "description": "Live-preview an HTML page you created in the workspace. "
                       "Call it after writing an .html/.htm file (e.g. a chart, "
                       "a report or a small web app) - the UI will show a live "
                       "iframe of the page. The page is served from this PC, so "
                       "JavaScript and local assets work. Returns the preview "
                       "URL.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Relative workspace path of the .html/.htm "
                                   "file, e.g. 'out/report.html'."
                }
            },
            "required": ["path"]
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


WINDOW_LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "window_list",
        "description": "List the open desktop windows: window title, process name "
                       "and PID for every visible top-level window. Use this "
                       "first to discover what is running, then bring one to the "
                       "front with window_action.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": []
        }
    }
}

WINDOW_ACTION_TOOL = {
    "type": "function",
    "function": {
        "name": "window_action",
        "description": "Control an existing desktop window: bring a window to the "
                       "front and give it keyboard focus, maximize it, minimize it "
                       "or restore it. Find a matching window first with "
                       "window_list. The window can be matched by a substring of "
                       "its title ('title') or by its process name/PID "
                       "('process'/'pid'). 'focus' makes the window visible, "
                       "restores it if minimized and steals focus so that "
                       "take_screenshot/control_input act on it.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["focus", "maximize", "minimize", "restore"],
                    "description": "What to do with the window. Default: focus."
                },
                "title": {
                    "type": "string",
                    "description": "Substring of the window title to match."
                },
                "process": {
                    "type": "string",
                    "description": "Process name (e.g. 'notepad.exe') to match."
                },
                "pid": {
                    "type": "integer",
                    "description": "Exact process ID to match."
                }
            },
            "required": ["action"]
        }
    }
}


def _pc_tool(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required
            }
        }
    }


DOCKER_PS_TOOL = _pc_tool(
    "docker_ps",
    "List Docker containers. Include stopped ones with 'all'. Returns id, name, "
    "image and status per container. Use docker_logs/docker_exec/docker_start/"
    "docker_stop with the returned names.",
    {"all": {"type": "boolean", "description": "Include stopped containers."}},
    []
)

DOCKER_IMAGES_TOOL = _pc_tool(
    "docker_images",
    "List Docker images available locally (repository, tag, id, size).",
    {"filter": {"type": "string", "description": "Optional name filter (repository substring)."}},
    []
)

DOCKER_START_TOOL = _pc_tool(
    "docker_start",
    "Start a Docker container by name or id.",
    {"name": {"type": "string", "description": "Container name or id."}},
    ["name"]
)

DOCKER_STOP_TOOL = _pc_tool(
    "docker_stop",
    "Stop a running Docker container by name or id.",
    {"name": {"type": "string", "description": "Container name or id."},
     "timeout": {"type": "integer", "description": "Seconds to wait before killing (default 10)."}},
    ["name"]
)

DOCKER_RESTART_TOOL = _pc_tool(
    "docker_restart",
    "Restart a Docker container by name or id.",
    {"name": {"type": "string", "description": "Container name or id."}},
    ["name"]
)

DOCKER_LOGS_TOOL = _pc_tool(
    "docker_logs",
    "Read the logs of a Docker container by name or id.",
    {"name": {"type": "string", "description": "Container name or id."},
     "tail": {"type": "integer", "description": "Number of lines from the end (default 100)."}},
    ["name"]
)

DOCKER_EXEC_TOOL = _pc_tool(
    "docker_exec",
    "Run a command inside a running Docker container (docker exec with sh -c). "
    "Returns the command output.",
    {"name": {"type": "string", "description": "Container name or id."},
     "command": {"type": "string", "description": "The shell command to run inside the container, e.g. 'ls -la /app'."}},
    ["name", "command"]
)

API_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": "api_call",
        "description": "Make an HTTP request to any REST or GraphQL API and read "
                       "the response: status code, headers and body. Works for "
                       "GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS. For GraphQL send "
                       "a JSON body like {\"query\": \"...\"} - the response JSON "
                       "is returned automatically. Handles JSON bodies from dict "
                       "input, or raw text bodies.",
        "parameters": {
            "type": "object",
            "properties": {
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"], "description": "HTTP method. Default GET."},
                "url": {"type": "string", "description": "Full URL, e.g. https://api.example.com/v1/users"},
                "headers": {"type": "object", "description": "Extra request headers, e.g. {\"Authorization\": \"Bearer ...\"}"},
                "body": {"description": "Request body. Use a dict/list for JSON or a string for raw text."},
                "content_type": {"type": "string", "description": "Content-Type header for string bodies (default 'application/json')."},
                "timeout": {"type": "integer", "description": "Request timeout in seconds (default 20, max 120)."},
                "insecure": {"type": "boolean", "description": "Skip TLS certificate verification (default false)."},
                "follow_redirects": {"type": "boolean", "description": "Follow HTTP redirects (default true)."}
            },
            "required": ["url"]
        }
    }
}

WS_TEST_TOOL = {
    "type": "function",
    "function": {
        "name": "ws_test",
        "description": "Connect to a WebSocket server, optionally send a message, "
                       "and collect the replies that arrive before the timeout. "
                       "Useful to test APIs, push feeds, or debug local sockets.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "WebSocket URL, e.g. ws://127.0.0.1:8000/ws or wss://..."},
                "send": {"description": "Optional message to send after connecting. Dicts/lists are JSON-encoded."},
                "timeout": {"type": "integer", "description": "How long to listen for replies, seconds (default 5, max 60)."}
            },
            "required": ["url"]
        }
    }
}

SCHEDULE_TOOL = {
    "type": "function",
    "function": {
        "name": "schedule_task",
        "description": "Schedule a shell command to run later (and while this PC "
                       "is on): once after a delay, on an interval, or on a cron "
                       "schedule. Intervals are 'every N seconds'. Cron is a "
                       "5-field expression: minute hour day-of-month month "
                       "day-of-week (0 or 7 = Sunday). Examples: '15 3 * * *' "
                       "daily at 03:15; '*/5 * * * *' every 5 minutes; "
                       "'0 9 * * 1-5' weekdays at 09:00. The command runs via the "
                       "system shell (cmd). Tasks are saved, so they survive a "
                       "restart of Bonsai, and a one-shot task that came due "
                       "while Bonsai was closed runs as soon as it starts again. "
                       "Check runs with list_schedules; cancel with "
                       "unschedule_task.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique name for this task."},
                "command": {"type": "string", "description": "The shell command to run."},
                "when": {"type": "string", "enum": ["once", "interval", "cron"], "description": "Schedule kind. Default 'once'."},
                "delay_seconds": {"type": "integer", "description": "For 'once': seconds to wait before running (default 1)."},
                "interval_seconds": {"type": "integer", "description": "For 'interval': seconds between runs (min 5)."},
                "cron": {"type": "string", "description": "For 'cron': 5-field cron expression."},
                "timeout": {"type": "integer", "description": "Max seconds the command may run (default 180, max 600)."}
            },
            "required": ["name", "command"]
        }
    }
}

LIST_SCHEDULES_TOOL = _pc_tool(
    "list_schedules",
    "List all scheduled tasks: name, schedule kind, status (active, running or "
    "completed), next run time, last run, how many times it ran and the tail of "
    "its last output. One-shot tasks stay in the list with status 'completed' "
    "after they run, so their output stays visible until you remove them.",
    {},
    []
)

UNSCHEDULE_TOOL = _pc_tool(
    "unschedule_task",
    "Cancel a scheduled task by name, or delete a completed one from the list. "
    "Already-started runs are not interrupted.",
    {"name": {"type": "string", "description": "Task name to cancel."}},
    ["name"]
)

TTS_VOICES_TOOL = _pc_tool(
    "tts_voices",
    "List the local Piper text-to-speech voices available on this machine "
    "(each .onnx + .onnx.json pair in the Piper folder). Use tts_speak with "
    "one of the returned names.",
    {},
    []
)

TTS_SPEAK_TOOL = {
    "type": "function",
    "function": {
        "name": "tts_speak",
        "description": "Speak text aloud using the local neural Piper TTS "
                       "engine (no cloud, no Windows voices). Also saves the "
                       "speech as a WAV file in the workspace. Use this to give "
                       "the user spoken feedback, alerts or TTS output.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to speak."},
                "voice": {"type": "string", "description": "Piper voice name, default 'en_US-lessac-medium'. Romanian: 'ro_RO-mihai-medium'. List with tts_voices."}
            },
            "required": ["text"]
        }
    }
}

SCREENSHOT_WINDOW_TOOL = {
    "type": "function",
    "function": {
        "name": "screenshot_window",
        "description": "Screenshot ONE specific window, matched by a substring of "
                       "its title (or a process name / PID), and look at it. "
                       "Use this instead of take_screenshot + guessing: it crops "
                       "to the window and the image is fed straight to your eyes. "
                       "A minimized window is restored automatically; the list of "
                       "open windows is suggested in the error if nothing matches.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Substring of the window title, e.g. 'YouTube' or 'Visual Studio Code'."},
                "process": {"type": "string",
                            "description": "Process name to match instead of the title, e.g. 'chrome'."},
                "pid": {"type": "integer",
                        "description": "Exact window PID (from window_list)."},
                "focus": {"type": "boolean",
                          "description": "Bring the window to the front first (default false)."},
                "save": {"type": "boolean",
                         "description": "Also save a PNG copy in the workspace."}
            },
            "required": []
        }
    }
}

WAIT_FOR_TOOL = {
    "type": "function",
    "function": {
        "name": "wait_for",
        "description": "Wait until something becomes true instead of polling with "
                       "screenshots. Kinds: 'file' (a file appears, optionally "
                       "reaching min_bytes - perfect for a download finishing), "
                       "'port' (a port starts accepting connections), 'url' (an "
                       "HTTP 2xx/3xx), 'process' (an app launches), 'window' (a "
                       "window title appears), 'text' (a string shows up in a "
                       "file), 'screen_text' (text becomes visible on screen, "
                       "needs OCR). Combine with must_disappear to wait for "
                       "something to go away. Never returns an error on timeout - "
                       "it reports timed_out plus what it last saw.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {"type": "string",
                         "enum": ["file", "port", "url", "process", "window",
                                  "text", "screen_text"],
                         "description": "What to wait for."},
                "target": {"type": "string",
                           "description": "File path (workspace-relative or absolute inside it), URL, process name, or window title."},
                "port": {"type": "integer",
                         "description": "Port number when kind=port."},
                "text": {"type": "string",
                         "description": "Text to look for when kind=text or kind=screen_text."},
                "min_bytes": {"type": "integer",
                              "description": "kind=file: wait until the file is at least this many bytes (0 = any size)."},
                "must_disappear": {"type": "boolean",
                                   "description": "Wait until the condition is FALSE instead (e.g. a spinner is gone)."},
                "timeout": {"type": "integer",
                            "description": "Give up after this many seconds (1-600, default 30)."},
                "poll_interval": {"type": "number",
                                  "description": "Seconds between checks (default 0.5)."}
            },
            "required": ["kind"]
        }
    }
}

COPY_CLIPBOARD_TOOL = {
    "type": "function",
    "function": {
        "name": "copy_to_clipboard",
        "description": "Put text on the system clipboard so any other app can "
                       "paste it (hand a result from one app to another). Use "
                       "format=json for structured data - it is validated before "
                       "being copied. Pair with paste_from_clipboard.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "The text to copy."},
                "format": {"type": "string", "enum": ["text", "json", "html"],
                           "description": "Type hint. json is validated first so you never paste broken JSON. Default text."}
            },
            "required": ["text"]
        }
    }
}

PASTE_CLIPBOARD_TOOL = {
    "type": "function",
    "function": {
        "name": "paste_from_clipboard",
        "description": "Read the system clipboard. format=auto (default) picks up "
                       "a copied PICTURE and returns it as an image you can see, "
                       "plus any text alongside it. Use text for plain text and "
                       "json to get the clipboard parsed into an object.",
        "parameters": {
            "type": "object",
            "properties": {
                "format": {"type": "string",
                           "enum": ["auto", "text", "json", "image"],
                           "description": "What you expect on the clipboard. 'auto' also detects images. Default auto."},
                "max_chars": {"type": "integer",
                              "description": "Truncate text after this many characters (default 8000)."}
            },
            "required": []
        }
    }
}

CLICK_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "click_text",
        "description": "Click a button or link by the TEXT it shows - no pixel "
                       "guessing. The screen (or one named window) is read with "
                       "OCR, the best match is located - tolerating typos - and "
                       "its centre is clicked. If the text cannot be found the "
                       "error lists close matches under 'did_you_mean'. Use "
                       "dry_run first to see what would be clicked, and prefer a "
                       "window= argument so background windows are not scanned.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The visible label to click, e.g. 'Subscribe'."},
                "window": {"type": "string",
                           "description": "Only look inside this window (substring of its title) instead of the whole screen."},
                "exact": {"type": "boolean",
                          "description": "Require the exact word instead of a phrase/fuzzy match."},
                "button": {"type": "string", "enum": ["left", "right", "middle"],
                           "description": "Mouse button. Default left."},
                "double_click": {"type": "boolean",
                                 "description": "Double-click instead of single-click."},
                "dry_run": {"type": "boolean",
                            "description": "Only report what would be clicked; do not click."}
            },
            "required": ["text"]
        }
    }
}

NEW_TOOLS = [WINDOW_LIST_TOOL, WINDOW_ACTION_TOOL,
             SCREENSHOT_WINDOW_TOOL, WAIT_FOR_TOOL,
             COPY_CLIPBOARD_TOOL, PASTE_CLIPBOARD_TOOL, CLICK_TEXT_TOOL,
             DOCKER_PS_TOOL, DOCKER_IMAGES_TOOL, DOCKER_START_TOOL,
             DOCKER_STOP_TOOL, DOCKER_RESTART_TOOL, DOCKER_LOGS_TOOL,
             DOCKER_EXEC_TOOL, API_CALL_TOOL, WS_TEST_TOOL,
             SCHEDULE_TOOL, LIST_SCHEDULES_TOOL, UNSCHEDULE_TOOL,
             TTS_VOICES_TOOL, TTS_SPEAK_TOOL, PREVIEW_HTML_TOOL]

PC_TOOLS = [SHOT_TOOL, INPUT_TOOL, CLIPBOARD_TOOL,
            DOWNLOAD_TOOL, ARCHIVE_TOOL, ASK_TOOL, TODO_TOOL] + NEW_TOOLS


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
                    url = str((part.get("image_url") or {}).get("url") or "")
                    if url and len(url) > MAX_IMAGE_BYTES:
                        texts.append("[Image skipped: it is larger than the "
                                     "%d MB limit]" % (MAX_IMAGE_BYTES //
                                                        (1024 * 1024)))
                    else:
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


def _linux_capture_png(bbox=None):
    """Capture the full screen on Linux via scrot / ImageMagick (X11)."""
    from PIL import Image
    exe = shutil.which("scrot") or shutil.which("import")
    if not exe:
        raise RuntimeError("screenshot capture failed on Linux: install scrot "
                           "or imagemagick (import) - sudo apt install scrot "
                           "(X11 session required)")
    import tempfile
    tmp = tempfile.mktemp(suffix=".png")
    try:
        if os.path.basename(exe) == "scrot":
            subprocess.run([exe, "-z", tmp], check=True, timeout=30,
                           creationflags=CREATE_NO_WINDOW)
        else:
            subprocess.run([exe, "-window", "root", tmp], check=True,
                           timeout=30, creationflags=CREATE_NO_WINDOW)
        img = Image.open(tmp)
        if bbox:
            img = img.crop(bbox)
        return img
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


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
        if IS_LINUX:
            try:
                img = _linux_capture_png(bbox)
            except Exception as exc2:
                return {"error": f"screenshot capture failed: {exc}; "
                                 f"{exc2}"}
        else:
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


_WAIT_KINDS = ("file", "port", "url", "process", "window", "text",
               "screen_text")


def _proc_running(name):
    name = str(name or "").strip().lower()
    if not name:
        return False
    if IS_WINDOWS:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=15,
                             creationflags=CREATE_NO_WINDOW).stdout
        return any(name in line.lower() for line in out.splitlines())
    try:
        out = subprocess.run(["pgrep", "-if", name], capture_output=True,
                             text=True, timeout=10).stdout
        return bool(out.strip())
    except Exception:
        return False


def _url_status(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA},
                                 method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return int(getattr(resp, "status", 200) or 200)


def _ocr_available():
    try:
        import importlib.util
        return (importlib.util.find_spec("pytesseract") is not None and
                importlib.util.find_spec("PIL") is not None)
    except Exception:
        return False


def _full_screen_bbox():
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab(all_screens=True)
        return (0, 0, im.size[0], im.size[1])
    except Exception:
        pass
    try:
        import ctypes
        user32 = ctypes.windll.user32  # noqa: F841
        return (0, 0, user32.GetSystemMetrics(78), user32.GetSystemMetrics(79))
    except Exception:
        return (0, 0, 1920, 1080)


def _wait_for(args):
    kind = str(args.get("kind") or "file").strip().lower()
    if kind not in _WAIT_KINDS:
        return {"error": "kind must be one of: %s" % ", ".join(_WAIT_KINDS)}
    try:
        timeout = min(max(float(args.get("timeout") or 30), 1), 600)
    except Exception:
        timeout = 30.0
    try:
        interval = min(max(float(args.get("poll_interval") or 0.5), 0.1), 10)
    except Exception:
        interval = 0.5
    target = args.get("target")
    if kind == "port":
        target = args.get("port", args.get("target"))
    if kind in ("file", "text"):
        if not str(target or "").strip():
            return {"error": "target (the file path) is required for kind=%s"
                             % kind}
        try:
            _safe_path(str(target))
        except ValueError as exc:
            return {"error": str(exc)}
    started = time.time()
    deadline = started + timeout
    checks = 0
    last_detail = ""
    want_gone = bool(args.get("must_disappear"))

    def probe():
        if kind == "file":
            path = str(target or "")
            p = _safe_path(path)
            if not os.path.exists(p):
                return False, "not found: %s" % path
            size = 0
            try:
                size = os.path.getsize(p)
            except Exception:
                pass
            if args.get("min_bytes") is not None:
                try:
                    if size < int(args["min_bytes"]):
                        return False, "still smaller than %s bytes" % args["min_bytes"]
                except Exception:
                    return False, "size unavailable"
            return True, "%s (%d bytes)" % (p, size)
        if kind == "port":
            port = int(target)
            if _port_listening(port):
                return True, "port %d is accepting connections" % port
            return False, "port %d not open" % port
        if kind == "url":
            code = _url_status(str(target), timeout=min(10, max(2, timeout)))
            return (200 <= code < 400), "HTTP %s" % code
        if kind == "process":
            if _proc_running(target):
                return True, "process %r is running" % str(target)
            return False, "process %r not running" % str(target)
        if kind == "window":
            if _window_find(title=str(target or "")):
                return True, "window %r is open" % str(target)
            return False, "no window matching %r" % str(target)
        if kind == "text":
            path = str(target or "")
            p = _safe_path(path)
            if not os.path.isfile(p):
                return False, "not a file: %s" % path
            try:
                with open(p, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
            except Exception as exc:
                return False, "unreadable: %s" % exc
            needle = str(args.get("text") or "")
            hit = needle in body
            return hit, ("found %r" % needle if hit
                         else "%r not in the file yet" % needle)
        # kind == screen_text
        if not _ocr_available():
            return False, ("OCR unavailable - install pytesseract + the "
                           "Tesseract engine")
        needle = str(args.get("text") or "").strip().lower()
        if not needle:
            return False, "text is required for kind=screen_text"
        try:
            import pytesseract
            img = _capture_bbox(_full_screen_bbox())
            body = (pytesseract.image_to_string(img) or "").lower()
        except Exception as exc:
            return False, "OCR failed: %s" % exc
        hit = needle in body
        return hit, ("saw %r on screen" % needle if hit
                     else "%r not visible yet" % needle)

    while True:
        checks += 1
        try:
            ok, detail = probe()
        except Exception as exc:
            ok, detail = False, "probe error: %s" % exc
        last_detail = detail
        if want_gone:
            if not ok:
                return {"result": "ok", "kind": kind, "target": target,
                        "detail": "gone: " + detail,
                        "elapsed": round(time.time() - started, 2),
                        "checks": checks, "timed_out": False}
        elif ok:
            return {"result": "ok", "kind": kind, "target": target,
                    "detail": detail, "elapsed": round(time.time() - started, 2),
                    "checks": checks, "timed_out": False}
        if time.time() >= deadline:
            break
        time.sleep(interval)
    return {"result": "ok", "kind": kind, "target": target, "timed_out": True,
            "detail": last_detail, "elapsed": round(time.time() - started, 2),
            "checks": checks,
            "hint": "still not true after %.0fs - take a screenshot to see the "
                    "current state" % timeout}


_CLIP_FORMATS = ("text", "json", "html", "auto", "image")


def _clip_write(text, fmt="text"):
    fmt = str(fmt or "text").strip().lower()
    if fmt not in _CLIP_FORMATS:
        return {"error": "format must be one of: %s" % ", ".join(_CLIP_FORMATS)}
    payload = "" if text is None else str(text)
    if fmt == "json":
        try:
            json.loads(payload)
        except Exception as exc:
            return {"error": "that is not valid JSON: %s" % exc}
    if fmt == "image":
        return {"error": "use paste_from_clipboard to read an image; "
                         "copy_to_clipboard writes text/json/html"}
    try:
        import pyperclip
    except Exception as exc:
        return {"error": "pyperclip is not installed: %s" % exc}
    try:
        pyperclip.copy(payload)
    except Exception as exc:
        return {"error": "could not write to the clipboard: %s" % exc}
    return {"result": "ok", "action": "copy", "format": fmt,
            "chars": len(payload), "preview": payload[:120]}


def _clip_read(fmt="auto", max_chars=8000):
    fmt = str(fmt or "auto").strip().lower()
    if fmt not in _CLIP_FORMATS:
        return {"error": "format must be one of: %s" % ", ".join(_CLIP_FORMATS)}
    out = {"result": "ok", "action": "paste", "format": fmt}
    if fmt in ("auto", "image"):
        img = _clipboard_image()
        if img is not None:
            data_uri, w, h = _image_payload(img, quality=85, max_edge=1280)
            out.update({"type": "image", "width": w, "height": h,
                        "image": data_uri})
            if fmt == "image":
                return out
    try:
        import pyperclip
    except Exception as exc:
        if out.get("type") == "image":
            return out
        return {"error": "pyperclip is not installed: %s" % exc}
    try:
        text = pyperclip.paste() or ""
    except Exception as exc:
        if out.get("type") == "image":
            return out
        return {"error": "could not read the clipboard: %s" % exc}
    out["text"] = text[:max_chars]
    out["chars"] = len(text)
    out["truncated"] = len(text) > max_chars
    if fmt == "json":
        try:
            out["json"] = json.loads(text)
        except Exception as exc:
            out["json_error"] = str(exc)
    if out.get("type") == "image":
        out["text"] = text[:max_chars] if text else ""
    return out


def _dib_to_image(raw):
    """Build a PIL image straight from a Windows CF_DIB payload.

    Pillow's ImageGrab.grabclipboard() returns a lazy DibImageFile that can
    fail to materialise ('image file is truncated'), so decode the bitmap
    ourselves for the common 24/32-bit uncompressed cases."""
    try:
        from PIL import Image
    except Exception:
        return None
    if not raw or len(raw) < 40:
        return None
    try:
        header = struct.unpack_from("<IiiHHIIiiII", raw, 0)
        (_size, width, height, planes, bpp, _comp, _img_size,
         _xppm, _yppm, _clr_used, _clr_important) = header
    except Exception:
        return None
    if planes != 1 or bpp not in (16, 24, 32):
        return None
    if header[5] not in (0, 3):  # BI_RGB / BI_BITFIELDS
        return None
    flip = height > 0            # positive height = bottom-up rows
    height = abs(height)
    if width <= 0 or height <= 0 or width * height > 80_000_000:
        return None
    stride = ((width * bpp + 31) // 32) * 4
    offset = struct.unpack_from("<I", raw, 0)[0]
    if bpp <= 8:
        offset += 4 * (1 << bpp)
    need = offset + stride * height
    if len(raw) < need:
        return None
    pixels = raw[offset:need]
    mode, rawmode = ("BGRX", "BGRX") if bpp == 32 else ("RGB", "BGR")
    try:
        img = Image.frombuffer("RGB", (width, height), pixels, "raw",
                               rawmode, stride, 1)
    except Exception:
        return None
    if flip:
        try:
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
        except Exception:
            pass
    return img


def _clipboard_image():
    """Return a PIL image of the clipboard picture, or None."""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        from PIL import ImageGrab
        data = ImageGrab.grabclipboard()
    except Exception:
        data = None
    if isinstance(data, Image.Image):
        for attempt in (lambda: data.convert("RGB"), lambda: data.copy()):
            try:
                return attempt()
            except Exception:
                continue
    elif isinstance(data, list):
        for item in data:
            try:
                if os.path.isfile(item):
                    return Image.open(item).convert("RGB")
            except Exception:
                continue
    if IS_WINDOWS:
        try:
            import win32clipboard
            import win32con
            win32clipboard.OpenClipboard()
            try:
                raw = win32clipboard.GetClipboardData(win32con.CF_DIB)
            finally:
                win32clipboard.CloseClipboard()
            return _dib_to_image(bytes(raw))
        except Exception:
            return None
    if IS_LINUX:
        exe = shutil.which("xclip")
        if exe:
            try:
                out = subprocess.run([exe, "-selection", "clipboard",
                                      "-t", "image/png", "-o"],
                                     capture_output=True, timeout=5)
                if out.stdout:
                    import io
                    return Image.open(io.BytesIO(out.stdout)).convert("RGB")
            except Exception:
                pass
    return None


def _ocr_words(img):
    """[(word, confidence, x, y, w, h)] from pytesseract, or None."""
    try:
        import pytesseract
    except Exception:
        return None
    try:
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    except Exception as exc:
        raise exc
    words = []
    n = len(data.get("text") or [])
    for i in range(n):
        text = (data["text"][i] or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1
        if conf < 30:
            continue
        words.append({"text": text, "conf": conf,
                      "x": int(data["left"][i]), "y": int(data["top"][i]),
                      "w": int(data["width"][i]), "h": int(data["height"][i])})
    return words


def _click_text(args):
    """Find text on screen (OCR) and click it - no pixel guessing needed."""
    target = str(args.get("text") or "").strip()
    if not target:
        return {"error": "text is required (the visible label to click)"}
    win = str(args.get("window") or "").strip() or None
    exact = bool(args.get("exact"))
    button = str(args.get("button") or "left").strip().lower()
    clicks = 2 if args.get("double_click") else 1
    if not _ocr_available():
        return {"error": "click_text needs OCR - install pytesseract and the "
                         "Tesseract engine (winget install UB-Mannheim.TesseractOCR)"}
    offset = (0, 0)
    scope = "screen"
    if win:
        geo = _window_geometry(win)
        if not geo:
            return {"error": "no window matched %r - use window_list" % win}
        bbox = geo["bbox"]
        offset = (bbox[0], bbox[1])
        scope = "window:%s" % (geo["title"] or win)
    else:
        bbox = _full_screen_bbox()
    try:
        img = _capture_bbox(bbox)
    except Exception as exc:
        return {"error": "could not capture the screen: %s" % exc}
    try:
        words = _ocr_words(img)
    except Exception as exc:
        return {"error": "OCR failed: %s" % exc}
    if not words:
        return {"error": "OCR found no readable text in the %s - try taking a "
                         "screenshot to see what is visible" % scope}
    needle = target.lower()
    hit = None
    if exact:
        for w in words:
            if w["text"].lower() == needle:
                hit = w
                break
    else:
        # 1) a single word that already contains the needle wins outright
        run1 = None
        for w in words:
            if needle in w["text"].lower():
                run1 = [w]
                break
        # 2) otherwise the shortest multi-word run on one line that contains it
        run2 = None
        if run1 is None:
            for i in range(len(words)):
                run, joined = [words[i]], words[i]["text"].lower()
                for j in range(i + 1, min(i + 6, len(words))):
                    nxt, prev = words[j], words[j - 1]
                    if nxt["y"] - prev["y"] > max(14, words[i]["h"]):
                        break
                    run.append(nxt)
                    joined = joined + " " + nxt["text"].lower()
                    if needle in joined:
                        run2 = run
                        break
                if run2:
                    break
        # 3) last resort: the closest single word, if the length is plausible
        scored = []
        if run1 is None and run2 is None:
            for w in words:
                wl = w["text"].lower()
                s = _similarity(needle, wl)
                if abs(len(wl) - len(needle)) <= max(2, len(needle) // 3) \
                        and s >= 0.75:
                    scored.append((s, w))
            if scored:
                scored.sort(key=lambda p: p[0], reverse=True)
                run1 = [scored[0][1]]
        best_run = run1 or run2
        if best_run:
            xs = [w["x"] for w in best_run]
            ys = [w["y"] for w in best_run]
            x2 = [w["x"] + w["w"] for w in best_run]
            y2 = [w["y"] + w["h"] for w in best_run]
            hit = {"text": " ".join(w["text"] for w in best_run),
                   "conf": round(min(w["conf"] for w in best_run), 1),
                   "x": min(xs), "y": min(ys),
                   "w": max(x2) - min(xs), "h": max(y2) - min(ys)}
        else:
            scored = []
            for w in words:
                wl = w["text"].lower()
                s = _similarity(needle, wl)
                if needle in wl:
                    s = max(s, 0.85)
                # only accept a fuzzy single-word hit when the lengths are
                # plausible, so "OK" never matches the letter "k"
                if abs(len(wl) - len(needle)) <= max(2, len(needle) // 3) \
                        and s >= 0.75:
                    scored.append((s, w))
            if scored:
                scored.sort(key=lambda p: p[0], reverse=True)
                hit = scored[0][1]
    if not hit:
        near, seen_words = [], set()
        for w in sorted(words, key=lambda w: _similarity(needle, w["text"].lower()),
                        reverse=True):
            t = w["text"]
            if t.lower() in seen_words:
                continue
            seen_words.add(t.lower())
            if _similarity(needle, t.lower()) > 0.45:
                near.append(t)
            if len(near) >= 8:
                break
        return {"error": "could not find %r on the %s" % (target, scope),
                "did_you_mean": near,
                "words_seen": len(words)}
    cx = offset[0] + hit["x"] + hit["w"] // 2
    cy = offset[1] + hit["y"] + hit["h"] // 2
    if args.get("dry_run"):
        return {"result": "ok", "action": "dry_run", "matched": hit["text"],
                "confidence": hit["conf"], "scope": scope,
                "x": cx, "y": cy}
    try:
        import pyautogui
    except Exception as exc:
        return {"error": "pyautogui is not installed: %s" % exc}
    try:
        pyautogui.moveTo(cx, cy, duration=0.15)
        pyautogui.click(clicks=clicks, button=button)
    except Exception as exc:
        return {"error": "click failed: %s" % exc}
    return {"result": "ok", "action": "click", "matched": hit["text"],
            "confidence": hit["conf"], "scope": scope, "clicks": clicks,
            "button": button, "x": cx, "y": cy}


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


# ---------------- Local neural TTS (Piper) ----------------

def _piper_voices():
    if not os.path.isdir(PIPER_DIR):
        return {"error": f"Piper voices folder not found at {PIPER_DIR} "
                         "(set PC_PIPER_DIR or run python piper\\download_voices.py)"}
    voices = []
    for f in sorted(os.listdir(PIPER_DIR)):
        if f.endswith(".onnx"):
            base = f[:-5]
            if os.path.exists(os.path.join(PIPER_DIR, base + ".onnx.json")):
                voices.append(base)
    if not voices:
        return {"error": f"no Piper voices (.onnx + .onnx.json) found in {PIPER_DIR}"}
    return {"result": "ok", "folder": PIPER_DIR, "voices": voices}


def _play_wav(path):
    try:
        import numpy as np
        import sounddevice as sd
        import wave as wave_mod
        with wave_mod.open(path, "rb") as wv:
            n = wv.getnframes()
            sr = wv.getframerate()
            raw = wv.readframes(n)
        data = np.frombuffer(raw, dtype=np.int16)
        sd.play(data, sr)  # non-blocking; keeps playing in the background
        return round(n / sr, 2)
    except Exception:
        try:
            import winsound
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            return None
        except Exception:
            pass
        for player in ("paplay", "aplay", "ffplay"):
            exe = shutil.which(player)
            if not exe:
                continue
            try:
                cmd = [exe, path]
                if player == "ffplay":
                    cmd = [exe, "-nodisp", "-autoexit", "-loglevel",
                           "quiet", path]
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL,
                                 creationflags=CREATE_NO_WINDOW)
                return None
            except Exception:
                continue
        return None


def _tts_speak(args):
    import wave as wave_mod
    text = str(args.get("text") or "").strip()
    if not text:
        return {"error": "text is required"}
    voice = str(args.get("voice") or "en_US-lessac-medium").strip()
    voice = voice[:-5] if voice.endswith(".onnx") else voice
    model = os.path.join(PIPER_DIR, voice + ".onnx")
    conf = os.path.join(PIPER_DIR, voice + ".onnx.json")
    if not (os.path.exists(model) and os.path.exists(conf)):
        return {"error": f"voice '{voice}' not found in {PIPER_DIR} "
                         "(run tts_voices to list the available voices)"}
    try:
        import piper
    except Exception as exc:
        return {"error": f"piper-tts is not installed: pip install piper-tts onnxruntime ({exc})"}
    out_dir = os.path.join(WORKDIR, "out", "tts")
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception:
        out_dir = os.path.join(WORKDIR, "out")
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception:
            out_dir = WORKDIR
    wav_path = os.path.join(out_dir,
                            "tts_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                            + ".wav")
    try:
        v = piper.PiperVoice.load(model, conf)
        with open(wav_path, "wb") as wf:
            v.synthesize_wav(text, wave_mod.Wave_write(wf))
    except Exception as exc:
        return {"error": f"piper synthesis failed: {exc}"}
    duration = None
    try:
        import wave as wave_mod2
        with wave_mod2.open(wav_path, "rb") as wv:
            duration = round(wv.getnframes() / max(1, wv.getframerate()), 2)
    except Exception:
        pass
    _play_wav(wav_path)
    return {"result": "ok", "voice": voice, "text": text[:200],
            "wav": wav_path.replace("\\", "/"),
            "bytes": os.path.getsize(wav_path),
            "duration_seconds": duration}


# ---------------- Window management ----------------

def _wmctrl():
    return shutil.which("wmctrl")


def _linux_window_rows():
    wm = _wmctrl()
    if not wm:
        return None
    try:
        proc = subprocess.run([wm, "-l", "-x"], capture_output=True,
                              text=True, timeout=10, creationflags=CREATE_NO_WINDOW)
    except Exception:
        return None
    rows = []
    for line in (proc.stdout or "").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 2 or not parts[0].startswith("0x"):
            continue
        row = {"id": parts[0], "desktop": parts[1] if len(parts) > 1 else "",
               "process": parts[2] if len(parts) > 2 else "",
               "title": (parts[3] if len(parts) > 3 else "")[:160], "pid": None}
        rows.append(row)
    return rows


def _linux_windows():
    rows = _linux_window_rows()
    if rows is None:
        return {"error": "window tools on Linux need 'wmctrl' (X11): "
                         "sudo apt install wmctrl  (Wayland compositors "
                         "support window management only partially)"}
    rows.sort(key=lambda w: (w.get("process") or "").lower())
    return {"result": "ok", "windows": rows[:80], "count": len(rows)}


def _window_list():
    if not IS_WINDOWS:
        return _linux_windows()
    if not _WIN_UI:
        return {"error": "window tools need pywin32 + psutil on Windows"}
    out = []
    try:
        def _cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return
            pid = None
            pname = None
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pass
            if pid:
                try:
                    pname = psutil.Process(pid).name()
                except Exception:
                    pass
            out.append({"title": title[:160], "process": pname, "pid": pid})
        win32gui.EnumWindows(_cb, None)
    except Exception as exc:
        return {"error": f"window enumeration failed: {exc}"}
    out = [w for w in out if w.get("pid")]
    out.sort(key=lambda w: (w.get("process") or "").lower())
    return {"result": "ok", "windows": out[:80], "count": len(out)}


def _window_geometry(name=None, process=None, pid=None):
    """Resolve a window to {'title','bbox','platform'} - None if not found.

    Windows uses win32; Linux uses wmctrl -G (geometry) and falls back to
    xdotool. Used by screenshot_window and click_text."""
    if IS_WINDOWS:
        if not _WIN_UI:
            return None
        hwnd = _window_find(name, process, pid)
        if not hwnd:
            return None
        try:
            if win32gui.IsIconic(hwnd):
                # a minimized window reports an off-screen dummy rect
                win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
                time.sleep(0.35)
        except Exception:
            pass
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return None
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return None
        try:
            title = win32gui.GetWindowText(hwnd)
        except Exception:
            title = ""
        return {"title": title, "bbox": (left, top, right, bottom),
                "width": w, "height": h, "platform": "windows"}
    rows = _linux_window_rows_geo()
    if not rows:
        return None
    t = str(name or "").strip().lower() if name else None
    p = str(process or "").strip().lower() if process else None
    hit = None
    for row in rows:
        if t and t in (row.get("title") or "").lower():
            hit = row
            break
        if p and p in (row.get("process") or "").lower() and hit is None:
            hit = row
    if not hit or not hit.get("bbox"):
        return None
    left, top, right, bottom = hit["bbox"]
    return {"title": hit.get("title") or "", "bbox": (left, top, right, bottom),
            "width": right - left, "height": bottom - top,
            "platform": "linux"}


def _linux_window_rows_geo():
    """wmctrl rows including geometry: wmctrl -l -G -x"""
    wm = _wmctrl()
    rows = []
    if wm:
        try:
            proc = subprocess.run([wm, "-l", "-G", "-x"], capture_output=True,
                                  text=True, timeout=10,
                                  creationflags=CREATE_NO_WINDOW)
            for line in (proc.stdout or "").splitlines():
                parts = line.split(None, 7)
                # 0xID DESKTOP WM_CLASS X Y WIDTH HEIGHT HOSTNAME TITLE
                if len(parts) >= 8 and parts[0].startswith("0x"):
                    try:
                        x, y, w, h = (int(v) for v in parts[3:7])
                    except ValueError:
                        continue
                    rows.append({"id": parts[0], "process": parts[2],
                                 "title": parts[7][:160], "bbox": (x, y, x + w, y + h)})
        except Exception:
            pass
    if rows:
        return rows
    for row in (_linux_window_rows() or []):
        geo = _xdotool_geometry(row.get("id"))
        if geo:
            row["bbox"] = geo
            rows.append(row)
    return rows


def _xdotool_geometry(win_id):
    exe = shutil.which("xdotool")
    if not exe or not win_id:
        return None
    try:
        out = subprocess.run([exe, "getwindowgeometry", "--shell", str(win_id)],
                             capture_output=True, text=True,
                             timeout=10).stdout
        vals = {}
        for line in out.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip().upper()] = v.strip()
        x, y = int(vals.get("X", 0)), int(vals.get("Y", 0))
        w, h = int(vals.get("WIDTH", 0)), int(vals.get("HEIGHT", 0))
        if w <= 0 or h <= 0:
            return None
        return (x, y, x + w, y + h)
    except Exception:
        return None


def _capture_bbox(bbox):
    """Grab a screen rectangle as a PIL image, with the Linux fallback."""
    from PIL import ImageGrab
    try:
        return ImageGrab.grab(bbox=tuple(bbox), all_screens=True)
    except Exception as exc:
        if IS_LINUX:
            return _linux_capture_png(tuple(bbox))
        raise exc


def _image_payload(img, quality=82, max_edge=1280):
    """Downscale + JPEG-encode a PIL image, returning (data_uri, w, h)."""
    from PIL import Image
    w, h = img.size
    view = img
    if max(w, h) > max_edge:
        r = max_edge / float(max(w, h))
        size = (max(1, int(w * r)), max(1, int(h * r)))
        try:
            view = img.resize(size, Image.Resampling.LANCZOS)
        except Exception:
            view = img.resize(size)
    if view.mode != "RGB":
        view = view.convert("RGB")
    buf = io.BytesIO()
    view.save(buf, "JPEG", quality=quality)
    data_uri = "data:image/jpeg;base64," + \
        base64.b64encode(buf.getvalue()).decode("ascii")
    return data_uri, w, h


def _screenshot_window(args):
    """Capture one specific window (matched by title substring or process)."""
    name = str(args.get("name") or args.get("title") or "").strip()
    process = str(args.get("process") or "").strip() or None
    pid = args.get("pid")
    if not name and not process and pid is None:
        return {"error": "name (or process / pid) is required - it matches the "
                         "window title; use window_list to see what is open"}
    if args.get("focus"):
        try:
            _window_action({"action": "focus", "title": name or None,
                            "process": process, "pid": pid})
            time.sleep(0.4)
        except Exception:
            pass
    geo = _window_geometry(name or None, process, pid)
    if not geo:
        avail = ""
        try:
            rows = _window_rows_public()
            avail = " | open windows: " + ", ".join(
                repr(r.get("title")) for r in rows[:8]) if rows else ""
        except Exception:
            pass
        return {"error": "no window matched %r%s" % (name or process or pid, avail)}
    try:
        img = _capture_bbox(geo["bbox"])
    except Exception as exc:
        return {"error": "could not capture the window: %s" % exc}
    data_uri, w, h = _image_payload(img)
    out = {"result": "ok", "matched": geo["title"] or (name or process),
           "width": w, "height": h, "platform": geo["platform"],
           "image": data_uri}
    save = args.get("save")
    if save:
        try:
            out_dir = os.path.join(WORKDIR, "screenshots")
            os.makedirs(out_dir, exist_ok=True)
            png = os.path.join(out_dir, "window_" +
                               time.strftime("%Y%m%d_%H%M%S") + ".png")
            img.save(png, "PNG")
            out["saved"] = png.replace("\\", "/")
        except Exception:
            pass
    return out


def _window_rows_public():
    res = _window_list()
    return res.get("windows") or []


def _window_find(title=None, process=None, pid=None):
    if not IS_WINDOWS:
        rows = _linux_window_rows()
        if not rows:
            return None
        t = str(title or "").strip().lower() if title else None
        p = str(process or "").strip().lower() if process else None
        fallback = None
        for w in rows:
            if t and t in w.get("title", "").lower():
                return w["id"]
            if p and p in w.get("process", "").lower():
                if fallback is None:
                    fallback = w["id"]
        return fallback
    if not _WIN_UI:
        return None
    if pid is not None:
        pid = int(pid)
    t = str(title or "").strip().lower() if title else None
    p = str(process or "").strip().lower() if process else None
    handles = []
    try:
        win32gui.EnumWindows(lambda h, _: handles.append(h), None)
    except Exception:
        return None
    fallback = None
    for h in handles:
        try:
            if not win32gui.IsWindowVisible(h):
                continue
            wtitle = win32gui.GetWindowText(h)
            wpid = None
            try:
                _, wpid = win32process.GetWindowThreadProcessId(h)
            except Exception:
                wpid = None
            if pid is not None and wpid == pid:
                return h
            if t and t in wtitle.lower():
                return h
            if p:
                pname = None
                if wpid:
                    try:
                        pname = psutil.Process(wpid).name().lower()
                    except Exception:
                        pname = None
                if pname and p in pname:
                    if fallback is None:
                        fallback = h
        except Exception:
            continue
    return fallback


def _window_action(args):
    if not IS_WINDOWS:
        wm = _wmctrl()
        if not wm:
            return {"error": "window tools on Linux need 'wmctrl' (X11): "
                             "sudo apt install wmctrl  (Wayland compositors "
                             "support window management only partially)"}
        action = str(args.get("action") or "focus")
        wid = str(args.get("id") or "")
        if not wid:
            wid = _window_find(args.get("title"), args.get("process"),
                               args.get("pid"))
        if not wid:
            return {"error": "no matching window found (list windows first "
                            "with window_list, or pass an 'id' from it)"}
        try:
            if action == "minimize":
                subprocess.run([wm, "-ir", wid, "-b", "add,hidden"],
                               capture_output=True, timeout=10,
                               creationflags=CREATE_NO_WINDOW)
            elif action == "maximize":
                subprocess.run([wm, "-ir", wid, "-b",
                                "add,maximized_vert,maximized_horz"],
                               capture_output=True, timeout=10,
                               creationflags=CREATE_NO_WINDOW)
            else:  # focus / restore
                subprocess.run([wm, "-ia", wid], capture_output=True,
                               timeout=10, creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            return {"error": f"window action failed: {exc}"}
        rows = {w["id"]: w for w in (_linux_window_rows() or [])}
        return {"result": "ok", "action": action,
                "window": (rows.get(wid, {}).get("title") or "")[:160]}
    if not _WIN_UI:
        return {"error": "window tools need pywin32 + psutil on Windows"}
    action = str(args.get("action") or "focus")
    hwnd = _window_find(args.get("title"), args.get("process"), args.get("pid"))
    if not hwnd:
        return {"error": "no matching window found (list windows first with window_list)"}
    try:
        if action == "minimize":
            win32gui.ShowWindow(hwnd, 6)  # SW_MINIMIZE
        elif action == "maximize":
            win32gui.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        else:  # focus / restore
            win32gui.ShowWindow(hwnd, 9)  # SW_RESTORE
            win32gui.ShowWindow(hwnd, 5)  # SW_SHOW
            try:
                ctypes.windll.user32.keybd_event(0x12, 0, 0, 0)
                ctypes.windll.user32.keybd_event(0x12, 0, 0x2, 0)
            except Exception:
                pass
            win32gui.BringWindowToTop(hwnd)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
    except Exception as exc:
        return {"error": f"window action failed: {exc}"}
    title = win32gui.GetWindowText(hwnd)
    return {"result": "ok", "action": action, "window": title[:160]}


# ---------------- Docker tools ----------------

def _docker_run(args_list, timeout=120):
    cmd = ["docker"] + list(args_list)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout, creationflags=CREATE_NO_WINDOW)
    except FileNotFoundError:
        return {"error": "docker CLI not found - is Docker Desktop installed, running and on PATH?"}
    except subprocess.TimeoutExpired:
        return {"error": f"docker command timed out after {timeout}s"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        return {"error": (err or out)[:1500]}
    return {"result": "ok", "output": out[:12000]}


def _docker_ps(args):
    fmt = r"{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}"
    cmd = ["ps", "--format", fmt]
    if args.get("all"):
        cmd.append("-a")
    res = _docker_run(cmd)
    if res.get("error"):
        return res
    rows = []
    for line in res["output"].splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            rows.append({"id": parts[0], "name": parts[1],
                         "image": parts[2], "status": parts[3]})
    return {"result": "ok", "containers": rows, "count": len(rows)}


def _docker_images(args):
    fmt = r"{{.Repository}}\t{{.Tag}}\t{{.ID}}\t{{.Size}}"
    res = _docker_run(["images", "--format", fmt])
    if res.get("error"):
        return res
    filt = str(args.get("filter") or "").strip().lower()
    rows = []
    for line in res["output"].splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            repo, tag, iid, size = parts[0], parts[1], parts[2], parts[3]
            if filt and filt not in repo.lower():
                continue
            rows.append({"repository": repo, "tag": tag, "id": iid, "size": size})
    return {"result": "ok", "images": rows, "count": len(rows)}


def _docker_action(action, args):
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "container name is required"}
    if action == "stop" and args.get("timeout"):
        secs = max(1, min(int(args.get("timeout")), 120))
        res = _docker_run(["stop", "-t", str(secs), name])
    else:
        res = _docker_run([action, name])
    if res.get("error"):
        return res
    return {"result": "ok", "action": action, "container": res["output"] or name}


def _docker_logs(args):
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "container name is required"}
    tail = max(1, min(int(args.get("tail") or 100), 5000))
    res = _docker_run(["logs", "--tail", str(tail), name], timeout=120)
    if res.get("error"):
        return res
    return {"result": "ok", "container": name, "logs": res["output"]}


def _docker_exec(args):
    name = str(args.get("name") or "").strip()
    command = str(args.get("command") or "").strip()
    if not name:
        return {"error": "container name is required"}
    if not command:
        return {"error": "command is required"}
    res = _docker_run(["exec", name, "sh", "-c", command], timeout=180)
    if res.get("error"):
        return res
    return {"result": "ok", "container": name, "output": res["output"]}


# ---------------- API client ----------------

def _api_call(args):
    try:
        import requests
    except Exception as exc:
        return {"error": "api_call needs the 'requests' package: " + str(exc)}
    method = str(args.get("method") or "GET").upper()
    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "url is required"}
    try:
        timeout = min(max(int(args.get("timeout") or 20), 1), 120)
    except Exception:
        timeout = 20
    headers = {str(k): str(v) for k, v in (args.get("headers") or {}).items()}
    body = args.get("body")
    data = None
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body)
            headers.setdefault("Content-Type", "application/json")
        else:
            data = str(body)
            ct = str(args.get("content_type") or "application/json")
            headers.setdefault("Content-Type", ct)
    verify = not bool(args.get("insecure"))
    follow = bool(args.get("follow_redirects", True))
    try:
        t0 = time.monotonic()
        resp = requests.request(method, url, headers=headers, data=data,
                                timeout=timeout, verify=verify,
                                allow_redirects=follow)
        elapsed = round((time.monotonic() - t0) * 1000)
    except Exception as exc:
        return {"error": f"request failed: {exc}"}
    text = ""
    try:
        text = resp.content.decode("utf-8", "replace")
    except Exception:
        text = ""
    parsed = None
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "json" in ctype:
        try:
            parsed = resp.json()
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            parsed = None
    capped = text if len(text) <= 8000 else text[:8000] + "\n[... response body truncated ...]"
    return {"result": "ok", "method": method, "url": url,
            "status": resp.status_code,
            "elapsed_ms": elapsed,
            "content_type": ctype,
            "headers": dict(list(resp.headers.items())[:20]),
            "response": capped}


def _ws_test(args):
    try:
        import websocket
    except Exception as exc:
        return {"error": "ws_test needs the 'websocket-client' package: " + str(exc)}
    url = str(args.get("url") or "").strip()
    if not url:
        return {"error": "url is required (ws:// or wss://)"}
    try:
        timeout = min(max(int(args.get("timeout") or 5), 1), 60)
    except Exception:
        timeout = 5
    try:
        ws = websocket.create_connection(url, timeout=timeout)
    except Exception as exc:
        return {"error": f"connection failed: {exc}"}
    received = []
    try:
        send = args.get("send")
        if send is not None:
            payload = json.dumps(send) if isinstance(send, (dict, list)) else str(send)
            ws.send(payload)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                ws.settimeout(max(0.1, deadline - time.monotonic()))
                msg = ws.recv()
                received.append(str(msg))
            except Exception as exc:
                if isinstance(exc, websocket.WebSocketTimeoutException):
                    break
                break
    finally:
        try:
            ws.close()
        except Exception:
            pass
    capped = [m if len(m) <= 4000 else m[:4000] + "[... truncated ...]" for m in received]
    return {"result": "ok", "messages_received": capped[:20], "count": len(received)}


# ---------------- Scheduled tasks ----------------

def _cron_field(field, lo, hi):
    field = str(field).strip()
    if field == "*":
        return set(range(lo, hi + 1))
    out = set()
    for part in field.split(","):
        part = part.strip()
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            step = max(1, int(step_s or 1))
        if part in ("*", ""):
            out.update(range(lo, hi + 1, step))
        elif "-" in part:
            a, _, b = part.partition("-")
            out.update(range(int(a), int(b) + 1, step))
        else:
            v = int(part)
            if lo <= v <= hi:
                out.add(v)
    return out


_CRON_DOW = {0: 6, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4, 6: 5, 7: 6}  # cron -> Python weekday


def _cron_next(expr, after):
    parts = expr.split()
    if len(parts) != 5:
        return None
    try:
        minutes = _cron_field(parts[0], 0, 59)
        hours = _cron_field(parts[1], 0, 23)
        doms = _cron_field(parts[2], 1, 31)
        months = _cron_field(parts[3], 1, 12)
        dows = {_CRON_DOW[d] for d in _cron_field(parts[4], 0, 7)}
    except Exception:
        return None
    probe = after.replace(second=0, microsecond=0) + datetime.timedelta(minutes=1)
    for _ in range(2 * 366 * 24 * 60):
        if (probe.minute in minutes and probe.hour in hours
                and probe.day in doms and probe.month in months
                and probe.weekday() in dows):
            return probe
        probe += datetime.timedelta(minutes=1)
    return None


def _sched_public(job):
    nxt = job.get("next_fire")
    if job.get("done"):
        status = "completed"
    elif job.get("_running"):
        status = "running"
    else:
        status = "active"
    return {"name": job["name"], "when": job.get("when"),
            "command": job.get("command"),
            "status": status,
            "next_run": datetime.datetime.fromtimestamp(nxt).isoformat(timespec="seconds") if nxt else None,
            "last_run": job.get("last_fire"),
            "count": job.get("count", 0),
            "interval_seconds": job.get("interval"),
            "cron": job.get("cron"),
            "last_output": job.get("last_output")}


def _sched_file():
    return os.path.join(BONSAI_DIR, "schedules.json")


def _sched_save_locked():
    jobs = []
    for job in _SCHED.values():
        if job.get("_cancel"):
            continue
        jobs.append({k: job.get(k) for k in
                     ("name", "command", "when", "timeout", "count", "next_fire",
                      "last_fire", "last_output", "interval", "cron", "delay",
                      "done")})
    path = _sched_file()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "jobs": jobs}, fh, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def _sched_load():
    global _SCHED_LOADED
    with _SCHED_LOCK:
        if _SCHED_LOADED:
            return
        _SCHED_LOADED = True
    try:
        with open(_sched_file(), encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return
    now = time.time()
    restored = []
    for raw in data.get("jobs") or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        command = str(raw.get("command") or "").strip()
        when = str(raw.get("when") or "once")
        if not name or not command or when not in ("once", "interval", "cron"):
            continue
        job = {"name": name, "command": command, "when": when,
               "timeout": int(raw.get("timeout") or 180),
               "count": int(raw.get("count") or 0),
               "last_fire": raw.get("last_fire"),
               "last_output": raw.get("last_output"),
               "_running": False, "_cancel": False}
        if when == "cron":
            expr = str(raw.get("cron") or "").strip()
            nxt = _cron_next(expr, datetime.datetime.now()) if len(expr.split()) == 5 else None
            if not nxt:
                continue
            job["cron"] = expr
            job["next_fire"] = nxt.timestamp()
        elif when == "interval":
            job["interval"] = max(5, int(raw.get("interval") or 60))
            due = float(raw.get("next_fire") or 0)
            job["next_fire"] = now if due and due <= now else (due or now + job["interval"])
        else:
            due = float(raw.get("next_fire") or 0)
            if raw.get("done") or not due:
                job["done"] = True
                job["next_fire"] = 0
            else:
                job["next_fire"] = due
        restored.append(job)
    if restored:
        with _SCHED_LOCK:
            for job in restored:
                _SCHED.setdefault(job["name"], job)
        _ensure_sched_thread()


def _sched_push_event(job, out):
    with _SCHED_LOCK:
        _SCHED_EVENTS.append({"name": job["name"], "command": job["command"],
                              "when": job.get("when"),
                              "ran_at": job.get("last_fire"),
                              "output": (out or "")[:2000]})
        if len(_SCHED_EVENTS) > 50:
            del _SCHED_EVENTS[:-50]


def _sched_take_events():
    with _SCHED_LOCK:
        events = list(_SCHED_EVENTS)
        del _SCHED_EVENTS[:]
    return events


def _schedule_task(args):
    name = str(args.get("name") or "").strip()
    command = str(args.get("command") or "").strip()
    if not name:
        return {"error": "task name is required"}
    if not command:
        return {"error": "shell command is required"}
    when = str(args.get("when") or "once").lower()
    try:
        timeout = min(max(int(args.get("timeout") or 180), 1), 600)
    except Exception:
        timeout = 180
    job = {"name": name, "command": command, "when": when,
           "timeout": timeout, "count": 0, "_running": False, "_cancel": False}
    if when == "once":
        delay = max(1, int(args.get("delay_seconds") or 1))
        job["delay"] = delay
        job["next_fire"] = time.time() + delay
    elif when == "interval":
        interval = max(5, int(args.get("interval_seconds") or 60))
        job["interval"] = interval
        job["next_fire"] = time.time() + interval
    elif when == "cron":
        expr = str(args.get("cron") or "").strip()
        if len(expr.split()) != 5:
            return {"error": "cron must be a 5-field expression: 'min hour day-of-month month day-of-week'"}
        nxt = _cron_next(expr, datetime.datetime.now())
        if not nxt:
            return {"error": "cron expression does not match a time within the next 2 years - check the fields"}
        job["cron"] = expr
        job["next_fire"] = nxt.timestamp()
    else:
        return {"error": "when must be one of: once, interval, cron"}
    with _SCHED_LOCK:
        _SCHED[name] = job
        _sched_save_locked()
    _ensure_sched_thread()
    return {"result": "ok", "scheduled": True, "task": _sched_public(job)}


def _list_schedules(args):
    with _SCHED_LOCK:
        items = [_sched_public(j) for j in _SCHED.values() if not j.get("_cancel")]
    items.sort(key=lambda j: (j.get("status") == "completed", j.get("name") or ""))
    return {"result": "ok", "scheduled_tasks": items, "count": len(items)}


def _unschedule_task(args):
    name = str(args.get("name") or "").strip()
    if not name:
        return {"error": "task name is required"}
    with _SCHED_LOCK:
        job = _SCHED.pop(name, None)
        if not job:
            for key in [n for n in _SCHED if n.lower() == name.lower()]:
                job = _SCHED.pop(key, None)
                break
        if job:
            job["_cancel"] = True
        _sched_save_locked()
    return {"result": "ok", "removed": bool(job), "name": name,
            "detail": "removed" if job else "no scheduled task with that name"}


def _ensure_sched_thread():
    global _SCHED_THREAD_ON
    _sched_load()
    with _SCHED_LOCK:
        if _SCHED_THREAD_ON:
            return
        _SCHED_THREAD_ON = True
    threading.Thread(target=_sched_loop, daemon=True, name="bonsai-scheduler").start()


def _sched_loop():
    while True:
        time.sleep(0.5)
        with _SCHED_LOCK:
            pending = bool(_SCHED)
        if not pending:
            continue
        now = time.time()
        to_fire = []
        with _SCHED_LOCK:
            for name, job in list(_SCHED.items()):
                if job.get("_cancel") or job.get("_running") or job.get("done"):
                    continue
                n = job.get("next_fire")
                if n and n <= now:
                    to_fire.append(job)
        for job in to_fire:
            with _SCHED_LOCK:
                if job.get("_cancel") or job.get("_running") or job.get("done"):
                    continue
                job["_running"] = True
            threading.Thread(target=_sched_run, args=(job,), daemon=True).start()


def _sched_run(job):
    try:
        proc = subprocess.Popen(job["command"], shell=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, creationflags=CREATE_NO_WINDOW)
        try:
            out, _ = proc.communicate(timeout=job.get("timeout") or 180)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            out = "[command timed out]"
    except Exception as exc:
        out = "error: " + str(exc)
    with _SCHED_LOCK:
        job["last_fire"] = datetime.datetime.now().isoformat(timespec="seconds")
        job["last_output"] = (out or "")[:2000]
        job["count"] = job.get("count", 0) + 1
        job["_running"] = False
        if job["when"] == "once":
            job["done"] = True
            job["next_fire"] = 0
        elif job["when"] == "interval":
            job["next_fire"] = time.time() + max(5, int(job.get("interval") or 60))
        elif job["when"] == "cron":
            nxt = _cron_next(job["cron"], datetime.datetime.now())
            job["next_fire"] = nxt.timestamp() if nxt else 0
            if not nxt:
                job["done"] = True
        finished = sorted((j for j in _SCHED.values() if j.get("done")),
                          key=lambda j: j.get("last_fire") or "")
        for stale in finished[:-_SCHED_DONE_MAX]:
            _SCHED.pop(stale["name"], None)
        _sched_save_locked()
    _sched_push_event(job, out)


def execute_tool_call(tc, hooks=None):
    fn = tc.get("function") or {}
    name = fn.get("name") or "launch_or_open"
    args = parse_arguments(fn.get("arguments"))
    image_uri = None
    _PATH_TLS.on_ask = (hooks or {}).get("on_ask")
    _PATH_TLS.tool = name
    _PATH_TLS.once = set()
    try:
        return _execute_tool_call(name, args, tc, hooks, image_uri)
    finally:
        with _PATH_LOCK:
            for gone in [p for p, g in _PATH_GRANTS.items() if g.get("scope") == "once"]:
                _PATH_GRANTS.pop(gone, None)
        _PATH_TLS.on_ask = None
        _PATH_TLS.tool = None
        _PATH_TLS.once = None


def _execute_tool_call(name, args, tc, hooks, image_uri):
    global _TODOS
    if name == "launch_or_open":
        raw_result = launch_or_open(args.get("name", ""))
    elif name == "web_search":
        raw_result = _web_search(args.get("query"), 6,
                                 str(args.get("action") or "search").lower()
                                 == "suggest")
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
    elif name == "screenshot_window":
        shot = _screenshot_window(args)
        if shot.get("error"):
            raw_result = {"error": shot["error"]}
        else:
            image_uri = shot.pop("image", None)
            raw_result = shot
    elif name == "wait_for":
        raw_result = _wait_for(args)
    elif name == "copy_to_clipboard":
        raw_result = _clip_write(args.get("text"),
                                 args.get("format") or "text")
    elif name == "paste_from_clipboard":
        pasted = _clip_read(args.get("format") or "auto",
                            int(args.get("max_chars") or 8000))
        if pasted.get("error"):
            raw_result = {"error": pasted["error"]}
        else:
            image_uri = pasted.pop("image", None)
            raw_result = pasted
    elif name == "click_text":
        raw_result = _click_text(args)
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
        raw_result = _run_code(args.get("language"), args.get("code"),
                               timeout=args.get("timeout"))
    elif name == "window_list":
        raw_result = _window_list()
    elif name == "window_action":
        raw_result = _window_action(args)
    elif name == "docker_ps":
        raw_result = _docker_ps(args)
    elif name == "docker_images":
        raw_result = _docker_images(args)
    elif name in ("docker_start", "docker_stop", "docker_restart"):
        raw_result = _docker_action(name[len("docker_"):], args)
    elif name == "docker_logs":
        raw_result = _docker_logs(args)
    elif name == "docker_exec":
        raw_result = _docker_exec(args)
    elif name == "api_call":
        raw_result = _api_call(args)
    elif name == "ws_test":
        raw_result = _ws_test(args)
    elif name == "schedule_task":
        raw_result = _schedule_task(args)
    elif name == "list_schedules":
        raw_result = _list_schedules(args)
    elif name == "unschedule_task":
        raw_result = _unschedule_task(args)
    elif name == "tts_voices":
        raw_result = _piper_voices()
    elif name == "tts_speak":
        if not tts_enabled():
            raw_result = {"error": "TTS is OFF - click the TTS button in the "
                                   "header to enable spoken replies, then ask again"}
        else:
            raw_result = _tts_speak(args)
    elif name == "preview_html":
        raw_result = _preview_html(args.get("path"))
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
    try:
        final = _bonsai_chat(msgs, [])
        content = (final.get("content") or "").strip()
        if content:
            return {"reply": content, "calls": calls, "_stats": total}
    except Exception:
        pass
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

    def emit(ev):
        nonlocal total
        kind = ev["kind"]
        if kind == "reason":
            if on_reason:
                on_reason(ev["text"])
        elif kind == "delta":
            if on_delta:
                on_delta(ev["text"])
        elif kind == "end":
            total = _merge_stats(total, ev.get("stats") or {})
            if on_stats:
                on_stats(total)

    def one_round(tools_for_round):
        tool_calls = None
        for ev in _bonsai_stream(msgs, tools_for_round):
            if ev["kind"] == "end":
                tool_calls = ev["tool_calls"]
                emit(ev)
            else:
                emit(ev)
        return tool_calls or []

    for _round in range(MAX_TOOL_ROUNDS):
        tool_calls = one_round(tools)
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
    # Tool budget exhausted but the model still wants tools - force a plain,
    # tool-free closing answer so the run never ends in silence.
    one_round([])
    return {"calls": calls, "_stats": total}


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
    color-scheme: dark;
    --bg: #0a0a0c;
    --bg2: #121216;
    --bg3: #191920;
    --bg4: #22222a;
    --bd: #272730;
    --bd2: #35353f;
    --txt: #e8e8ec;
    --txt2: #c2c2cb;
    --mut: #85858f;
    --acc: #38bdf8;
    --acc2: #0ea5e9;
    --ok: #34d399;
    --warn: #fbbf24;
    --err: #f87171;
    --violet: #a78bfa;
    --hud-bg: var(--bg2);
    --panel-border: var(--bd);
    --cyan-glow: #38bdf8;
    --blue-glow: #0ea5e9;
    --gold-glow: #fbbf24;
    --red-glow: #f87171;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: "Segoe UI", system-ui, -apple-system, sans-serif;
    background: var(--bg); color: var(--txt); overflow-x: hidden;
  }
  .mono { font-family: Consolas, "Courier New", monospace; }
  ::-webkit-scrollbar { width: 9px; height: 9px; }
  ::-webkit-scrollbar-track { background: transparent; }
  ::-webkit-scrollbar-thumb { background: #33333c; border-radius: 6px; }
  ::-webkit-scrollbar-thumb:hover { background: #45454f; }

  .hud-border { background: var(--bg2); border: 1px solid var(--bd); border-radius: 12px; position: relative; }

  .app { display: flex; flex-direction: column; height: 100vh; padding: 12px; }
  header { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 10px; padding: 10px 14px; }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand .chip-ic {
    width: 34px; height: 34px; border-radius: 50%; border: 1px solid var(--bd2);
    display: flex; align-items: center; justify-content: center; background: var(--bg3); color: var(--acc); font-size: 14px;
  }
  .brand h1 { font-size: 18px; margin: 0; letter-spacing: 3px; color: var(--txt); font-family: Consolas, monospace; font-weight: 700; }
  .brand p { margin: 0; font-size: 10.5px; color: var(--mut); letter-spacing: 1.5px; font-family: Consolas, monospace; }
  .hstatus { display: flex; gap: 16px; font-size: 12.5px; color: var(--mut); font-family: Consolas, monospace; }
  .hstatus b { color: var(--txt2); font-weight: 600; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--ok); margin-right: 4px; }
  .dot.idle { background: var(--err); }
  .dot.busy { animation: dotLoad 1.2s ease-in-out infinite; }
  @keyframes dotLoad {
    0%, 100% { background: var(--warn); box-shadow: 0 0 0 0 rgba(251,191,36,.5); }
    50% { background: var(--ok); box-shadow: 0 0 0 5px rgba(52,211,153,0); }
  }
  .hclock { text-align: right; font-family: Consolas, monospace; }
  .hclock .t { font-size: 17px; font-weight: 700; color: var(--txt); }
  .hclock .d { font-size: 11px; color: var(--mut); }
  .hbtn {
    background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2);
    padding: 8px 12px; border-radius: 8px; cursor: pointer; font-size: 12.5px; transition: all .15s;
  }
  .hbtn:hover { background: var(--bg4); border-color: var(--acc); color: var(--txt); }
  .hbtn.on { background: rgba(52, 211, 153, .14); border-color: var(--ok); color: var(--ok); }
  .hbtn.add { padding: 8px 11px; font-weight: 700; font-size: 14px; }
  select.hbtn { appearance: none; -webkit-appearance: none; max-width: 190px; text-overflow: ellipsis; }
  select.hbtn option { background: #16161b; color: var(--txt2); }
  .modelsel.off { border-color: var(--err); color: var(--err); }

  /* add-model modal */
  .mdl { position: fixed; inset: 0; z-index: 90; display: none; align-items: center; justify-content: center; background: rgba(0,0,0,.62); backdrop-filter: blur(3px); }
  .mdl.on { display: flex; }
  .mdlbox { width: min(560px, 92vw); background: var(--bg2); border: 1px solid var(--bd2); border-radius: 14px; padding: 18px; font-family: Consolas, monospace; }
  .mdlbox h4 { margin: 0 0 12px; font-size: 13px; letter-spacing: 1.5px; color: var(--acc); }
  .mdlbox .act { display: block; width: 100%; text-align: left; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 10px; padding: 10px 12px; margin-bottom: 8px; cursor: pointer; font: inherit; font-size: 13px; }
  .mdlbox .act:hover { background: var(--bg4); border-color: var(--acc); color: var(--txt); }
  .mdlbox .act small { display: block; color: var(--mut); font-size: 11px; margin-top: 3px; }
  .mdlbox .act.inline { width: auto; margin: 0; padding: 10px 16px; }
  .mdlbox .mdlsep { color: var(--mut); font-size: 11px; letter-spacing: 1px; text-transform: uppercase; margin: 14px 0 8px; }
  .mdlbox .row { display: flex; gap: 8px; }
  .mdlbox input { flex: 1; min-width: 0; background: var(--bg3); border: 1px solid var(--bd2); border-radius: 10px; padding: 10px; color: var(--txt); font: inherit; font-size: 12.5px; outline: none; }
  .mdlbox input:focus { border-color: var(--acc); }
  .mdlbox select { flex: 1; min-width: 0; background: var(--bg3); border: 1px solid var(--bd2); border-radius: 10px; padding: 10px; color: var(--txt); font: inherit; font-size: 12.5px; outline: none; }
  .mdlbox select option { background: #16161b; color: var(--txt2); }
  .mdlbox .hint { color: var(--mut); font-size: 11.5px; margin-top: 8px; }
  .cfgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 4px; }
  .cfgrid label { display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; color: var(--mut); }
  .cfgrid input { width: 100%; box-sizing: border-box; background: var(--bg3); border: 1px solid var(--bd2); border-radius: 8px; padding: 8px 10px; color: var(--txt); font: inherit; font-size: 13px; outline: none; }
  .cfgrid input:focus { border-color: var(--acc); }
  .mdlbox h4 span { color: var(--mut); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .mdlbox .close { margin-top: 14px; text-align: center; color: var(--mut); cursor: pointer; font-size: 12.5px; }
  .mdlbox .close:hover { color: var(--err); }

  main.grid {
    display: grid; grid-template-columns: 250px 1fr 1.35fr; gap: 12px;
    flex: 1; min-height: 0; margin: 12px 0;
  }
  @media (max-width: 1180px) {
    main.grid { grid-template-columns: 220px 1fr; }
    .center { display: none; }
  }
  @media (max-width: 860px) {
    main.grid { grid-template-columns: 1fr; }
    .left { display: none; }
  }

  .panel { border-radius: 12px; display: flex; flex-direction: column; min-height: 0; }
  .center { align-items: center; justify-content: center; padding: 20px; position: relative; }
  #reactorwrap { display: flex; flex-direction: column; align-items: center; justify-content: center; width: 100%; }
  .center.previewing #reactorwrap { display: none; }
  .stage { display: none; position: absolute; inset: 0; flex-direction: column; background: var(--bg2); border-radius: 12px; overflow: hidden; }
  .center.previewing .stage { display: flex; }
  .stagehd { display: flex; align-items: center; gap: 10px; padding: 7px 10px; border-bottom: 1px solid var(--bd); font-size: 11px; letter-spacing: 1.5px; color: var(--mut); font-family: Consolas, monospace; flex: none; }
  .stagehd b { color: var(--acc); }
  .stagehd #stagename { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: 0; }
  .stagex { background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 6px; cursor: pointer; font-size: 15px; line-height: 1; padding: 2px 9px; }
  .stagex:hover { border-color: var(--err); color: var(--err); }
  .stageframe { flex: 1; width: 100%; border: none; background: #0d0d11; }
  .right { min-height: 0; }

  .ptitle { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--bd); padding: 10px 12px; font-size: 10.5px; letter-spacing: 1.5px; color: var(--mut); font-family: Consolas, monospace; text-transform: uppercase; }

  /* left: chat history */
  .left .new { margin: 10px 12px; }
  #chatlist { flex: 1; overflow-y: auto; padding: 4px 8px; }
  .chat-item { display: flex; align-items: center; gap: 8px; padding: 9px 10px; border-radius: 8px; cursor: pointer; font-size: 13.5px; color: var(--txt2); }
  .chat-item:hover { background: var(--bg3); }
  .chat-item.active { background: var(--bg4); color: var(--txt); }
  .chat-item .tit { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-item .del { background: none; border: none; color: var(--mut); cursor: pointer; font-size: 14px; }
  .chat-item .del:hover { color: var(--err); }

  /* center: arc reactor */
  .arc-container { position: relative; width: 210px; height: 210px; display: flex; align-items: center; justify-content: center; }
  .arc-container { --ring: rgba(56,189,248,.45); --core1: #7dd3fc; --core2: rgba(14,165,233,.75); --glow1: #38bdf8; --glow2: #0ea5e9; }
  .arc-ring-outer { position: absolute; width: 100%; height: 100%; border-radius: 50%; border: 2px dashed var(--ring); animation: rotC 20s linear infinite; transition: border-color .3s; }
  .arc-ring-mid { position: absolute; width: 78%; height: 78%; border-radius: 50%; border: 2px solid transparent; border-top-color: var(--glow1); border-bottom-color: var(--glow2); animation: rotCC 8s linear infinite; transition: border-color .3s; }
  .arc-ring-inner { position: absolute; width: 58%; height: 58%; border-radius: 50%; border: 3px dotted var(--ring); animation: rotC 12s linear infinite; transition: border-color .3s; }
  .arc-core {
    position: absolute; width: 38%; height: 38%; border-radius: 50%;
    background: radial-gradient(circle, #f8fafc 0%, var(--core1) 40%, var(--core2) 70%, transparent 100%);
    box-shadow: 0 0 26px var(--glow1), 0 0 48px var(--glow2); transition: all .3s ease;
  }
  @keyframes rotC { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
  @keyframes rotCC { from { transform: rotate(360deg); } to { transform: rotate(0deg); } }

  .arc-container.thinking { --ring: rgba(251,191,36,.45); --core1: #fcd34d; --core2: rgba(245,158,11,.7); --glow1: #fbbf24; --glow2: #f59e0b; }
  .arc-container.tools { --ring: rgba(52,211,153,.45); --core1: #6ee7b7; --core2: rgba(16,185,129,.7); --glow1: #34d399; --glow2: #10b981; }
  .arc-container.planmode { --ring: rgba(167,139,250,.5); --core1: #c4b5fd; --core2: rgba(124,58,237,.75); --glow1: #a78bfa; --glow2: #7c3aed; }
  .arc-container.rainbow { animation: hueSpin 9s linear infinite; }
  @keyframes hueSpin { from { filter: hue-rotate(0deg) saturate(1.15); } to { filter: hue-rotate(360deg) saturate(1.15); } }

  .arc-container.thinking .arc-core { animation: pulseFast .3s infinite alternate; }
  .arc-container.tools .arc-core { animation: pulseGreen .4s infinite alternate; }
  .arc-container.thinking .arc-ring-outer { animation-duration: 6s; }
  .arc-container.tools .arc-ring-outer { animation-duration: 4s; }
  .arc-container.thinking .arc-ring-inner { animation-duration: 5s; }
  .arc-container.tools .arc-ring-inner { animation-duration: 3.5s; }
  @keyframes pulseFast { 0% { transform: scale(.95); opacity: .8; } 100% { transform: scale(1.1); opacity: 1; } }
  @keyframes pulseGreen { 0% { transform: scale(.92); box-shadow: 0 0 16px var(--glow1); } 100% { transform: scale(1.18); box-shadow: 0 0 42px var(--glow1), 0 0 66px var(--glow2); } }
  #waveform { display: flex; align-items: center; justify-content: center; gap: 6px; height: 40px; margin: 16px 0; }
  .wave-bar { width: 3px; height: 14px; border-radius: 2px; background-color: var(--acc); transition: background-color .3s; }
  body.thinking .wave-bar { background-color: var(--warn); }
  body.tools .wave-bar { background-color: var(--ok); }
  .active-wave .wave-bar { animation: waveAnim .8s infinite ease-in-out alternate; }
  .wave-bar:nth-child(2) { animation-delay: .1s; } .wave-bar:nth-child(3) { animation-delay: .2s; }
  .wave-bar:nth-child(4) { animation-delay: .3s; } .wave-bar:nth-child(5) { animation-delay: .4s; }
  .wave-bar:nth-child(6) { animation-delay: .5s; } .wave-bar:nth-child(7) { animation-delay: .6s; }
  .wave-bar:nth-child(8) { animation-delay: .7s; } .wave-bar:nth-child(9) { animation-delay: .8s; }
  .wave-bar:nth-child(10) { animation-delay: .9s; }
  @keyframes waveAnim { 0% { height: 6px; } 100% { height: 35px; } }
  #bonsai-state-label { font-size: 12px; letter-spacing: 3px; color: var(--txt2); text-align: center; font-family: Consolas, monospace; text-transform: uppercase; margin: 0; }
  .sub { font-size: 11px; color: var(--mut); margin: 6px 0 0; text-align: center; font-family: Consolas, monospace; }

  /* meters for metrics */
  .meterrow { padding: 10px 12px; }
  .meter { margin-bottom: 12px; }
  .meter:last-child { margin-bottom: 0; }
  .meter .lab { display: flex; justify-content: space-between; font-size: 12.5px; margin-bottom: 5px; color: var(--mut); font-family: Consolas, monospace; }
  .meter .lab b { color: var(--txt2); }
  .meter .bar { height: 6px; background: var(--bg3); border: 1px solid var(--bd); border-radius: 4px; overflow: hidden; }
  .meter .fill { height: 100%; border-radius: 4px; width: 0%; transition: width .5s; background: linear-gradient(90deg, var(--acc2), var(--acc)); }
  .fill.gold { background: linear-gradient(90deg, #d97706, var(--warn)); }

  /* right: chat console */
  .right .chat-wrap { flex: 1; min-height: 0; display: flex; flex-direction: column; padding: 10px; }
  #chat-container { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 4px 6px; }
  .msgrow { display: flex; gap: 10px; padding: 12px 0; }
  .msgrow.user { flex-direction: row-reverse; }
  .av { width: 30px; height: 30px; border-radius: 8px; flex-shrink: 0; display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 700; font-family: Consolas, monospace; }
  .av.bonsai { background: var(--bg3); border: 1px solid var(--bd2); color: var(--acc); }
  .av.me { background: var(--bg4); border: 1px solid var(--bd2); color: var(--txt2); }
  .bubble { max-width: 82%; padding: 10px 14px; border-radius: 12px; font-size: 15px; line-height: 1.6; overflow-wrap: anywhere; color: var(--txt); }
  .msgrow.user .bubble { background: var(--bg3); border: 1px solid var(--bd); }
  .msgrow.bonsai .bubble { background: var(--bg2); border: 1px solid var(--bd); }
  .bubble p { margin: 0 0 10px; } .bubble p:last-child { margin-bottom: 0; }
  .bubble code { background: var(--bg4); border-radius: 4px; padding: 1px 5px; font-family: Consolas, monospace; font-size: 13.5px; color: var(--acc); }
  .bubble pre { background: #0d0d11; border: 1px solid var(--bd); border-radius: 8px; padding: 12px; overflow-x: auto; font-size: 13.5px; color: var(--txt2); }
  .bubble a { color: var(--acc); }
  .abody table { border-collapse: collapse; margin: 8px 0; }
  .abody th, .abody td { border: 1px solid var(--bd); padding: 5px 9px; }
  .toolchip { display: inline-flex; align-items: center; gap: 6px; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 999px; padding: 4px 12px; margin: 4px 6px 4px 0; font-size: 12.5px; font-family: Consolas, monospace; }
  .statschip { display: inline-block; background: var(--bg3); border: 1px solid var(--bd2); color: var(--mut); border-radius: 6px; padding: 3px 10px; margin: 6px 0 2px; font-size: 11px; font-family: Consolas, monospace; }
  .toolchip.err { background: rgba(248,113,113,.1); border-color: rgba(248,113,113,.45); color: var(--err); }
  .think { color: var(--mut); font-style: italic; font-size: 13.5px; display: flex; align-items: center; gap: 8px; font-family: Consolas, monospace; }
  .dots { display: inline-flex; gap: 3px; } .dots i { width: 5px; height: 5px; border-radius: 50%; background: var(--mut); animation: bl 1.2s infinite; } .dots i:nth-child(2){animation-delay:.2s} .dots i:nth-child(3){animation-delay:.4s}
  @keyframes bl { 0%,60%,100%{opacity:.25} 30%{opacity:1} }
  .caret::after { content: "\\258C"; color: var(--acc); margin-left: 2px; animation: blink 1s steps(1) infinite; }
  @keyframes blink { 50% { opacity: 0; } }
  .imggrid { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .imggrid img { width: 90px; height: 90px; object-fit: cover; border-radius: 10px; border: 1px solid var(--bd); }
  .filechip { display: inline-flex; align-items: center; gap: 6px; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 8px; padding: 5px 10px; margin: 2px 6px 2px 0; font-size: 12.5px; font-family: Consolas, monospace; }
  .filechip .fname { font-weight: 700; }

  .composer { display: flex; align-items: flex-end; gap: 8px; padding: 8px 0 2px; }
  .field {
    flex: 1; position: relative; display: flex; align-items: flex-end; gap: 6px;
    background: var(--bg3); border: 1px solid var(--bd2); border-radius: 12px; padding: 8px; min-height: 52px;
  }
  .field:focus-within { border-color: var(--acc); }
  #filein { display: none; }
  #user-input {
    flex: 1; background: transparent; border: none; outline: none; resize: none; color: var(--txt);
    font: inherit; font-size: 14.5px; padding: 8px 6px; max-height: 160px; min-width: 0;
  }
  #user-input::placeholder { color: var(--mut); }
  .iconbtn { background: none; border: none; cursor: pointer; font-size: 16px; padding: 8px; border-radius: 8px; color: var(--mut); }
  .iconbtn:hover { background: var(--bg4); color: var(--txt2); }
  #send {
    background: var(--acc); color: #04212e; border: none; border-radius: 12px;
    padding: 13px 16px; cursor: pointer; font-size: 14px; font-weight: 700; font-family: Consolas, monospace; transition: all .15s;
  }
  #send:hover { background: #7dd3fc; }
  #send:disabled { opacity: .4; cursor: default; }
  #send.stop { background: var(--err); color: #fff; }
  #queue {
    background: transparent; border: 1px solid var(--bd2); color: var(--mut); border-radius: 12px;
    padding: 13px 12px; cursor: pointer; font-size: 12.5px; font-weight: 700; font-family: Consolas, monospace; transition: all .15s; white-space: nowrap;
  }
  #queue:hover { background: var(--bg3); color: var(--txt2); }
  #queue.hasq { background: var(--bg4); border-color: var(--acc); color: var(--acc); }

  #preview { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
  .thumb { position: relative; }
  .thumb img { width: 62px; height: 62px; object-fit: cover; border-radius: 8px; border: 1px solid var(--bd); }
  .thumb .x { position: absolute; top: -6px; right: -6px; background: var(--err); color: #fff; border: none; border-radius: 50%; width: 20px; height: 20px; cursor: pointer; font-size: 12px; line-height: 1; }
  .pf { display: inline-flex; align-items: center; gap: 6px; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 8px; padding: 5px 10px; font-size: 12.5px; font-family: Consolas, monospace; }
  .pf .x { background: none; border: none; color: var(--mut); cursor: pointer; font-size: 13px; }
  .pf .x:hover { color: var(--err); }

  /* reason / thinking box */
  .reasonbox { border: 1px solid rgba(167,139,250,.3); border-radius: 10px; background: rgba(167,139,250,.06); margin-bottom: 8px; overflow: hidden; }
  .reasonbox summary { cursor: pointer; padding: 6px 10px; font-size: 12.5px; font-family: Consolas, monospace; color: var(--violet); list-style: none; display: flex; align-items: center; gap: 6px; user-select: none; }
  .reasonbox summary::-webkit-details-marker { display: none; }
  .reasonbox summary::before { content: '\\25B8'; transition: transform .15s; }
  .reasonbox[open] summary::before { transform: rotate(90deg); }
  .reasonbox .rc { padding: 2px 10px 8px; max-height: 220px; overflow-y: auto; font-size: 12.5px; line-height: 1.55; color: #c4b5fd; white-space: pre-wrap; font-family: Consolas, monospace; }
  .reasonbox.live summary::after { content: '\\25CF'; color: var(--violet); animation: bl 1.2s infinite; margin-left: 4px; }
  .reasonbox.errtitle summary { color: var(--err); }

  /* tool log */
  .toollog { border: 1px solid var(--bd); border-radius: 10px; background: var(--bg3); margin-bottom: 8px; overflow: hidden; }
  .toollog summary { cursor: pointer; padding: 6px 10px; font-size: 12.5px; font-family: Consolas, monospace; color: var(--txt2); list-style: none; display: flex; align-items: center; gap: 6px; user-select: none; }
  .toollog summary::-webkit-details-marker { display: none; }
  .toollog summary::before { content: '\\25B8'; transition: transform .15s; }
  .toollog[open] summary::before { transform: rotate(90deg); }
  .toollog .tl { padding: 2px 10px 8px; max-height: 260px; overflow-y: auto; display: flex; flex-direction: column; gap: 6px; }
  .tlitem { border: 1px solid var(--bd); border-radius: 8px; background: var(--bg2); padding: 6px 8px; font-size: 12.5px; font-family: Consolas, monospace; }
  .tlitem .nm { color: var(--acc); font-weight: 700; }
  .tlitem .rs { color: var(--txt2); white-space: pre-wrap; margin-top: 4px; }
  .tlitem .ar { color: var(--mut); white-space: pre-wrap; margin-top: 2px; }
  .tlitem.err { border-color: rgba(248,113,113,.4); } .tlitem.err .nm { color: var(--err); }

  /* mode toggle + workdir */
  .modebtn { background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 12px; padding: 13px 12px; cursor: pointer; font-size: 12.5px; font-family: Consolas, monospace; font-weight: 700; letter-spacing: 1px; transition: all .15s; white-space: nowrap; }
  .modebtn:hover { background: var(--bg4); color: var(--txt); }
  .modebtn.plan { border-color: var(--acc); color: var(--acc); }
  .wbtn { background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); padding: 7px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; font-family: Consolas, monospace; max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .wbtn:hover { background: var(--bg4); color: var(--txt); }
  .blstatus { display: inline-flex; align-items: center; gap: 6px; color: var(--mut); font-size: 12px; font-family: Consolas, monospace; padding: 7px 6px; letter-spacing: .5px; white-space: nowrap; }
  .blicon { width: 15px; height: 15px; flex: none; }
  .blstatus.on .blicon { color: #ea7600; }
  .blstatus.mid .blicon { color: var(--warn); }
  .blstatus.off .blicon { color: var(--mut); }
  .bldot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--mut); }
  .bldot.on { background: var(--ok); }
  .bldot.mid { background: var(--warn); }
  .bldot.off { background: var(--err); }
  .blstatus.on { color: var(--txt2); }
  .blstatus.mid { color: var(--warn); }
  .blstatus.off { color: var(--mut); }

  footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; padding: 6px 14px; border-radius: 12px; font-size: 12.5px; color: var(--mut); font-family: Consolas, monospace; letter-spacing: 1px; }
  footer b { color: var(--txt2); }
  .fine { text-align: center; color: var(--mut); font-size: 12px; margin-top: 6px; font-family: Consolas, monospace; }
  .statsline { min-height: 16px; padding: 2px 4px 0; font-size: 11px; color: var(--txt2); font-family: Consolas, monospace; letter-spacing: 0; opacity: .85; }
  .statsline b { color: var(--acc); font-weight: 600; }
  .statsline .sdim { color: var(--mut); }

  /* thinking effort selector */
  #effort-sel {
    background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2);
    border-radius: 12px; padding: 13px 8px; cursor: pointer; font-size: 12px; font-family: Consolas, monospace;
    font-weight: 700; outline: none; transition: all .15s;
  }
  #effort-sel option { background: #16161b; color: var(--txt2); }
  #effort-sel:hover { border-color: var(--acc); }

  /* todo panel */
  #todopanel { display: none; padding: 4px 8px; }
  #todopanel.on { display: block; }
  #todopanel .todo { display: flex; align-items: flex-start; gap: 8px; padding: 5px 6px; border-radius: 6px; font-size: 13px; font-family: Consolas, monospace; }
  #todopanel .todo .st { width: 14px; flex: 0 0 14px; }
  #todopanel .todo.pending { color: var(--mut); }
  #todopanel .todo.in_progress { color: var(--warn); }
  #todopanel .todo.completed { color: var(--ok); text-decoration: line-through; opacity: .75; }

  /* ask_user dialog */
  .askov { position: fixed; inset: 0; background: rgba(5,5,8,.75); backdrop-filter: blur(3px); display: flex; align-items: center; justify-content: center; z-index: 60; }
  .askbox { width: min(540px, 92vw); background: var(--bg2); border: 1px solid var(--bd2); border-radius: 14px; padding: 18px; font-family: Consolas, monospace; }
  .askbox h4 { margin: 0 0 10px; color: var(--acc); font-size: 14px; letter-spacing: 1px; }
  .askbox .aq { color: var(--txt); font-size: 15px; line-height: 1.5; white-space: pre-wrap; }
  .askbox .opts { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 14px; }
  .askbox .opt { background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 10px; padding: 8px 14px; cursor: pointer; font-size: 13px; font-family: Consolas, monospace; }
  .askbox .opt:hover { background: var(--bg4); border-color: var(--acc); color: var(--txt); }
  .askbox .afree { display: flex; gap: 8px; margin-top: 14px; }
  .askbox .afree input { flex: 1; min-width: 0; background: var(--bg3); border: 1px solid var(--bd2); border-radius: 10px; padding: 10px; color: var(--txt); font-size: 14px; font-family: Consolas, monospace; outline: none; }
  .askbox .afree input:focus { border-color: var(--acc); }
  .askbox .afree button { background: var(--acc); color: #04212e; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 700; cursor: pointer; font-size: 14px; font-family: Consolas, monospace; }

  /* screenshot thumb in tool log */
  .tlitem .tthumb { max-width: 220px; border-radius: 6px; border: 1px solid var(--bd); margin-top: 6px; display: block; }
  .shotcard { margin-top: 8px; border: 1px solid var(--bd); border-radius: 10px; overflow: hidden; background: var(--bg2); }
  .shotcard .shothd { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 8px; font-size: 11px; color: var(--mut); border-bottom: 1px solid var(--bd); }
  .shotcard .shothd b { color: var(--txt); font-weight: 600; }
  .shotcard .shotimg { display: block; width: 100%; height: auto; cursor: zoom-in; background: var(--bg); }
  .shotcard .shotpath { padding: 4px 8px; font-size: 10.5px; color: var(--mut); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .shotcard { margin-top: 8px; border: 1px solid var(--bd2); border-radius: 8px; overflow: hidden; background: var(--bg2); }
  .shotcard .shothd { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 8px; font-size: 11px; color: var(--mut); border-bottom: 1px solid var(--bd); font-family: Consolas, monospace; }
  .shotcard .shothd b { color: var(--txt2); font-weight: 600; }
  .shotcard .shotimg { display: block; width: 100%; height: auto; cursor: zoom-in; background: #0d0d11; }
  .shotcard .shotpath { padding: 4px 8px; font-size: 10.5px; color: var(--mut); font-family: Consolas, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tlprev { margin-top: 8px; }
  .tlprev a { font-size: 11px; color: var(--acc); }
  .tlprev .tlprevbtn { margin-left: 8px; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); border-radius: 6px; padding: 2px 8px; font-size: 11px; cursor: pointer; font-family: Consolas, monospace; }
  .tlprev .tlprevbtn:hover { border-color: var(--acc); color: var(--acc); }
  .pvbox { position: fixed; inset: 0; z-index: 95; display: none; flex-direction: column; background: var(--bg2); }
  .pvbox.on { display: flex; }
  .pvboxhd { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--bd); font-size: 11px; letter-spacing: 1.5px; color: var(--mut); flex: none; }
  .pvboxhd b { color: var(--txt); }
  .pvboxhd span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: 0; }
  .pvboxx { background: transparent; border: 1px solid var(--bd); color: var(--txt); border-radius: 6px; cursor: pointer; font-size: 16px; line-height: 1; padding: 2px 10px; }
  .pvboxx:hover { border-color: var(--err); color: var(--err); }
  .pvboxframe { flex: 1; width: 100%; border: none; background: var(--bg); }
  .msgrow.user.queued .bubble { border-color: var(--warn); opacity: .92; }
  .qbadge { display: inline-block; font-size: 9.5px; letter-spacing: 1.1px; color: var(--warn); border: 1px dashed var(--warn); border-radius: 999px; padding: 2px 9px; margin-bottom: 6px; animation: qpulse 1.6s ease-in-out infinite; }
  @keyframes qpulse { 0%, 100% { opacity: .55; } 50% { opacity: 1; } }
  .scopebtn { width: 100%; text-align: left; font-family: Consolas, monospace; font-size: 11px; letter-spacing: 1px; color: var(--acc); background: var(--bg3); border: 1px solid var(--bd2); border-radius: 6px; padding: 6px 8px; cursor: pointer; }
  .scopebtn:hover { border-color: var(--acc); }
  .scopebtn.sc-system { color: var(--err); border-color: var(--err); }
  .scopebtn.sc-workspace { color: var(--ok); border-color: var(--ok); }
  .scopelist { display: flex; flex-direction: column; gap: 3px; margin-top: 5px; }
  .scoperow { display: flex; align-items: center; gap: 4px; font-size: 10.5px; font-family: Consolas, monospace; background: var(--bg); border: 1px solid var(--bd); border-left-width: 2px; border-radius: 5px; padding: 3px 4px 3px 6px; }
  .scoperow.allow { border-left-color: var(--ok); }
  .scoperow.deny { border-left-color: var(--err); }
  .scoperow .sp { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; color: var(--txt2); }
  .scoperow.allow .sp { color: var(--ok); }
  .scoperow.deny .sp { color: var(--err); }
  .scoperow .sx { background: transparent; border: none; color: var(--mut); cursor: pointer; font-size: 13px; line-height: 1; padding: 0 3px; }
  .scoperow .sx:hover { color: var(--err); }
  .scopeclear { margin-top: 5px; width: 100%; background: transparent; border: 1px dashed var(--bd2); color: var(--mut); border-radius: 6px; font-size: 10px; letter-spacing: 1px; padding: 4px; cursor: pointer; }
  .scopeclear:hover { color: var(--err); border-color: var(--err); }
  .askpath { font-family: Consolas, monospace; font-size: 12px; color: var(--acc); background: var(--bg); border: 1px solid var(--bd2); border-radius: 6px; padding: 7px 9px; margin: 6px 0 5px; word-break: break-all; }
  .asktool { font-size: 10.5px; color: var(--mut); margin-bottom: 4px; }
  .opt.allow { border-color: var(--ok); color: var(--ok); }
  .opt.allow:hover { background: var(--ok); color: #06210f; }
  .opt.deny { border-color: var(--err); color: var(--err); }
  .opt.deny:hover { background: var(--err); color: #2b0710; }
  .shots { margin: 6px 0 10px; display: flex; flex-direction: column; gap: 8px; }
  #toasts { position: fixed; right: 14px; bottom: 122px; z-index: 90; display: flex; flex-direction: column; gap: 8px; max-width: 380px; pointer-events: none; }
  .toast { position: relative; background: var(--bg2); border: 1px solid var(--acc); border-left-width: 3px; border-radius: 8px; padding: 9px 30px 10px 11px; box-shadow: 0 6px 22px rgba(0,0,0,.55); animation: toastin .22s ease-out; pointer-events: auto; }
  @keyframes toastin { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .toasthd { font-size: 10px; letter-spacing: 1.2px; color: var(--acc); margin-bottom: 5px; }
  .toastx { position: absolute; top: 5px; right: 6px; background: transparent; border: none; color: var(--mut); font-size: 15px; line-height: 1; cursor: pointer; }
  .toastx:hover { color: var(--err); }
  .toastcmd { font-family: Consolas, monospace; font-size: 11px; color: var(--txt2); word-break: break-all; margin-bottom: 5px; }
  .toastout { font-family: Consolas, monospace; font-size: 11px; color: var(--txt); background: var(--bg); border: 1px solid var(--bd); border-radius: 5px; padding: 5px 7px; margin: 0; max-height: 130px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
  .toastwhen { font-size: 10px; color: var(--mut); margin-top: 5px; }
  .tlprev .tlframe { width: 100%; height: 280px; border: 1px dashed var(--bd2); border-radius: 8px; background: #0d0d11; margin-top: 6px; }

  /* drag & drop attach overlay */
  .chat-wrap { position: relative; }
  .dropov { position: absolute; inset: 0; z-index: 70; display: none; align-items: center; justify-content: center;
    background: rgba(56,189,248,.08); border: 2px dashed var(--acc); border-radius: 14px;
    font-family: Consolas, monospace; color: var(--acc); font-size: 15px; letter-spacing: 1px;
    pointer-events: none; }
  .dropov.on { display: flex; }
  .dropov b { color: var(--txt); }
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
      <span><span class="dot idle" id="sdot"></span>STATUS: <b id="system-status-text" title="The model loads the first time you send a message.">ONLINE / IDLE</b></span>
      <span>MODEL: <b id="modelbadge">Bonsai 2 &middot; 27B</b></span>
      <span>VISION: <b id="visionbadge">mmproj ON</b></span>
    </div>
    <div class="hstatus" style="gap:10px; align-items:center;">
      <div class="hclock">
        <div class="t" id="clock-time">00:00:00</div>
        <div class="d" id="clock-date"></div>
      </div>
      <button class="wbtn" id="workbtn" title="Click to choose the workspace folder">\\WORKSPACE</button>
      <select class="hbtn modelsel" id="modelsel" title="Active AI model - pick a local .gguf or an external OpenAI-compatible endpoint"></select>
      <button class="hbtn add" id="addmodelbtn" title="Add a model (local .gguf file or an external API endpoint)">+</button>
      <button class="hbtn add" id="cfgbtn" title="Model settings - context size, temperature, GPU layers">&#9881;</button>
      <button class="hbtn" id="ttsbtn" title="Text-to-speech (Piper) - speak replies aloud. Off by default.">TTS: OFF</button>
      <button class="hbtn" id="ejectbtn" title="Stop the model server now and free its RAM/VRAM (it restarts automatically on the next message)">EJECT</button>
      <span class="blstatus off" id="blstatus" title="Blender MCP status" aria-label="Blender MCP status"><svg class="blicon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 1.6C7.4 1.6 3.7 3.9 3.7 6.9c0 1.6 1.1 3 2.8 3.9-2.1.9-3.5 2.4-3.5 4.2 0 3.2 4 5.8 9 5.8 2.4 0 4.6-.7 6.2-1.8l3.4 2.8 1.7-2-3.3-2.7c.6-.9.9-1.9.9-3 0-1.9-1-3.6-2.6-4.9.3-.5.4-1.1.4-1.7 0-3-3.7-5.3-8.3-5.3Z"/><ellipse cx="12" cy="6.9" rx="4.2" ry="2.5" fill="#0a0a0c"/></svg><span class="bldot off" id="bldot"></span></span>
    </div>
  </header>

  <div class="mdl" id="addmdl">
    <div class="mdlbox">
      <h4>ADD A MODEL</h4>
      <button class="act" id="mdl-file">Model file (.gguf) &mdash; browse&hellip;<small>Pick a single GGUF model from anywhere on this PC.</small></button>
      <button class="act" id="mdl-folder">Model folder &mdash; browse&hellip;<small>Scan a folder (and its subfolders) and add every .gguf it finds.</small></button>
      <div class="mdlsep">or connect to an API server</div>
      <div class="row">
        <input id="apibase" placeholder="base URL, e.g. http://127.0.0.1:1234/v1">
        <input id="apimodel" placeholder="model id, e.g. qwen3-8b">
        <button class="act inline" id="mdl-api">Add</button>
      </div>
      <div class="mdlsep">vision projector (optional - enables screenshots &amp; images)</div>
      <div class="row">
        <select id="mmprojfor"></select>
        <button class="act inline" id="mdl-mmproj">Browse&hellip;</button>
        <button class="act inline" id="mdl-mmproj-clear">Clear</button>
      </div>
      <div class="hint" id="mmprojhint"></div>
      <div class="close" id="addmdl-close">Cancel</div>
    </div>
  </div>

  <div class="mdl" id="cfgmdl">
    <div class="mdlbox">
      <h4>MODEL SETTINGS <span id="cfgwho"></span></h4>
      <div class="cfgrid">
        <label>Context size (ctx)<input id="cfg_ctx" type="number" min="512" max="1048576" step="512"></label>
        <label>Temperature<input id="cfg_temp" type="number" min="0" max="2" step="0.05"></label>
        <label>Top-p<input id="cfg_top_p" type="number" min="0.01" max="1" step="0.01"></label>
        <label>Top-k<input id="cfg_top_k" type="number" min="0" max="1000" step="1"></label>
        <label>GPU layers (-ngl)<input id="cfg_ngl" type="number" min="-1" max="999" step="1"></label>
      </div>
      <div class="hint" id="cfghint"></div>
      <div class="row">
        <button class="act inline" id="cfg-save">Save</button>
        <button class="act inline" id="cfg-defaults">Defaults</button>
        <button class="act inline" id="cfg-close">Close</button>
      </div>
    </div>
  </div>

  <main class="grid">
    <section class="left panel hud-border">
      <div class="ptitle"><span>CHAT HISTORY</span><span>LOCAL</span></div>
      <button class="hbtn new" id="newchat2">+ NEW CHAT</button>
      <div id="chatlist"></div>
      <div class="ptitle" style="margin-top:8px; border-top:1px solid #164e63; padding-top:8px;"><span>TODO</span><span id="todocount"></span></div>
      <div id="todopanel"></div>
      <div class="ptitle" style="margin-top:8px; border-top:1px solid #164e63; padding-top:8px;"><span>PATH SCOPE</span></div>
      <button class="scopebtn" id="scopebtn" title="How far Bonsai may reach outside the workspace. Click to switch: WORKSPACE (hard sandbox) / ASK (ask me every time) / SYSTEM (no prompts).">SCOPE: ...</button>
      <div class="scopelist" id="scopelist"></div>
      <button class="scopeclear" id="scopeclear" title="Forget every approved and denied path">CLEAR APPROVALS</button>
    </section>

    <section class="center panel hud-border" id="centerpanel">
      <div class="stage" id="previewstage">
        <div class="stagehd">
          <b>LIVE PREVIEW</b>
          <span id="stagename"></span>
          <button class="stagex" id="stageclose" title="Close the preview and bring back the reactor">&times;</button>
        </div>
        <iframe id="stageframe" class="stageframe" title="HTML preview" sandbox="allow-scripts"></iframe>
      </div>
      <div id="reactorwrap">
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
      <p id="bonsai-sub" class="sub">Awaiting your command</p>
      <div class="meterrow" style="width:100%; margin-top:8px;">
        <div class="meter"><div class="lab"><span>CPU</span><b id="cpu-val">0%</b></div><div class="bar"><div class="fill" id="cpu-bar"></div></div></div>
        <div class="meter"><div class="lab"><span>RAM</span><b id="ram-val">0%</b></div><div class="bar"><div class="fill" id="ram-bar"></div></div></div>
        <div class="meter"><div class="lab"><span>GPU</span><b id="gpu-val">0%</b></div><div class="bar"><div class="fill gold" id="gpu-bar"></div></div></div>
      </div>
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
            <button type="button" class="modebtn" id="modebtn" title="Switch Plan / Build mode - Plan is read-only (no tools run)">BUILD</button>
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
    <div>MODEL: <b id="footer-model">BONSAI 2 27B</b> &middot; <span id="footer-caps">VISION+TOOLS</span></div>
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

function load() { try { const v = JSON.parse(localStorage.getItem('jarvis_chats') || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
const IMG_KEEP = 200 * 1024;
function stripHeavyImages(m) {
  if (!Array.isArray(m.content)) return;
  const keep = [];
  let dropped = 0;
  m.content.forEach(function (p) {
    if (p.type === 'image_url' && p.image_url && (p.image_url.url || '').length > IMG_KEEP) { dropped++; return; }
    keep.push(p);
  });
  if (dropped) m.content = keep.length ? keep : '[large image removed from history]';
}
function buildPersist(keepRecentImages) {
  let ids = null;
  if (keepRecentImages) {
    ids = {};
    chats.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); })
      .slice(0, 5).forEach(function (c) { ids[c.id] = 1; });
  }
  return chats.slice(-200).map(function (ch) {
    const copy = JSON.parse(JSON.stringify(ch));
    (copy.messages || []).forEach(function (m) {
      if (m.calls) (m.calls).forEach(function (cl) { if (cl.preview) delete cl.preview; });
      if (!ids || !ids[ch.id]) stripHeavyImages(m);
    });
    return copy;
  });
}
function save() {
  try { localStorage.setItem('jarvis_chats', JSON.stringify(buildPersist(false))); } catch (e) {}
  try {
    fetch('/api/chats', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chats: buildPersist(true) }) }).catch(function () {});
  } catch (e) {}
}
async function serverLoad() {
  try {
    const r = await fetch('/api/chats');
    const j = await r.json();
    if (j && Array.isArray(j.chats) && j.chats.length) {
      const byId = {};
      chats.forEach(function (c, i) { byId[c.id] = i; });
      let changed = false;
      j.chats.forEach(function (c) {
        if (!c || !c.id) return;
        const i = byId[c.id];
        if (i === undefined) { byId[c.id] = chats.length; chats.push(c); changed = true; return; }
        const loc = chats[i];
        if ((c.messages || []).length > (loc.messages || []).length) { chats[i] = c; changed = true; }
        else if ((c.ts || 0) > (loc.ts || 0)) { loc.ts = c.ts; changed = true; }
      });
      const before = chats.length;
      dedupeChats();
      if (chats.length !== before) changed = true;
      if (cur) {
        const keep = chats.filter(function (c) { return c.id === cur.id; })[0];
        if (keep) cur = keep;
      }
      if (changed) save();
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
  postPolicy({ clear: true });
}
function init() {
  bindModeBtn();
  loadWorkdir();
  dedupeChats();
  if (!chats.length) newChat();
  else cur = chats[chats.length - 1];
  renderAll();
  setSendUI();
  serverLoad();
  startSchedWatch();
  bindScope();
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
    if (typeof setBonsaiState === 'function') setBonsaiState(bonsaiState || 'idle');
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
function dedupeChats() {
  const byId = {};
  const out = [];
  (chats || []).forEach(function (c) {
    if (!c || !c.id) return;
    const prev = byId[c.id];
    if (!prev) { byId[c.id] = c; out.push(c); return; }
    const a = (c.messages || []).length, b = (prev.messages || []).length;
    if (a > b || (a === b && (c.ts || 0) > (prev.ts || 0))) {
      const i = out.indexOf(prev);
      if (i !== -1) out[i] = c;
      byId[c.id] = c;
    }
  });
  chats = out;
}
function renderList() {
  const el = document.getElementById('chatlist');
  el.innerHTML = '';
  let marked = false;
  chats.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).forEach(function (c) {
    const row = document.createElement('div');
    const isActive = !marked && cur && c.id === cur.id;
    if (isActive) marked = true;
    row.className = 'chat-item' + (isActive ? ' active' : '');
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
      if (m.role === 'user') addUser(m.content, !!m.queued);
      else if (m.role === 'assistant') addAsst(m.content, m.calls || [], m.reason, m.stats);
    });
  }
  busy = false;
  requeuePending();
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
function addUser(content, queued) {
  const conv = convEl();
  const row = document.createElement('div'); row.className = 'msgrow user' + (queued ? ' queued' : '');
  const av = document.createElement('div'); av.className = 'av me'; av.textContent = 'U';
  const b = document.createElement('div'); b.className = 'bubble';
  if (queued) {
    const qb = document.createElement('div'); qb.className = 'qbadge';
    qb.setAttribute('data-state', 'queued');
    qb.textContent = 'QUEUED \u00b7 waiting for the current reply';
    qb.title = 'This message is in line - it will be sent automatically.';
    b.appendChild(qb);
  }
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
  addToolChips(b, calls);
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
    const pv = previewIframeDom(c.result);
    if (pv) it.appendChild(pv);
    l.appendChild(it);
  });
  d.appendChild(s); d.appendChild(l);
  container.appendChild(d);
  const shots = document.createElement('div'); shots.className = 'shots';
  let shotCount = 0;
  calls.forEach(function (c) {
    if (!(c.preview || c.image_data)) return;
    const sc = shotCardDom(c);
    if (sc) { shots.appendChild(sc); shotCount++; }
  });
  if (shotCount) container.insertBefore(shots, d);
}
function openPreviewStage(url, name) {
  if (!url) return false;
  const panel = document.getElementById('centerpanel');
  const frame = document.getElementById('stageframe');
  if (panel && frame) {
    frame.src = url;
    const nm = document.getElementById('stagename');
    if (nm) nm.textContent = name || '';
    panel.classList.add('previewing');
    return true;
  }
  const box = document.getElementById('pvbox');
  const lf = document.getElementById('pvframe');
  if (box && lf) {
    lf.src = url;
    const ln = document.getElementById('pvname');
    if (ln) ln.textContent = name || '';
    box.classList.add('on');
    return true;
  }
  window.open(url, '_blank');
  return false;
}
function closePreviewStage() {
  const panel = document.getElementById('centerpanel');
  const frame = document.getElementById('stageframe');
  if (panel && frame) {
    frame.src = 'about:blank';
    panel.classList.remove('previewing');
  }
  const box = document.getElementById('pvbox');
  const lf = document.getElementById('pvframe');
  if (box && lf) {
    lf.src = 'about:blank';
    box.classList.remove('on');
  }
}
const _stageClose = document.getElementById('stageclose');
if (_stageClose) _stageClose.onclick = closePreviewStage;
const _pvClose = document.getElementById('pvclose');
if (_pvClose) _pvClose.onclick = closePreviewStage;
function previewIframeDom(r) {
  const url = r && typeof r === 'object' ? (r.preview_url || '') : '';
  if (!url) return null;
  const w = document.createElement('div');
  w.className = 'tlprev';
  const a = document.createElement('a');
  a.href = url; a.target = '_blank'; a.rel = 'noreferrer';
  a.textContent = 'preview shown in the center stage \u2014 click to open in a new tab';
  a.onclick = function () { openPreviewStage(url, r && (r.path || '')); };
  const b2 = document.createElement('button');
  b2.className = 'tlprevbtn';
  b2.textContent = 'Show';
  b2.title = 'Show it in the center stage';
  b2.onclick = function () { openPreviewStage(url, r && (r.path || '')); };
  w.appendChild(a); w.appendChild(b2);
  return w;
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
  return chip;
}
function addToolChips(container, calls) {
  let prevName = null, count = 0, chipEl = null;
  (calls || []).forEach(function (c) {
    if (c.name === prevName) {
      count += 1;
      chipEl.textContent = '\u2699\ufe0f ' + prevName + ' \u00d7' + count;
    } else {
      chipEl = toolChip(container, c);
      prevName = c.name;
      count = 1;
    }
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
  if (busy) { queueNow(); return; }
  go();
}
function setSendUI() {
  const b = document.getElementById('send');
  if (busy) { b.textContent = msgQueue.length ? ('STOP \u00b7 ' + msgQueue.length + ' queued') : 'STOP'; b.disabled = false; b.classList.add('stop'); }
  else { b.textContent = 'SEND'; b.classList.remove('stop'); }
}
function refreshQueueUI() {
  setSendUI();
}
function clearQueuedBadge(chat, msg) {
  if (!chat || !msg) return;
  if (cur && chat.id !== cur.id) return;
  const all = chat.messages || [];
  const at = all.indexOf(msg);
  if (at < 0) return;
  let k = -1;
  for (let i = 0; i <= at; i++) if (all[i].role === 'user') k++;
  const row = document.querySelectorAll('.msgrow.user')[k];
  if (!row) return;
  row.classList.remove('queued');
  const b = row.querySelector('.qbadge');
  if (b) b.remove();
}
function queueNow() {
  const parts = buildUserMsg();
  if (!parts) return;
  const chat = cur || newChat();
  const raw = parts.length === 1 && parts[0].type === 'text' ? parts[0].text : parts;
  const msg = { role: 'user', content: raw, queued: true };
  chat.messages.push(msg);
  msgQueue.push({ parts: parts, chat: chat, msg: msg });
  addUser(parts, true);
  pendingAtt = [];
  renderPreview();
  document.getElementById('user-input').value = '';
  save();
  refreshQueueUI();
  scrollBottom();
}
function requeuePending() {
  msgQueue = msgQueue.filter(function (q) { return q.chat && q.msg && q.msg.queued; });
  if (!cur) return;
  (cur.messages || []).forEach(function (m) {
    if (m.role !== 'user' || !m.queued) return;
    if (msgQueue.some(function (q) { return q.msg === m; })) return;
    const parts = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content || []);
    if (parts.length) msgQueue.push({ parts: parts, chat: cur, msg: m });
  });
  refreshQueueUI();
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
  if (next.msg) delete next.msg.queued;
  clearQueuedBadge(next.chat, next.msg);
  refreshQueueUI();
  go(next.parts, next.chat, true, next.msg);
}


async function go(forcedParts, chat, alreadyAdded, askedMsg) {
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
  let asked = alreadyAdded ? (askedMsg || null) : null;
  if (!asked) { asked = { role: 'user', content: raw }; target.messages.push(asked); addUser(parts); }

  addThinking();
  setBonsaiState('thinking');
  pendingAtt = [];
  renderPreview();
  document.getElementById('user-input').value = '';
  try { await streamRun(target.messages.filter(function (m) { return !m.queued; }).slice(), target, asked); }
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
  Object.keys(r).forEach(function (k) { if (k !== 'preview' && k !== 'preview_url') copy[k] = r[k]; });
  return JSON.stringify(copy, null, 2);
}
function shotCardDom(call) {
  if (!call) return null;
  const src = call.preview || call.image_data || '';
  if (!src) return null;
  const r = call.result || {};
  const saved = r.saved || r.path || '';
  const box = document.createElement('div'); box.className = 'shotcard';
  const head = document.createElement('div'); head.className = 'shothd';
  const name = document.createElement('b');
  name.textContent = '\\ud83d\\udcbe ' + (r.matched ? 'window: ' + r.matched : 'screenshot');
  head.appendChild(name);
  if (r.width && r.height) {
    const d = document.createElement('span');
    d.textContent = r.width + '\u00d7' + r.height;
    head.appendChild(d);
  }
  box.appendChild(head);
  const im = document.createElement('img'); im.className = 'shotimg';
  im.src = src; im.alt = 'screenshot captured by Bonsai';
  im.title = 'Click to open full size';
  im.onclick = function () { window.open(src, '_blank'); };
  box.appendChild(im);
  if (saved) {
    const p = document.createElement('div'); p.className = 'shotpath';
    p.textContent = saved;
    box.appendChild(p);
  }
  return box;
}
function toolItemDom(call) {
  const it = document.createElement('div');
  it.className = 'tlitem' + (call.result && call.result.error ? ' err' : '');
  const nm = document.createElement('div'); nm.className = 'nm';
  nm.textContent = '\u2699 ' + call.name + ' ' + JSON.stringify(call.arguments || {});
  const rs = document.createElement('div'); rs.className = 'rs';
  rs.textContent = toolResultText(call.result);
  it.appendChild(nm); it.appendChild(rs);
  const pv = previewIframeDom(call.result);
  if (pv) it.appendChild(pv);
  return it;
}
function onTool(call) {
  if (thinkingRow && thinkingRow.body) thinkingRow.body.classList.remove('caret');
  const prev = thinkingRow.lastChip;
  if (prev && prev.__name === call.name) {
    prev.__count += 1;
    prev.textContent = '\u2699\ufe0f ' + call.name + ' \u00d7' + prev.__count;
  } else {
    const chip = toolChip(thinkingRow.b, call);
    chip.__name = call.name;
    chip.__count = 1;
    thinkingRow.lastChip = chip;
  }
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
  const live = shotCardDom(call);
  if (live) thinkingRow.b.insertBefore(live, thinkingRow.toolEl);
  if (call.result && call.result.preview_url) {
    openPreviewStage(call.result.preview_url, call.result.path || '');
  }
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

const SCOPES = ['workspace', 'ask', 'system'];
function postPolicy(body) {
  return fetch('/api/path_policy', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) }).then(function (r) { return r.json(); })
    .then(function (j) { renderPathPolicy(j); return j; }).catch(function () {});
}
function scopeRow(p, label, cls) {
  const row = document.createElement('div'); row.className = 'scoperow ' + cls;
  const t = document.createElement('span'); t.className = 'sp'; t.textContent = p; t.title = label;
  const x = document.createElement('button'); x.className = 'sx'; x.textContent = '\u00d7';
  x.title = 'revoke this ' + label.replace('d', '') + ' - Bonsai will ask again';
  x.onclick = function () { postPolicy({ path: p }); };
  row.appendChild(t); row.appendChild(x);
  return row;
}
function renderPathPolicy(j) {
  const btn = document.getElementById('scopebtn');
  const list = document.getElementById('scopelist');
  const clr = document.getElementById('scopeclear');
  if (!btn) return;
  const pol = (j && j.policy) || 'ask';
  btn.textContent = 'SCOPE: ' + pol.toUpperCase();
  const base = btn.className.split(' ').filter(function (c) { return c && c.indexOf('sc-') !== 0; }).join(' ');
  btn.className = base + ' sc-' + pol;
  if (list) {
    list.innerHTML = '';
    (j && j.grants || []).forEach(function (g) { list.appendChild(scopeRow(g.path, 'approved', 'allow')); });
    (j && j.denies || []).forEach(function (d) { list.appendChild(scopeRow(d.path, 'denied', 'deny')); });
  }
  if (clr) {
    const n = ((j && j.grants) || []).length + ((j && j.denies) || []).length;
    clr.style.display = n ? '' : 'none';
  }
}
function loadPathPolicy() {
  fetch('/api/path_policy').then(function (r) { return r.json(); })
    .then(renderPathPolicy).catch(function () {});
}
function cycleScope() {
  const btn = document.getElementById('scopebtn');
  if (!btn) return;
  const cur = (btn.textContent || '').toLowerCase().replace('scope:', '').trim();
  const i = SCOPES.indexOf(cur);
  postPolicy({ policy: SCOPES[(i + 1 + SCOPES.length) % SCOPES.length] });
}
function bindScope() {
  const btn = document.getElementById('scopebtn');
  if (btn) btn.onclick = cycleScope;
  const clr = document.getElementById('scopeclear');
  if (clr) clr.onclick = function () { postPolicy({ clear: true }); };
  loadPathPolicy();
}
function onAsk(j) {
  const old = document.getElementById('askov');
  if (old) old.remove();
  const ov = document.createElement('div'); ov.className = 'askov'; ov.id = 'askov';
  const box = document.createElement('div'); box.className = 'askbox';
  const perm = j.kind === 'path_approval';
  const h = document.createElement('h4');
  h.textContent = perm ? 'PERMISSION NEEDED' : 'BONSAI is asking you';
  box.appendChild(h);
  if (perm) {
    const p = document.createElement('div'); p.className = 'askpath';
    p.textContent = j.path || '';
    const w = document.createElement('div'); w.className = 'asktool';
    w.textContent = 'tool: ' + (j.tool || 'file tool') + '  -  outside the workspace';
    box.appendChild(p); box.appendChild(w);
  } else {
    const q = document.createElement('div'); q.className = 'aq';
    q.textContent = j.question || 'What should I do?';
    box.appendChild(q);
  }
  const say = function (ans) {
    ov.remove();
    if (perm) loadPathPolicy();
    fetch('/api/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: j.id, answer: ans }) }).catch(function () {});
  };
  if (j.options && j.options.length) {
    const opts = document.createElement('div'); opts.className = 'opts';
    (j.options).forEach(function (o) {
      const b = document.createElement('button');
      b.className = 'opt' + (/^ALLOW/.test(o) ? ' allow' : (/^DENY/.test(o) ? ' deny' : ''));
      b.textContent = o;
      b.onclick = function () { say(o); };
      opts.appendChild(b);
    });
    box.appendChild(opts);
  }
  if (perm) {
    ov.appendChild(box);
    document.body.appendChild(ov);
    return;
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

async function streamRun(messages, chat, anchorMsg) {
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
  let gotEnd = false;
  let modelUp = false;
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
        if (ev === 'start') { setModelStatus(!!j.model_ready, !j.model_ready); }
        else if (ev === 'delta') { if (!modelUp) { modelUp = true; setModelStatus(true, false); } reply += j.text; onDelta(j.text); statsVals.respond_ms = Date.now() - startedAt; }
        else if (ev === 'reason') { if (!modelUp) { modelUp = true; setModelStatus(true, false); } reason += j.text; onReason(j.text); setBonsaiState('thinking'); statsVals.think_ms = Date.now() - startedAt; }
        else if (ev === 'tool') { if (!modelUp) { modelUp = true; setModelStatus(true, false); } calls.push(j.call); onTool(j.call); setBonsaiState('tools'); }
        else if (ev === 'stats') { onStats(j); }
        else if (ev === 'ask') { onAsk(j); setBonsaiState('thinking'); }
        else if (ev === 'todo') { renderTodo(j.todos); }
        else if (ev === 'error') { doneThinking('Error: ' + j.text); setBonsaiState('idle'); if (!modelUp) setModelStatus(false, false); stopStats(); throw new Error(j.text); }
        else if (ev === 'done') { gotEnd = true; doneThinking(); setBonsaiState('idle'); }
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
  if (!gotEnd) throw new Error('The reply was cut off unexpectedly - please try again.');
  const last = chat.messages[chat.messages.length - 1];
  const savedStats = statsVals && (statsVals.think_ms || statsVals.respond_ms || statsVals.completion_tokens)
      ? JSON.parse(JSON.stringify(statsVals)) : null;
  const entry = { role: 'assistant', content: reply, calls: calls,
                  reason: reason.trim() ? reason : undefined, stats: savedStats };
  if (anchorMsg) {
    const at = chat.messages.indexOf(anchorMsg);
    if (at >= 0) chat.messages.splice(at + 1, 0, entry);
    else chat.messages.push(entry);
  } else if (last && last.role === 'user') {
    chat.messages.push(entry);
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
const _sendBtn = document.getElementById('send');
if (_sendBtn) _sendBtn.onclick = function () {
  if (busy) { stopRun(); return false; }
  return true;
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
    ok: ['on', 'Blender MCP: connected - the Blender tools are available.'],
    bridge: ['mid', 'Blender MCP: bridge is up, but the addon is not enabled.'],
    down: ['off', 'Blender MCP: not connected. Open Blender with the addon enabled.'],
    missing: ['off', 'Blender MCP: not installed (pip install mcp-for-blender).']
  };
  const m = map[j.state || 'down'] || map.down;
  el.className = el.classList.contains('blrow') ? 'blrow ' + m[0] : 'blstatus ' + m[0];
  const dot = document.getElementById('bldot');
  if (dot) dot.className = 'bldot ' + m[0];
  el.title = j.detail ? (m[1] + ' (' + j.state + ': ' + j.detail + ')') : m[1];
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
  btn.textContent = 'EJECTING...';
  btn.style.opacity = '0.5';
  fetch('/api/eject', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      btn.textContent = j.ok ? 'EJECTED' : 'FAILED';
      btn.title = j.detail || j.error || (j.ok ? 'Model unloaded - it reloads on the next message' : 'Eject failed');
      if (typeof setModelStatus === 'function') setModelStatus(false, false);
      setTimeout(function () {
        btn.textContent = 'EJECT';
        btn.disabled = false;
        btn.style.opacity = '1';
      }, 2500);
    })
    .catch(function () {
      btn.textContent = 'FAILED';
      btn.title = 'The eject request failed.';
      setTimeout(function () {
        btn.textContent = 'EJECT';
        btn.disabled = false;
        btn.style.opacity = '1';
      }, 2500);
    });
};

/* ---------- TTS toggle (Piper) ---------- */
function renderTtsBtn(j) {
  const el = document.getElementById('ttsbtn');
  if (!el) return;
  el.textContent = j.enabled ? 'TTS: ON' : 'TTS: OFF';
  el.classList.toggle('on', !!j.enabled);
  el.title = 'Text-to-speech (Piper) - ' + (j.enabled ? 'speech enabled' : 'off, click to enable');
}
function refreshTts() {
  fetch('/api/tts')
    .then(function (r) { return r.json(); })
    .then(renderTtsBtn)
    .catch(function () { renderTtsBtn({ enabled: false }); });
}
document.getElementById('ttsbtn').onclick = function () {
  const el = document.getElementById('ttsbtn');
  const next = !el.classList.contains('on');
  fetch('/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: next }) })
    .then(function (r) { return r.json(); })
    .then(renderTtsBtn)
    .catch(function () { alert('TTS toggle failed'); });
};
refreshTts();

/* ---------- MODEL selector ---------- */
function modelLabel(m) {
  return (m.type === 'api' ? '\u2601 ' : '\u25C9 ') + (m.label || m.id);
}
function setModelStatus(ready, loading) {
  const t = document.getElementById('system-status-text');
  const dot = document.getElementById('sdot');
  if (dot) dot.className = 'dot' + (loading ? ' busy' : (ready ? '' : ' idle'));
  if (!t) return;
  t.textContent = loading ? 'LOADING MODEL...' : (ready ? 'ONLINE / LOADED' : 'ONLINE / NOT LOADED');
  t.title = loading ? 'The model is being loaded into RAM/VRAM - this only happens on your first message.'
    : (ready ? 'The model is resident in RAM/VRAM.' : 'The model is not loaded. It loads the first time you send a message.');
}
function renderModels(j) {
  const sel = document.getElementById('modelsel');
  if (!sel) return;
  sel.innerHTML = '';
  (j.models || []).forEach(function (m) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = modelLabel(m) + (m.type === 'local' && m.vision ? ' · vision' : '') +
      (m.type === 'local' && m.available === false ? ' (missing)' : '');
    if (m.id === j.active) o.selected = true;
    sel.appendChild(o);
  });
  sel.classList.toggle('off', !j.managed);
  sel.setAttribute('data-prev', j.active || '');
  const badge = document.getElementById('modelbadge');
  if (badge && j.active_label) badge.textContent = j.active_label;
  const vs = document.getElementById('visionbadge');
  if (vs) vs.textContent = j.vision ? 'mmproj ON' : 'mmproj OFF';
  const fm = document.getElementById('footer-model');
  if (fm && j.active_label) fm.textContent = j.active_label;
  const fc = document.getElementById('footer-caps');
  if (fc) fc.textContent = (j.vision ? 'VISION' : 'TEXT-ONLY') + '+TOOLS';
  if (typeof setModelStatus === 'function') setModelStatus(!!j.ready, !!j.loading);
  window.__lastModels = j;
  renderMmprojList(j);
}
function renderMmprojList(j) {
  const sel = document.getElementById('mmprojfor');
  const hint = document.getElementById('mmprojhint');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '';
  (j.models || []).filter(function (m) { return m.type === 'local'; })
    .forEach(function (m) {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = (m.label || m.id) + (m.vision ? '  (vision on)' : '');
      sel.appendChild(o);
    });
  if (keep) sel.value = keep;
  const cur = (j.models || []).filter(function (m) { return m.id === sel.value; })[0];
  if (hint) {
    hint.textContent = !cur ? 'No local models yet.'
      : (cur.vision ? 'Projector: ' + String(cur.mmproj).split(/[\\/]/).pop()
                    : 'No projector attached - this model is text-only.');
  }
}
function setMmproj(mmproj) {
  const sel = document.getElementById('mmprojfor');
  if (!sel || !sel.value) { alert('Add a local model first.'); return; }
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'mmproj', id: sel.value, mmproj: mmproj }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) { alert('Could not set the projector:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j);
    })
    .catch(function () { alert('Could not set the projector'); });
}
function refreshModels() {
  fetch('/api/models')
    .then(function (r) { return r.json(); })
    .then(renderModels)
    .catch(function () {});
}
document.getElementById('modelsel').onchange = function () {
  const sel = document.getElementById('modelsel');
  const id = sel.value;
  const prev = sel.getAttribute('data-prev') || '';
  sel.disabled = true;
  sel.title = 'Switching model - a local model restarts the model server, this can take up to a minute...';
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'select', id: id }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      sel.disabled = false;
      renderModels(j);
      if (!j.ok) { alert('Could not switch model: ' + (j.error || 'unknown error')); sel.value = prev; }
    })
    .catch(function () { sel.disabled = false; sel.value = prev; alert('Model switch failed'); });
};
/* ---------- ADD MODEL modal (native file / folder browser) ---------- */
function openAddMdl() { const m = document.getElementById('addmdl'); if (m) m.classList.add('on'); }
function closeAddMdl() { const m = document.getElementById('addmdl'); if (m) m.classList.remove('on'); }
function afterAdd(j, what) {
  if (j.cancelled) return;
  if (!j.ok) { alert('Could not add the model:\\n' + (j.error || 'unknown error')); return; }
  renderModels(j);
  if (what === 'folder') {
    alert('Added ' + ((j.added && j.added.length) || 0) + ' model(s) from that folder' +
          (j.skipped ? ' (' + j.skipped + ' already in the list)' : '') +
          '.\\n\\nPick the one you want from the dropdown.');
  } else if (what === 'file') {
    alert('Model added. Pick it from the dropdown to use it.');
  }
  closeAddMdl();
}
function addModelPick(what) {
  const btn = document.getElementById('addmodelbtn');
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  fetch('/api/pick_model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ what: what }) })
    .then(function (r) { return r.json(); })
    .then(function (j) { afterAdd(j, what); })
    .catch(function () { alert('Could not open the file browser'); })
    .then(function () { if (btn) { btn.disabled = false; btn.textContent = '+'; } });
}
function addModelApi() {
  const base = (document.getElementById('apibase').value || '').trim();
  const model = (document.getElementById('apimodel').value || '').trim();
  if (!base || !model) { alert('Fill in both the base URL and the model id.'); return; }
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'add', type: 'api', label: model, base_url: base, model: model }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) { alert('Could not add the model:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j); closeAddMdl();
      document.getElementById('apibase').value = '';
      document.getElementById('apimodel').value = '';
    })
    .catch(function () { alert('Add failed'); });
}
document.getElementById('addmodelbtn').onclick = openAddMdl;
const _mdlFile = document.getElementById('mdl-file');
if (_mdlFile) _mdlFile.onclick = function () { addModelPick('file'); };
const _mdlFolder = document.getElementById('mdl-folder');
if (_mdlFolder) _mdlFolder.onclick = function () { addModelPick('folder'); };
const _mdlApi = document.getElementById('mdl-api');
if (_mdlApi) _mdlApi.onclick = addModelApi;
const _mmprojBtn = document.getElementById('mdl-mmproj');
if (_mmprojBtn) _mmprojBtn.onclick = function () {
  const sel = document.getElementById('mmprojfor');
  if (!sel || !sel.value) { alert('Add a local model first.'); return; }
  _mmprojBtn.disabled = true; _mmprojBtn.textContent = '...';
  fetch('/api/pick_model', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ what: 'mmproj', id: sel.value }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.cancelled) return;
      if (!j.ok) { alert('Could not set the projector:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j);
    })
    .catch(function () { alert('Could not open the file browser'); })
    .then(function () { _mmprojBtn.disabled = false; _mmprojBtn.innerHTML = 'Browse&hellip;'; });
};
const _mmprojClear = document.getElementById('mdl-mmproj-clear');
if (_mmprojClear) _mmprojClear.onclick = function () { setMmproj(''); };
const _mmprojSel = document.getElementById('mmprojfor');
if (_mmprojSel) _mmprojSel.onchange = function () { renderMmprojList(window.__lastModels || { models: [] }); };
const _mdlClose = document.getElementById('addmdl-close');
if (_mdlClose) _mdlClose.onclick = closeAddMdl;
const _mdl = document.getElementById('addmdl');
if (_mdl) _mdl.onclick = function (ev) { if (ev.target === _mdl) closeAddMdl(); };
/* ---------- MODEL SETTINGS modal ---------- */
function cfgFill(j) {
  const models = (j && j.models) || [];
  const act = models.filter(function (m) { return m.id === (j && j.active); })[0];
  const cfg = (act && act.cfg) || (j && j.defaults) || { ctx: 32768, temp: 1, top_p: 0.95, top_k: 20, ngl: 99 };
  const set = function (id, v) { const e = document.getElementById(id); if (e) e.value = v; };
  set('cfg_ctx', cfg.ctx); set('cfg_temp', cfg.temp);
  set('cfg_top_p', cfg.top_p); set('cfg_top_k', cfg.top_k); set('cfg_ngl', cfg.ngl);
  const who = document.getElementById('cfgwho');
  if (who) who.textContent = act ? ('- ' + (act.label || act.id)) : '';
  const hint = document.getElementById('cfghint');
  if (hint) {
    hint.textContent = !act ? 'No local model selected.'
      : (act.type === 'api' ? 'External endpoints are configured by their own server.'
      : 'Saved settings are used the next time this model is loaded (after a switch or EJECT).');
  }
}
function openCfgMdl() {
  const m = document.getElementById('cfgmdl');
  if (!m) return;
  cfgFill(window.__lastModels);
  m.classList.add('on');
}
function closeCfgMdl() { const m = document.getElementById('cfgmdl'); if (m) m.classList.remove('on'); }
const _cfgBtn = document.getElementById('cfgbtn');
if (_cfgBtn) _cfgBtn.onclick = openCfgMdl;
const _cfgClose = document.getElementById('cfg-close');
if (_cfgClose) _cfgClose.onclick = closeCfgMdl;
const _cfgMdl = document.getElementById('cfgmdl');
if (_cfgMdl) _cfgMdl.onclick = function (ev) { if (ev.target === _cfgMdl) closeCfgMdl(); };
const _cfgDefaults = document.getElementById('cfg-defaults');
if (_cfgDefaults) _cfgDefaults.onclick = function () {
  const j = window.__lastModels || {};
  const d = j.defaults || { ctx: 32768, temp: 1, top_p: 0.95, top_k: 20, ngl: 99 };
  const set = function (id, v) { const e = document.getElementById(id); if (e) e.value = v; };
  set('cfg_ctx', d.ctx); set('cfg_temp', d.temp);
  set('cfg_top_p', d.top_p); set('cfg_top_k', d.top_k); set('cfg_ngl', d.ngl);
};
const _cfgSave = document.getElementById('cfg-save');
if (_cfgSave) _cfgSave.onclick = function () {
  const j = window.__lastModels || {};
  const act = (j.models || []).filter(function (m) { return m.id === j.active; })[0];
  if (!act) { alert('No model selected.'); return; }
  const num = function (id) {
    const e = document.getElementById(id);
    return e && e.value !== '' ? Number(e.value) : undefined;
  };
  const cfg = { ctx: num('cfg_ctx'), temp: num('cfg_temp'), top_p: num('cfg_top_p'),
                top_k: num('cfg_top_k'), ngl: num('cfg_ngl') };
  _cfgSave.disabled = true;
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'config', id: act.id, cfg: cfg }) })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (!res.ok) { alert('Could not save the settings:\\n' + (res.error || 'unknown error')); return; }
      renderModels(res);
      cfgFill(res);
      const hint = document.getElementById('cfghint');
      if (hint) hint.textContent = 'Saved. These values apply the next time this model is loaded.';
    })
    .catch(function () { alert('Could not save the settings'); })
    .then(function () { _cfgSave.disabled = false; });
};
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') closeAddMdl();
});
refreshModels();

const inp = document.getElementById('user-input');
inp.addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 160) + 'px'; });
inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); handleSubmit(ev); } });

/* ---------- BONSAI FX: clock & metrics ---------- */
function updateClock() {
  const now = new Date();
  document.getElementById('clock-time').textContent = now.toLocaleTimeString('ro-RO');
  document.getElementById('clock-date').textContent = now.toLocaleDateString('ro-RO');
}
function schedToast(ev) {
  let box = document.getElementById('toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    document.body.appendChild(box);
  }
  const t = document.createElement('div');
  t.className = 'toast';
  const h = document.createElement('div');
  h.className = 'toasthd';
  h.textContent = 'SCHEDULED TASK RAN: ' + ev.name + (ev.when ? '  (' + ev.when + ')' : '');
  const x = document.createElement('button');
  x.className = 'toastx';
  x.textContent = '\u00d7';
  x.title = 'dismiss';
  x.onclick = function () { if (t.parentNode) t.parentNode.removeChild(t); };
  const c = document.createElement('div');
  c.className = 'toastcmd';
  c.textContent = ev.command || '';
  const o = document.createElement('pre');
  o.className = 'toastout';
  o.textContent = (ev.output || '').slice(0, 500) || '(no output)';
  t.appendChild(h); t.appendChild(x); t.appendChild(c); t.appendChild(o);
  if (ev.ran_at) {
    const w = document.createElement('div');
    w.className = 'toastwhen';
    w.textContent = 'ran at ' + ev.ran_at;
    t.appendChild(w);
  }
  box.appendChild(t);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 30000);
}
function startSchedWatch() {
  const tick = function () {
    fetch('/api/sched_results').then(function (r) { return r.json(); })
      .then(function (j) { (j.events || []).forEach(schedToast); })
      .catch(function () {});
  };
  tick();
  setInterval(tick, 4000);
}
function startMetrics() {
  const set = function (id, v) {
    const el = document.getElementById(id + '-val');
    const bar = document.getElementById(id + '-bar');
    if (el) el.textContent = (v === null || v === undefined) ? 'n/a' : v + '%';
    if (bar) bar.style.width = (v === null || v === undefined) ? '0%' : Math.max(0, Math.min(100, v)) + '%';
  };
  const tick = function () {
    fetch('/api/sysinfo')
      .then(function (r) { return r.json(); })
      .then(function (j) { set('cpu', j.cpu); set('ram', j.ram); set('gpu', j.gpu); })
      .catch(function () { set('cpu', null); set('ram', null); set('gpu', null); });
  };
  tick();
  setInterval(tick, 2500);
}

const reactor = document.getElementById('arc-reactor');
const waveform = document.getElementById('waveform');
const stateLabel = document.getElementById('bonsai-state-label');
const subLabel = document.getElementById('bonsai-sub');
let bonsaiState = 'idle';
function setBonsaiState(state) {
  bonsaiState = state;
  ['thinking', 'tools', 'rainbow', 'planmode'].forEach(function (s) {
    reactor.classList.remove(s); document.body.classList.remove(s);
  });
  waveform.classList.remove('active-wave');
  if (state === 'thinking') {
    reactor.classList.add('thinking'); document.body.classList.add('thinking');
    stateLabel.textContent = 'PROCESSING COMMAND...';
    if (subLabel) subLabel.textContent = 'Thinking it through...';
  } else if (state === 'tools') {
    reactor.classList.add('tools'); document.body.classList.add('tools');
    waveform.classList.add('active-wave');
    stateLabel.textContent = 'EXECUTING TOOLS...';
    if (subLabel) subLabel.textContent = 'Working on your PC...';
  } else {
    const plan = chatMode === 'plan';
    reactor.classList.add(plan ? 'planmode' : 'rainbow');
    stateLabel.textContent = plan ? 'PLAN MODE' : 'BONSAI READY';
    if (subLabel) subLabel.textContent = plan ? 'Read-only - no tools will run' : 'Awaiting your command';
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

# ---------------------------------------------------------------------------
# GPT-style clean UI, served at /chat. Same backend, same features, fresh look.
# ---------------------------------------------------------------------------
PAGE_GPT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BONSAI - Chat</title>
<style>
  :root {
    color-scheme: light;
    --bg: #ffffff; --bg2: #f7f7f9; --bg3: #ededf0; --bd: #e3e3e7;
    --txt: #111214; --mut: #71717a; --acc: #19191c; --acc-txt: #fafafa;
    --ok: #16a34a; --warn: #d97706; --err: #dc2626;
  }
  html.dark {
    color-scheme: dark;
    --bg: #212121; --bg2: #171717; --bg3: #2f2f2f; --bd: #303030;
    --txt: #ececec; --mut: #9b9ba3; --acc: #e4e4e7; --acc-txt: #18181b;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body { font-family: "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--txt); }
  .app { display: flex; height: 100vh; min-height: 0; }
  .side { width: 264px; flex: none; background: var(--bg2); border-right: 1px solid var(--bd); display: flex; flex-direction: column; min-height: 0; }
  .side-head { padding: 12px 14px; border-bottom: 1px solid var(--bd); }
  .brand { display: flex; align-items: center; gap: 9px; font-weight: 600; letter-spacing: .3px; }
  .logo { width: 26px; height: 26px; border-radius: 7px; background: var(--acc); color: var(--acc-txt); display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 14px; }
  .btn-new { width: 100%; margin-top: 12px; border: 1px solid var(--bd); background: transparent; color: var(--txt); border-radius: 10px; padding: 9px 12px; cursor: pointer; font-weight: 600; text-align: left; }
  .btn-new:hover { border-color: var(--mut); }
  .chatlist { flex: 1; overflow-y: auto; padding: 10px; display: flex; flex-direction: column; gap: 2px; min-height: 0; }
  .chat-item { display: flex; align-items: center; gap: 6px; padding: 8px 10px; border-radius: 8px; cursor: pointer; color: var(--txt); font-size: 13px; }
  .chat-item:hover { background: var(--bg3); }
  .chat-item.active { background: var(--bg3); font-weight: 600; }
  .chat-item .tit { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .chat-item .del { background: none; border: none; color: var(--mut); cursor: pointer; font-size: 14px; opacity: 0; padding: 0 4px; }
  .chat-item:hover .del { opacity: 1; }
  .side-foot { padding: 10px 12px; border-top: 1px solid var(--bd); display: flex; flex-direction: column; gap: 8px; }
  .blrow { display: flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--mut); }
  .blrow b { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #8b8b8b; }
  .blrow.ok b { background: var(--ok); }
  .blrow.mid b { background: var(--warn); }
  .blrow.off b { background: var(--err); }
  .blrow .blicon { width: 14px; height: 14px; flex: none; }
  .blrow .bldot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--mut); }
  .blrow .bldot.on { background: var(--ok); }
  .blrow .bldot.mid { background: var(--warn); }
  .blrow .bldot.off { background: var(--err); }
  .btn-ghost { border: 1px solid var(--bd); background: transparent; color: var(--txt); border-radius: 8px; padding: 7px 10px; cursor: pointer; font-size: 12px; }
  .btn-ghost.on { background: rgba(74,222,128,.15); border-color: #4ade80; color: #86efac; }
  .btn-ghost.on:hover { background: rgba(74,222,128,.3); }
  .btn-ghost:hover { border-color: var(--mut); }
  .mdl { position: fixed; inset: 0; z-index: 90; display: none; align-items: center; justify-content: center; background: rgba(0,0,0,.55); }
  .mdl.on { display: flex; }
  .mdlbox { width: min(560px, 92vw); background: var(--bg2); border: 1px solid var(--bd); border-radius: 14px; padding: 18px; }
  .mdlbox h4 { margin: 0 0 12px; font-size: 12px; letter-spacing: 1.5px; color: var(--mut); text-transform: uppercase; }
  .mdlbox .act { display: block; width: 100%; text-align: left; background: transparent; border: 1px solid var(--bd); color: var(--txt); border-radius: 10px; padding: 11px 12px; margin-bottom: 8px; cursor: pointer; font-size: 13px; font-weight: 500; }
  .mdlbox .act:hover { background: var(--bg3); border-color: var(--mut); }
  .mdlbox .act small { display: block; color: var(--mut); font-size: 11.5px; margin-top: 3px; font-weight: 400; }
  .mdlbox .act.inline { width: auto; margin: 0; padding: 10px 16px; }
  .mdlbox .mdlsep { color: var(--mut); font-size: 11px; letter-spacing: 1px; text-transform: uppercase; margin: 14px 0 8px; }
  .mdlbox .row { display: flex; gap: 8px; }
  .mdlbox input { flex: 1; min-width: 0; background: transparent; border: 1px solid var(--bd); border-radius: 10px; padding: 10px; color: var(--txt); font-size: 13px; outline: none; }
  .mdlbox input:focus { border-color: var(--mut); }
  .mdlbox select { flex: 1; min-width: 0; background: transparent; border: 1px solid var(--bd); border-radius: 10px; padding: 10px; color: var(--txt); font-size: 13px; outline: none; }
  .mdlbox select option { background: #17171a; color: var(--txt); }
  .mdlbox .hint { color: var(--mut); font-size: 11.5px; margin-top: 8px; }
  .cfgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-bottom: 4px; }
  .cfgrid label { display: flex; flex-direction: column; gap: 4px; font-size: 11.5px; color: var(--mut); }
  .cfgrid input { width: 100%; box-sizing: border-box; background: transparent; border: 1px solid var(--bd); border-radius: 8px; padding: 8px 10px; color: var(--txt); font-size: 13px; outline: none; }
  .cfgrid input:focus { border-color: var(--mut); }
  .mdlbox h4 span { color: var(--mut); font-weight: 400; text-transform: none; letter-spacing: 0; }
  .mdlbox .close { margin-top: 14px; text-align: center; color: var(--mut); cursor: pointer; font-size: 12.5px; }
  .mdlbox .close:hover { color: var(--err); }
  .wd { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 100%; text-align: left; }
  .foot-row { display: flex; gap: 6px; }
  .foot-row a { text-decoration: none; color: var(--mut); flex: 1; text-align: center; }
  .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
  .topbar { height: 54px; flex: none; border-bottom: 1px solid var(--bd); display: flex; align-items: center; justify-content: flex-end; gap: 10px; padding: 0 18px; }
  .pill { border: 1px solid var(--bd); background: transparent; color: var(--txt); border-radius: 999px; padding: 6px 14px; font-size: 12px; cursor: pointer; font-weight: 600; }
  .pill:hover { border-color: var(--mut); }
  .pill.plan { color: var(--acc); border-color: var(--acc); }
  select.pill { cursor: pointer; background: var(--bg2); }
  select.pill option { background: var(--bg); color: var(--txt); }
  .state { font-size: 11px; letter-spacing: 1px; color: var(--mut); font-weight: 600; }
  .messages { flex: 1; overflow-y: auto; min-height: 0; }
  .inner { max-width: 780px; margin: 0 auto; padding: 20px 24px 4px; display: flex; flex-direction: column; gap: 20px; min-height: 100%; }
  .empty { display: flex; flex-direction: column; align-items: center; justify-content: center; flex: 1; text-align: center; color: var(--mut); min-height: 55vh; }
  .empty .logo { width: 52px; height: 52px; border-radius: 16px; font-size: 26px; margin-bottom: 16px; }
  .empty h1 { margin: 0 0 8px; color: var(--txt); font-size: 22px; letter-spacing: .5px; }
  .empty p { font-size: 13px; line-height: 1.6; max-width: 390px; margin: 0; }
  .msgrow { display: flex; flex-direction: column; }
  .msgrow.user { align-items: flex-end; }
  .ubub { background: var(--bg3); border-radius: 16px 16px 4px 16px; padding: 10px 14px; max-width: 80%; font-size: 14px; white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
  .ubub .thumbs { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .ubub .thumb { width: 60px; height: 60px; border-radius: 10px; overflow: hidden; border: 1px solid var(--bd); }
  .ubub .thumb img { width: 100%; height: 100%; object-fit: cover; }
  .ubub .pf { display: inline-block; background: var(--bg2); border: 1px solid var(--bd); border-radius: 999px; padding: 4px 10px; font-size: 12px; }
  .abody { font-size: 14px; line-height: 1.65; word-break: break-word; }
  .abody pre { background: var(--bg2); border: 1px solid var(--bd); border-radius: 10px; padding: 10px 12px; overflow-x: auto; font-size: 12.5px; line-height: 1.5; }
  .abody code { background: var(--bg3); border-radius: 5px; padding: 1px 5px; font-size: 12.5px; }
  .abody pre code { background: none; padding: 0; }
  .abody a { color: var(--acc); }
  .abody.caret::after { content: ''; display: inline-block; width: 7px; height: 14px; background: var(--acc); margin-left: 3px; vertical-align: text-bottom; animation: blk .8s steps(1) infinite; }
  @keyframes blk { 50% { opacity: 0; } }
  .thinkstub { display: flex; align-items: center; gap: 8px; color: var(--mut); font-size: 13px; }
  .dots i { display: inline-block; width: 5px; height: 5px; border-radius: 50%; background: var(--mut); margin-right: 3px; animation: puls 1s infinite; }
  .dots i:nth-child(2) { animation-delay: .15s; }
  .dots i:nth-child(3) { animation-delay: .3s; }
  @keyframes puls { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }
  .reasonbox, .tlog { margin: 2px 0 6px; }
  .reasonbox > summary, .tlog > summary { cursor: pointer; list-style: none; user-select: none; display: inline-flex; align-items: center; gap: 6px; font-size: 11.5px; color: var(--mut); font-weight: 600; padding: 5px 11px; border-radius: 999px; background: var(--bg2); border: 1px solid var(--bd); }
  .reasonbox > summary::-webkit-details-marker, .tlog > summary::-webkit-details-marker { display: none; }
  .reasonbox > summary::before { content: ''; }
  .rc { margin-top: 6px; font-size: 12.5px; line-height: 1.55; color: var(--mut); background: var(--bg2); border: 1px solid var(--bd); border-radius: 10px; padding: 10px; white-space: pre-wrap; max-height: 220px; overflow-y: auto; }
  .tl { margin-top: 6px; display: flex; flex-direction: column; gap: 6px; max-height: 260px; overflow-y: auto; }
  .tlitem { font-family: Consolas, monospace; font-size: 11.5px; background: var(--bg2); border: 1px solid var(--bd); border-radius: 8px; padding: 8px 10px; white-space: pre-wrap; word-break: break-word; }
  .tlitem.err { border-color: var(--err); color: var(--err); }
  .tlitem .rs { color: var(--mut); margin-top: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .tlitem .rs img.tthumb { max-width: 240px; border-radius: 8px; margin-top: 6px; cursor: zoom-in; display: block; }
  .tlprev { margin-top: 8px; }
  .tlprev a { font-size: 11px; color: var(--acc); }
  .tlprev .tlframe { width: 100%; height: 280px; border: 1px dashed var(--bd); border-radius: 8px; background: var(--bg2); margin-top: 6px; }
  .toolsline { margin: 2px 0 8px; display: flex; flex-wrap: wrap; gap: 6px; }
  .msgrow.user.queued .bubble { border-color: var(--warn); opacity: .92; }
  .qbadge { display: inline-block; font-size: 9.5px; letter-spacing: 1.1px; color: var(--warn); border: 1px dashed var(--warn); border-radius: 999px; padding: 2px 9px; margin-bottom: 6px; animation: qpulse 1.6s ease-in-out infinite; }
  @keyframes qpulse { 0%, 100% { opacity: .55; } 50% { opacity: 1; } }
  .scopebtn { width: 100%; text-align: left; font-size: 11px; letter-spacing: 1px; color: var(--acc); background: transparent; border: 1px solid var(--bd); border-radius: 8px; padding: 7px 10px; cursor: pointer; }
  .scopebtn:hover { border-color: var(--acc); }
  .scopebtn.sc-system { color: var(--err); border-color: var(--err); }
  .scopebtn.sc-workspace { color: var(--ok); border-color: var(--ok); }
  .scopelist { display: flex; flex-direction: column; gap: 3px; }
  .scoperow { display: flex; align-items: center; gap: 4px; font-size: 10.5px; font-family: Consolas, monospace; background: var(--bg2); border: 1px solid var(--bd); border-left-width: 2px; border-radius: 5px; padding: 3px 4px 3px 6px; }
  .scoperow.allow { border-left-color: var(--ok); }
  .scoperow.deny { border-left-color: var(--err); }
  .scoperow .sp { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left; color: var(--mut); }
  .scoperow.allow .sp { color: var(--ok); }
  .scoperow.deny .sp { color: var(--err); }
  .scoperow .sx { background: transparent; border: none; color: var(--mut); cursor: pointer; font-size: 13px; line-height: 1; padding: 0 3px; }
  .scoperow .sx:hover { color: var(--err); }
  .scopeclear { width: 100%; background: transparent; border: 1px dashed var(--bd); color: var(--mut); border-radius: 8px; font-size: 10px; letter-spacing: 1px; padding: 5px; cursor: pointer; }
  .scopeclear:hover { color: var(--err); border-color: var(--err); }
  .askpath { font-family: Consolas, monospace; font-size: 12px; color: var(--acc); background: var(--bg2); border: 1px solid var(--bd); border-radius: 6px; padding: 7px 9px; margin: 6px 0 5px; word-break: break-all; }
  .asktool { font-size: 10.5px; color: var(--mut); margin-bottom: 4px; }
  .opt.allow { border-color: var(--ok); color: var(--ok); }
  .opt.allow:hover { background: var(--ok); color: #06210f; }
  .opt.deny { border-color: var(--err); color: var(--err); }
  .opt.deny:hover { background: var(--err); color: #2b0710; }
  #toasts { position: fixed; right: 14px; bottom: 122px; z-index: 90; display: flex; flex-direction: column; gap: 8px; max-width: 380px; pointer-events: none; }
  .toast { position: relative; background: var(--bg2); border: 1px solid var(--acc); border-left-width: 3px; border-radius: 8px; padding: 9px 30px 10px 11px; box-shadow: 0 6px 22px rgba(0,0,0,.55); animation: toastin .22s ease-out; pointer-events: auto; }
  @keyframes toastin { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
  .toasthd { font-size: 10px; letter-spacing: 1.2px; color: var(--acc); margin-bottom: 5px; }
  .toastx { position: absolute; top: 5px; right: 6px; background: transparent; border: none; color: var(--mut); font-size: 15px; line-height: 1; cursor: pointer; }
  .toastx:hover { color: var(--err); }
  .toastcmd { font-family: Consolas, monospace; font-size: 11px; color: var(--txt2); word-break: break-all; margin-bottom: 5px; }
  .toastout { font-family: Consolas, monospace; font-size: 11px; color: var(--txt); background: var(--bg); border: 1px solid var(--bd); border-radius: 5px; padding: 5px 7px; margin: 0; max-height: 130px; overflow: auto; white-space: pre-wrap; word-break: break-word; }
  .toastwhen { font-size: 10px; color: var(--mut); margin-top: 5px; }
  .pvbox { position: fixed; inset: 0; z-index: 95; display: none; flex-direction: column; background: var(--bg2); }
  .pvbox.on { display: flex; }
  .pvboxhd { display: flex; align-items: center; gap: 10px; padding: 8px 12px; border-bottom: 1px solid var(--bd); font-size: 11px; letter-spacing: 1.5px; color: var(--mut); flex: none; }
  .pvboxhd b { color: var(--txt); }
  .pvboxhd span { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: 0; }
  .pvboxx { background: transparent; border: 1px solid var(--bd); color: var(--txt); border-radius: 6px; cursor: pointer; font-size: 16px; line-height: 1; padding: 2px 10px; }
  .pvboxx:hover { border-color: var(--err); color: var(--err); }
  .pvboxframe { flex: 1; width: 100%; border: none; background: var(--bg); }
  .chip { font-size: 11.5px; color: var(--mut); background: var(--bg3); border: 1px solid var(--bd); border-radius: 999px; padding: 4px 11px; font-weight: 600; }
  .chip.ok { color: var(--ok); }
  .chip.err { color: var(--err); border-color: var(--err); }
  .statschip { display: block; margin-top: 8px; font-size: 11px; color: var(--mut); }
  .inputzone { flex: none; padding: 0 0 16px; }
  .innerc { max-width: 780px; margin: 0 auto; padding: 0 24px; }
  .preview { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
  .preview:empty { display: none; }
  .thumb { position: relative; width: 62px; height: 62px; border-radius: 10px; overflow: hidden; border: 1px solid var(--bd); }
  .thumb img { width: 100%; height: 100%; object-fit: cover; }
  .pf { display: inline-flex; align-items: center; gap: 8px; background: var(--bg2); border: 1px solid var(--bd); border-radius: 999px; padding: 6px 12px; font-size: 12px; }
  .x { width: 18px; height: 18px; border-radius: 50%; border: none; background: rgba(0,0,0,.55); color: #fff; font-size: 12px; line-height: 1; cursor: pointer; }
  .pf .x { background: var(--bg3); color: var(--txt); }
  .composer { display: flex; align-items: flex-end; gap: 8px; border: 1px solid var(--bd); border-radius: 18px; padding: 10px 12px; background: var(--bg2); }
  .composer:focus-within { border-color: var(--mut); }
  #user-input { flex: 1; border: none; outline: none; background: transparent; color: var(--txt); font: inherit; font-size: 14px; resize: none; max-height: 160px; padding: 6px 2px; line-height: 1.5; }
  .ic { border: none; background: transparent; color: var(--mut); cursor: pointer; font-size: 15px; width: 32px; height: 32px; border-radius: 8px; display: flex; align-items: center; justify-content: center; }
  .ic:hover { background: var(--bg3); color: var(--txt); }
  .ic.err { color: var(--err); }
  .send { width: 34px; height: 34px; border: none; border-radius: 50%; background: var(--acc); color: var(--acc-txt); cursor: pointer; display: flex; align-items: center; justify-content: center; flex: none; }
  .send:hover { opacity: .85; }
  .send.busy { background: var(--err); }
  .stats { margin: 8px auto 0; font-size: 11px; color: var(--mut); text-align: center; }
  .todo-panel { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 8px; }
  .todo-panel:empty { display: none; }
  .todochip { font-size: 11.5px; padding: 4px 10px; border-radius: 999px; background: var(--bg2); border: 1px solid var(--bd); color: var(--mut); }
  .todochip .st.ok { color: var(--ok); }
  .todochip .st.run { color: var(--warn); }
  .fine { margin: 10px auto 0; font-size: 11px; color: var(--mut); text-align: center; }
  #filein { display: none; }
  .askov { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: flex; align-items: center; justify-content: center; z-index: 90; backdrop-filter: blur(3px); }
  .askbox { background: var(--bg); border: 1px solid var(--bd); border-radius: 16px; padding: 22px; width: min(460px, 90vw); box-shadow: 0 12px 40px rgba(0,0,0,.25); }
  .askbox h4 { margin: 0 0 12px; font-size: 12px; color: var(--mut); letter-spacing: 1px; }
  .aq { margin-bottom: 14px; font-size: 15px; line-height: 1.5; }
  .opts { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 12px; }
  .opt, .afree button { border: 1px solid var(--bd); background: var(--bg2); color: var(--txt); border-radius: 999px; padding: 8px 14px; font-size: 13px; cursor: pointer; }
  .opt:hover { border-color: var(--mut); }
  .afree { display: flex; gap: 8px; }
  .afree input { flex: 1; border: 1px solid var(--bd); border-radius: 10px; padding: 9px 12px; background: var(--bg2); color: var(--txt); font: inherit; outline: none; }
  .afree button { padding: 8px 18px; }
  .dropov { position: fixed; inset: 0; z-index: 95; display: none; align-items: center; justify-content: center; pointer-events: none; }
  .dropov.on { display: flex; }
  .dropov .bx { border: 2px dashed var(--mut); border-radius: 20px; padding: 40px 60px; background: var(--bg); font-size: 15px; color: var(--mut); }
</style>
</head>
<body>
<div class="app">
  <nav class="side">
    <div class="side-head">
      <div class="brand"><div class="logo">B</div><div>BONSAI</div></div>
      <button class="btn-new" id="newchat">+ New chat</button>
    </div>
    <div class="chatlist" id="chatlist"></div>
    <div class="side-foot">
      <div class="blrow" id="blstatus" title="Blender MCP status"><svg class="blicon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 1.6C7.4 1.6 3.7 3.9 3.7 6.9c0 1.6 1.1 3 2.8 3.9-2.1.9-3.5 2.4-3.5 4.2 0 3.2 4 5.8 9 5.8 2.4 0 4.6-.7 6.2-1.8l3.4 2.8 1.7-2-3.3-2.7c.6-.9.9-1.9.9-3 0-1.9-1-3.6-2.6-4.9.3-.5.4-1.1.4-1.7 0-3-3.7-5.3-8.3-5.3Z"/><ellipse cx="12" cy="6.9" rx="4.2" ry="2.5" fill="#18181b"/></svg><span class="bldot off" id="bldot"></span></div>
      <button class="btn-ghost wd" id="workbtn"></button>
      <button class="btn-ghost scopebtn" id="scopebtn" title="How far Bonsai may reach outside the workspace. Click to switch: WORKSPACE (hard sandbox) / ASK (ask me every time) / SYSTEM (no prompts).">SCOPE: ...</button>
      <div class="scopelist" id="scopelist"></div>
      <button class="btn-ghost scopeclear" id="scopeclear" title="Forget every approved and denied path">CLEAR APPROVALS</button>
      <div class="foot-row">
        <a href="/" title="Classic UI">Classic UI</a>
        <button class="btn-ghost" id="ttsbtn" title="Text-to-speech (Piper) - speak replies aloud. Off by default.">TTS: OFF</button>
        <button class="btn-ghost" id="themebtn" title="Toggle theme"></button>
        <button class="btn-ghost" id="ejectbtn" title="Stop the model server now and free its RAM/VRAM (it restarts automatically on the next message)">EJECT</button>
      </div>
    </div>
  </nav>
  <main class="main">
    <header class="topbar">
      <div class="tb-r" style="display:flex;align-items:center;gap:10px">
        <button class="pill" id="modebtn" title="Plan = pure reasoning without tools">BUILD</button>
        <select class="pill" id="modelsel" title="Active AI model - pick a local .gguf or an external OpenAI-compatible endpoint" style="max-width:190px"></select>
        <button class="pill" id="addmodelbtn" title="Add a model (local .gguf file or an external API endpoint)">+</button>
        <button class="pill" id="cfgbtn" title="Model settings - context size, temperature, GPU layers">&#9881;</button>
        <select class="pill" id="effort-sel" title="Thinking effort - how deeply BONSAI reasons">
          <option value="off">Effort: off</option>
          <option value="low">Effort: low</option>
          <option value="med" selected>Effort: med</option>
          <option value="high">Effort: high</option>
        </select>
        <span class="state" id="state-label">READY</span>
      </div>
    </header>

    <div class="mdl" id="addmdl">
      <div class="mdlbox">
        <h4>ADD A MODEL</h4>
        <button class="act" id="mdl-file">Model file (.gguf) &mdash; browse&hellip;<small>Pick a single GGUF model from anywhere on this PC.</small></button>
        <button class="act" id="mdl-folder">Model folder &mdash; browse&hellip;<small>Scan a folder (and its subfolders) and add every .gguf it finds.</small></button>
        <div class="mdlsep">or connect to an API server</div>
        <div class="row">
          <input id="apibase" placeholder="base URL, e.g. http://127.0.0.1:1234/v1">
          <input id="apimodel" placeholder="model id, e.g. qwen3-8b">
          <button class="act inline" id="mdl-api">Add</button>
        </div>
        <div class="mdlsep">vision projector (optional - enables screenshots &amp; images)</div>
        <div class="row">
          <select id="mmprojfor"></select>
          <button class="act inline" id="mdl-mmproj">Browse&hellip;</button>
          <button class="act inline" id="mdl-mmproj-clear">Clear</button>
        </div>
        <div class="hint" id="mmprojhint"></div>
        <div class="close" id="addmdl-close">Cancel</div>
      </div>
    </div>

    <div class="mdl" id="cfgmdl">
      <div class="mdlbox">
        <h4>MODEL SETTINGS <span id="cfgwho"></span></h4>
        <div class="cfgrid">
          <label>Context size (ctx)<input id="cfg_ctx" type="number" min="512" max="1048576" step="512"></label>
          <label>Temperature<input id="cfg_temp" type="number" min="0" max="2" step="0.05"></label>
          <label>Top-p<input id="cfg_top_p" type="number" min="0.01" max="1" step="0.01"></label>
          <label>Top-k<input id="cfg_top_k" type="number" min="0" max="1000" step="1"></label>
          <label>GPU layers (-ngl)<input id="cfg_ngl" type="number" min="-1" max="999" step="1"></label>
        </div>
        <div class="hint" id="cfghint"></div>
        <div class="row">
          <button class="act inline" id="cfg-save">Save</button>
          <button class="act inline" id="cfg-defaults">Defaults</button>
          <button class="act inline" id="cfg-close">Close</button>
        </div>
      </div>
    </div>

    <div class="messages" id="messages">
      <div class="inner">
        <div class="empty" id="empty">
          <div class="logo">B</div>
          <h1>BONSAI</h1>
          <p>Your local PC assistant. Ask anything - I can search, read files, run commands and see your screen. Everything runs on this PC.</p>
        </div>
      </div>
    </div>
    <div class="inputzone">
      <div class="innerc">
        <div class="preview" id="preview"></div>
        <div class="todo-panel" id="todopanel"></div>
        <div class="composer">
          <textarea id="user-input" rows="1" placeholder="Message BONSAI..."></textarea>
          <div style="display:flex;align-items:center;gap:2px">
            <button class="ic" id="attach" title="Attach images / files">&#128206;</button>
            <button class="ic" id="mic-btn" title="Microphone">&#127908;</button>
            <button class="send" id="send" title="Send"></button>
          </div>
        </div>
        <div class="stats" id="statsline"></div>
        <div class="fine">BONSAI 2 27B local &middot; tools + vision &middot; your data stays on this PC</div>
      </div>
    </div>
  </main>
</div>
<input type="file" id="filein" accept="image/*,.txt,.md,.py,.js,.ts,.json,.csv,.log,.ini,.cfg,.xml,.html,.css,.bat,.ps1,.sh,.yml,.yaml,.sql,.java,.cpp,.c,.h,.cs,.go,.rb,.php,.toml,.env,.gitignore" multiple>
<script>
const BONSAI_CTX = 32768;
let chats = load();
let cur = null, busy = false, pendingAtt = [], started = false, abortCtrl = null, msgQueue = [];
let chatMode = localStorage.getItem('bonsai_gpt_mode') === 'plan' ? 'plan' : 'build';
let workdir = '';
let thinkingRow = null, liveCalls = [], statsTimer = null;
let statsVals = { think_ms: 0, respond_ms: 0, tok_s: 0, ctx_used: 0, ctx_left: 0, prompt_tokens: 0, completion_tokens: 0 };

const SEND_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 19V5M5 12l7-7 7 7"/></svg>';
const STOP_SVG = '<svg width="13" height="13" viewBox="0 0 24 24"><rect x="5" y="5" width="14" height="14" rx="2" fill="currentColor"/></svg>';

function load() { try { const v = JSON.parse(localStorage.getItem('bonsai_gpt_chats') || '[]'); return Array.isArray(v) ? v : []; } catch (e) { return []; } }
const IMG_KEEP = 200 * 1024;
function stripHeavyImages(m) {
  if (!Array.isArray(m.content)) return;
  const keep = [];
  let dropped = 0;
  m.content.forEach(function (p) {
    if (p.type === 'image_url' && p.image_url && (p.image_url.url || '').length > IMG_KEEP) { dropped++; return; }
    keep.push(p);
  });
  if (dropped) m.content = keep.length ? keep : '[large image removed from history]';
}
function save() {
  try {
    const recent = {};
    chats.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); })
      .slice(0, 5).forEach(function (c) { recent[c.id] = 1; });
    const c = chats.slice(-200).map(function (ch) {
      const copy = JSON.parse(JSON.stringify(ch));
      (copy.messages || []).forEach(function (m) {
        if (m.calls) (m.calls).forEach(function (cl) { if (cl.preview) delete cl.preview; });
        if (!recent[ch.id]) stripHeavyImages(m);
      });
      return copy;
    });
    localStorage.setItem('bonsai_gpt_chats', JSON.stringify(c));
  } catch (e) {}
}
function newChat() {
  if (cur && chats.indexOf(cur) !== -1 && cur.messages.length === 0 && cur.title === 'New chat') { started = false; renderAll(); return; }
  if (cur && cur.messages.length > 0 && chats.indexOf(cur) === -1) chats.push(cur);
  cur = { id: 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6), title: 'New chat', ts: Date.now(), messages: [] };
  chats.push(cur); started = false; save(); renderAll();
  postPolicy({ clear: true });
}
function schedToast(ev) {
  let box = document.getElementById('toasts');
  if (!box) {
    box = document.createElement('div');
    box.id = 'toasts';
    document.body.appendChild(box);
  }
  const t = document.createElement('div');
  t.className = 'toast';
  const h = document.createElement('div');
  h.className = 'toasthd';
  h.textContent = 'SCHEDULED TASK RAN: ' + ev.name + (ev.when ? '  (' + ev.when + ')' : '');
  const x = document.createElement('button');
  x.className = 'toastx';
  x.textContent = '\u00d7';
  x.title = 'dismiss';
  x.onclick = function () { if (t.parentNode) t.parentNode.removeChild(t); };
  const c = document.createElement('div');
  c.className = 'toastcmd';
  c.textContent = ev.command || '';
  const o = document.createElement('pre');
  o.className = 'toastout';
  o.textContent = (ev.output || '').slice(0, 500) || '(no output)';
  t.appendChild(h); t.appendChild(x); t.appendChild(c); t.appendChild(o);
  if (ev.ran_at) {
    const w = document.createElement('div');
    w.className = 'toastwhen';
    w.textContent = 'ran at ' + ev.ran_at;
    t.appendChild(w);
  }
  box.appendChild(t);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 30000);
}
function startSchedWatch() {
  const tick = function () {
    fetch('/api/sched_results').then(function (r) { return r.json(); })
      .then(function (j) { (j.events || []).forEach(schedToast); })
      .catch(function () {});
  };
  tick();
  setInterval(tick, 4000);
}
function init() {
  bindModeBtn(); bindTheme(); loadWorkdir();
  dedupeChats();
  if (!chats.length) newChat(); else cur = chats[chats.length - 1];
  renderAll(); setSendUI();
  startSchedWatch();
  bindScope();
  fetch('/api/todos').then(function (r) { return r.json(); }).then(function (j) {
    if (j && j.todos) renderTodo(j.todos);
  }).catch(function () {});
  refreshBlender();
  setInterval(refreshBlender, 8000);
}
function bindModeBtn() {
  const btn = document.getElementById('modebtn');
  const update = function () { btn.textContent = chatMode === 'plan' ? 'PLAN' : 'BUILD'; btn.classList.toggle('plan', chatMode === 'plan'); };
  update();
  btn.onclick = function () { chatMode = chatMode === 'plan' ? 'build' : 'plan'; localStorage.setItem('bonsai_gpt_mode', chatMode); update(); };
}
function bindTheme() {
  const b = document.getElementById('themebtn');
  const apply = function (d) { document.documentElement.classList.toggle('dark', d); b.textContent = d ? '\u2600\ufe0f' : '\u263e'; };
  const saved = localStorage.getItem('bonsai_gpt_theme');
  apply(saved ? saved === 'dark' : true);
  b.onclick = function () {
    const d = !document.documentElement.classList.contains('dark');
    localStorage.setItem('bonsai_gpt_theme', d ? 'dark' : 'light');
    apply(d);
  };
}
async function loadWorkdir() {
  try {
    const r = await fetch('/api/workdir');
    const j = await r.json();
    if (j.workdir) setWorkdirInUI(j.workdir);
  } catch (e) {}
}
function setWorkdirInUI(wd) {
  workdir = wd;
  const w = document.getElementById('workbtn');
  w.textContent = '\\ud83d\\udcc1 ' + workdir;
  w.title = 'Working folder: ' + workdir;
}
function renderAll() { renderList(); renderConv(); }
function dedupeChats() {
  const byId = {};
  const out = [];
  (chats || []).forEach(function (c) {
    if (!c || !c.id) return;
    const prev = byId[c.id];
    if (!prev) { byId[c.id] = c; out.push(c); return; }
    const a = (c.messages || []).length, b = (prev.messages || []).length;
    if (a > b || (a === b && (c.ts || 0) > (prev.ts || 0))) {
      const i = out.indexOf(prev);
      if (i !== -1) out[i] = c;
      byId[c.id] = c;
    }
  });
  chats = out;
}
function renderList() {
  const el = document.getElementById('chatlist');
  el.innerHTML = '';
  let marked = false;
  chats.slice().sort(function (a, b) { return (b.ts || 0) - (a.ts || 0); }).forEach(function (c) {
    const row = document.createElement('div');
    const isActive = !marked && cur && c.id === cur.id;
    if (isActive) marked = true;
    row.className = 'chat-item' + (isActive ? ' active' : '');
    const t = document.createElement('span'); t.className = 'tit'; t.textContent = c.title; t.title = c.title;
    const d = document.createElement('button'); d.className = 'del'; d.textContent = '\u00d7';
    d.onclick = function (e) {
      e.stopPropagation();
      chats = chats.filter(function (x) { return x.id !== c.id; });
      if (!chats.length) { cur = null; newChat(); }
      else { if (cur && cur.id === c.id) cur = chats[chats.length - 1]; save(); renderAll(); }
    };
    row.onclick = function () { cur = c; started = cur.messages.length > 0; renderAll(); setState('idle'); };
    row.appendChild(t); row.appendChild(d);
    el.appendChild(row);
  });
}
function convInner() { return document.querySelector('#messages .inner'); }
function scrollBottom() { const c = document.getElementById('messages'); c.scrollTop = c.scrollHeight; }
function renderConv() {
  const conv = convInner();
  const empty = document.getElementById('empty');
  conv.innerHTML = '';
  if (!cur || !cur.messages.length) {
    if (empty) { if (!empty.parentNode) conv.appendChild(empty); empty.style.display = 'flex'; }
  } else {
    if (empty) empty.remove();
    cur.messages.forEach(function (m) {
      if (m.role === 'user') addUser(m.content);
      else if (m.role === 'assistant') addAsst(m.content, m.calls || [], m.reason, m.stats);
    });
  }
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
function addUser(content) {
  const row = document.createElement('div'); row.className = 'msgrow user';
  const b = document.createElement('div'); b.className = 'ubub';
  const parts = typeof content === 'string' ? [{ type: 'text', text: content }] : content;
  const arr = parts || [];
  const imgs = arr.filter(function (p) { return p.type === 'image_url'; });
  const files = arr.filter(function (p) { return p.type === 'file_att'; });
  const text = arr.filter(function (p) { return p.type === 'text'; }).map(function (p) { return p.text; }).join(' ');
  if (imgs.length || files.length) {
    const g = document.createElement('div'); g.className = 'thumbs';
    imgs.forEach(function (p) {
      const th = document.createElement('div'); th.className = 'thumb';
      const im = document.createElement('img'); im.src = p.image_url.url; th.appendChild(im); g.appendChild(th);
    });
    files.forEach(function (p) {
      const pf = document.createElement('span'); pf.className = 'pf'; pf.textContent = '\\ud83d\\udcc4 ' + p.name; g.appendChild(pf);
    });
    const sp = document.createElement('div');
    if (text) sp.textContent = text;
    else if (imgs.length && !files.length) sp.textContent = '[Image attached]';
    b.appendChild(g);
    if (sp.textContent) { sp.style.marginTop = '6px'; b.appendChild(sp); }
  } else {
    b.textContent = text;
  }
  row.appendChild(b); convInner().appendChild(row); scrollBottom();
}
function toolResultText(r) {
  if (typeof r === 'string') return r;
  if (!r || typeof r !== 'object') return String(r);
  const copy = {};
  Object.keys(r).forEach(function (k) { if (k !== 'preview' && k !== 'preview_url') copy[k] = r[k]; });
  return JSON.stringify(copy, null, 2);
}
function openPreviewStage(url, name) {
  if (!url) return false;
  const panel = document.getElementById('centerpanel');
  const frame = document.getElementById('stageframe');
  if (panel && frame) {
    frame.src = url;
    const nm = document.getElementById('stagename');
    if (nm) nm.textContent = name || '';
    panel.classList.add('previewing');
    return true;
  }
  const box = document.getElementById('pvbox');
  const lf = document.getElementById('pvframe');
  if (box && lf) {
    lf.src = url;
    const ln = document.getElementById('pvname');
    if (ln) ln.textContent = name || '';
    box.classList.add('on');
    return true;
  }
  window.open(url, '_blank');
  return false;
}
function closePreviewStage() {
  const panel = document.getElementById('centerpanel');
  const frame = document.getElementById('stageframe');
  if (panel && frame) {
    frame.src = 'about:blank';
    panel.classList.remove('previewing');
  }
  const box = document.getElementById('pvbox');
  const lf = document.getElementById('pvframe');
  if (box && lf) {
    lf.src = 'about:blank';
    box.classList.remove('on');
  }
}
const _stageClose = document.getElementById('stageclose');
if (_stageClose) _stageClose.onclick = closePreviewStage;
const _pvClose = document.getElementById('pvclose');
if (_pvClose) _pvClose.onclick = closePreviewStage;
function previewIframeDom(r) {
  const url = r && typeof r === 'object' ? (r.preview_url || '') : '';
  if (!url) return null;
  const w = document.createElement('div');
  w.className = 'tlprev';
  const a = document.createElement('a');
  a.href = url; a.target = '_blank'; a.rel = 'noreferrer';
  a.textContent = 'preview shown in the center stage \u2014 click to open in a new tab';
  a.onclick = function () { openPreviewStage(url, r && (r.path || '')); };
  const b2 = document.createElement('button');
  b2.className = 'tlprevbtn';
  b2.textContent = 'Show';
  b2.title = 'Show it in the center stage';
  b2.onclick = function () { openPreviewStage(url, r && (r.path || '')); };
  w.appendChild(a); w.appendChild(b2);
  return w;
}
function toolItemDom(call) {
  const it = document.createElement('div');
  it.className = 'tlitem' + (call.result && call.result.error ? ' err' : '');
  const nm = document.createElement('div'); nm.textContent = '\u2699 ' + call.name + ' ' + JSON.stringify(call.arguments || {});
  const rs = document.createElement('div'); rs.className = 'rs'; rs.textContent = toolResultText(call.result);
  it.appendChild(nm); it.appendChild(rs);
  const pv = previewIframeDom(call.result);
  if (pv) it.appendChild(pv);
  return it;
}
function toolGroups(calls) {
  const g = [];
  (calls || []).forEach(function (c) {
    const last = g[g.length - 1];
    if (last && last.name === c.name) last.count += 1;
    else g.push({ name: c.name, count: 1, call: c });
  });
  return g;
}
function renderLivePills(container, calls) {
  container.innerHTML = '';
  toolGroups(calls).forEach(function (g) {
    const chip = document.createElement('span'); chip.className = 'chip gear';
    const ok = g.call.result && g.call.result.result === 'ok';
    const bad = g.call.result && g.call.result.error;
    let label = '\u2699\ufe0f ' + g.name + (g.count > 1 ? ' \u00d7' + g.count : '');
    if (ok) { chip.classList.add('ok'); label = '\u2713 ' + g.name + (g.count > 1 ? ' \u00d7' + g.count : ''); }
    else if (bad) { chip.classList.add('err'); label = '\u26a0 ' + g.call.result.error.slice(0, 90); }
    chip.textContent = label;
    container.appendChild(chip);
  });
}
function addToolLog(container, calls) {
  if (!calls || !calls.length) return;
  const d = document.createElement('details'); d.className = 'tlog';
  const s = document.createElement('summary'); s.textContent = 'Tool log \u00b7 ' + calls.length + ' call' + (calls.length === 1 ? '' : 's');
  const l = document.createElement('div'); l.className = 'tl';
  (calls).forEach(function (c) { l.appendChild(toolItemDom(c)); });
  d.appendChild(s); d.appendChild(l); container.appendChild(d);
  const shots = document.createElement('div'); shots.className = 'shots';
  let shotCount = 0;
  (calls).forEach(function (c) {
    if (!(c.preview || c.image_data)) return;
    const sc = shotCardDom(c);
    if (sc) { shots.appendChild(sc); shotCount++; }
  });
  if (shotCount) container.insertBefore(shots, d);
}
function addReasonBox(container, text, live) {
  const d = document.createElement('details'); d.className = 'reasonbox';
  const s = document.createElement('summary'); s.textContent = live ? 'Thinking...' : ('Thinking \u00b7 ' + (text.trim() ? text.trim().length + ' chars' : 'empty'));
  const c = document.createElement('div'); c.className = 'rc'; c.textContent = text;
  d.appendChild(s); d.appendChild(c); container.appendChild(d);
  return { d: d, s: s, c: c };
}
function addStatsChip(container, s) {
  const c = document.createElement('span'); c.className = 'statschip';
  const think = (s.think_ms || 0) / 1000;
  const speak = (s.respond_ms || 0) / 1000;
  let txt = 'THINK ' + think.toFixed(1) + 's \u00b7 SPEAK ' + speak.toFixed(1) + 's';
  if (s.tok_s) txt += ' \u00b7 ' + s.tok_s.toFixed(1) + ' tok/s';
  if (s.completion_tokens) txt += ' \u00b7 ' + s.completion_tokens + ' tok';
  if (s.ctx_used) txt += ' \u00b7 ctx ' + s.ctx_used + '/' + (s.ctx_used + (s.ctx_left || 0));
  c.textContent = txt;
  container.appendChild(c);
}
function addAsst(text, calls, reason, stats) {
  const row = document.createElement('div'); row.className = 'msgrow';
  const b = document.createElement('div');
  if (reason) addReasonBox(b, reason, false);
  if (calls && calls.length) {
    const pills = document.createElement('div'); pills.className = 'toolsline';
    renderLivePills(pills, calls); b.appendChild(pills);
    addToolLog(b, calls);
  }
  const inner = document.createElement('div'); inner.className = 'abody'; inner.innerHTML = fmt(text) || '';
  b.appendChild(inner);
  if (stats) addStatsChip(b, stats);
  row.appendChild(b); convInner().appendChild(row); scrollBottom();
  return inner;
}
function addThinking() {
  liveCalls = [];
  const row = document.createElement('div'); row.className = 'msgrow';
  const b = document.createElement('div');
  const t = document.createElement('div'); t.className = 'thinkstub';
  t.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> thinking...';
  b.appendChild(t);
  row.appendChild(b); convInner().appendChild(row); scrollBottom();
  thinkingRow = { row: row, b: b, body: null, pills: null, tlog: null, reasonD: null };
}
function onDelta(txt) {
  if (!thinkingRow) return;
  if (!thinkingRow.body) {
    const t = thinkingRow.row.querySelector('.thinkstub');
    if (t) t.remove();
    thinkingRow.body = document.createElement('div'); thinkingRow.body.className = 'abody caret';
    thinkingRow.b.appendChild(thinkingRow.body);
  }
  thinkingRow.body.textContent += txt;
  scrollBottom();
}
function onReason(txt) {
  if (!thinkingRow) return;
  if (!thinkingRow.reasonD) thinkingRow.reasonD = addReasonBox(thinkingRow.b, '', true);
  thinkingRow.reasonD.c.textContent += txt;
  thinkingRow.reasonD.c.scrollTop = thinkingRow.reasonD.c.scrollHeight;
  scrollBottom();
}
function onTool(call) {
  if (thinkingRow && thinkingRow.body) thinkingRow.body.classList.remove('caret');
  if (!thinkingRow.pills) { thinkingRow.pills = document.createElement('div'); thinkingRow.pills.className = 'toolsline'; thinkingRow.b.appendChild(thinkingRow.pills); }
  liveCalls.push(call);
  renderLivePills(thinkingRow.pills, liveCalls);
  if (!thinkingRow.tlog) {
    const d = document.createElement('details'); d.className = 'tlog';
    const s = document.createElement('summary'); s.textContent = 'Tool log \u00b7 running...';
    const l = document.createElement('div'); l.className = 'tl';
    d.appendChild(s); d.appendChild(l);
    thinkingRow.tlog = { d: d, s: s, l: l };
    thinkingRow.b.appendChild(d);
  }
  thinkingRow.tlog.s.textContent = 'Tool log \u00b7 ' + liveCalls.length + ' running...';
  thinkingRow.tlog.l.appendChild(toolItemDom(call));
  const live = shotCardDom(call);
  if (live) thinkingRow.b.insertBefore(live, thinkingRow.tlog.d);
  scrollBottom();
}
function doneThinking(errMsg) {
  if (!thinkingRow) return;
  if (thinkingRow.reasonD) {
    const txt = (thinkingRow.reasonD.c.textContent || '').trim();
    thinkingRow.reasonD.s.textContent = 'Thinking \u00b7 ' + (txt ? txt.length + ' chars' : 'empty');
  }
  if (thinkingRow.tlog) thinkingRow.tlog.s.textContent = 'Tool log \u00b7 ' + liveCalls.length + ' call' + (liveCalls.length === 1 ? '' : 's');
  if (thinkingRow.body) { thinkingRow.body.classList.remove('caret'); }
  if (errMsg) { const e = document.createElement('div'); e.style.color = 'var(--err)'; e.style.fontSize = '13px'; e.textContent = errMsg; thinkingRow.b.appendChild(e); }
  thinkingRow = null;
}
function effortValue() { return document.getElementById('effort-sel').value; }
function statLine() { return document.getElementById('statsline'); }
function fmtMs(ms) { if (!ms && ms !== 0) return '--'; return (ms / 1000).toFixed(1) + 's'; }
function runningStats() {
  const el = statLine(); if (!el) return;
  const v = statsVals;
  const ctx = v.ctx_left ? (v.ctx_used) + ' / ' + (v.ctx_used + v.ctx_left) : '--';
  el.textContent = 'THINK ' + fmtMs(v.think_ms || 0) + ' \u00b7 SPEAK ' + fmtMs(v.respond_ms || 0) + ' \u00b7 ' + (v.tok_s ? v.tok_s.toFixed(1) : '--') + ' tok/s \u00b7 ' + (v.completion_tokens || 0) + ' tok \u00b7 ctx ' + ctx;
}
function startStats() {
  statsVals = { think_ms: 0, respond_ms: 0, tok_s: 0, ctx_used: 0, ctx_left: 0, prompt_tokens: 0, completion_tokens: 0 };
  if (statsTimer) clearInterval(statsTimer);
  statsTimer = setInterval(runningStats, 250);
}
function stopStats() { if (statsTimer) { clearInterval(statsTimer); statsTimer = null; } runningStats(); }
function clearStats() { if (statsTimer) { clearInterval(statsTimer); statsTimer = null; } const el = statLine(); if (el) el.textContent = ''; }
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
const SCOPES = ['workspace', 'ask', 'system'];
function postPolicy(body) {
  return fetch('/api/path_policy', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) }).then(function (r) { return r.json(); })
    .then(function (j) { renderPathPolicy(j); return j; }).catch(function () {});
}
function scopeRow(p, label, cls) {
  const row = document.createElement('div'); row.className = 'scoperow ' + cls;
  const t = document.createElement('span'); t.className = 'sp'; t.textContent = p; t.title = label;
  const x = document.createElement('button'); x.className = 'sx'; x.textContent = '\u00d7';
  x.title = 'revoke this ' + label.replace('d', '') + ' - Bonsai will ask again';
  x.onclick = function () { postPolicy({ path: p }); };
  row.appendChild(t); row.appendChild(x);
  return row;
}
function renderPathPolicy(j) {
  const btn = document.getElementById('scopebtn');
  const list = document.getElementById('scopelist');
  const clr = document.getElementById('scopeclear');
  if (!btn) return;
  const pol = (j && j.policy) || 'ask';
  btn.textContent = 'SCOPE: ' + pol.toUpperCase();
  const base = btn.className.split(' ').filter(function (c) { return c && c.indexOf('sc-') !== 0; }).join(' ');
  btn.className = base + ' sc-' + pol;
  if (list) {
    list.innerHTML = '';
    (j && j.grants || []).forEach(function (g) { list.appendChild(scopeRow(g.path, 'approved', 'allow')); });
    (j && j.denies || []).forEach(function (d) { list.appendChild(scopeRow(d.path, 'denied', 'deny')); });
  }
  if (clr) {
    const n = ((j && j.grants) || []).length + ((j && j.denies) || []).length;
    clr.style.display = n ? '' : 'none';
  }
}
function loadPathPolicy() {
  fetch('/api/path_policy').then(function (r) { return r.json(); })
    .then(renderPathPolicy).catch(function () {});
}
function cycleScope() {
  const btn = document.getElementById('scopebtn');
  if (!btn) return;
  const cur = (btn.textContent || '').toLowerCase().replace('scope:', '').trim();
  const i = SCOPES.indexOf(cur);
  postPolicy({ policy: SCOPES[(i + 1 + SCOPES.length) % SCOPES.length] });
}
function bindScope() {
  const btn = document.getElementById('scopebtn');
  if (btn) btn.onclick = cycleScope;
  const clr = document.getElementById('scopeclear');
  if (clr) clr.onclick = function () { postPolicy({ clear: true }); };
  loadPathPolicy();
}
function onAsk(j) {
  const old = document.getElementById('askov');
  if (old) old.remove();
  const ov = document.createElement('div'); ov.className = 'askov'; ov.id = 'askov';
  const box = document.createElement('div'); box.className = 'askbox';
  const perm = j.kind === 'path_approval';
  const h = document.createElement('h4');
  h.textContent = perm ? 'PERMISSION NEEDED' : 'BONSAI IS ASKING YOU';
  box.appendChild(h);
  if (perm) {
    const p = document.createElement('div'); p.className = 'askpath'; p.textContent = j.path || '';
    const w = document.createElement('div'); w.className = 'asktool';
    w.textContent = 'tool: ' + (j.tool || 'file tool') + '  -  outside the workspace';
    box.appendChild(p); box.appendChild(w);
  } else {
    const q = document.createElement('div'); q.className = 'aq'; q.textContent = j.question || 'What should I do?';
    box.appendChild(q);
  }
  const say = function (ans) {
    ov.remove();
    if (perm) loadPathPolicy();
    fetch('/api/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: j.id, answer: ans }) }).catch(function () {});
  };
  if (j.options && j.options.length) {
    const opts = document.createElement('div'); opts.className = 'opts';
    (j.options).forEach(function (o) {
      const b = document.createElement('button');
      b.className = 'opt' + (/^ALLOW/.test(o) ? ' allow' : (/^DENY/.test(o) ? ' deny' : ''));
      b.textContent = o;
      b.onclick = function () { say(o); };
      opts.appendChild(b);
    });
    box.appendChild(opts);
  }
  if (perm) {
    ov.appendChild(box);
    document.body.appendChild(ov);
    return;
  }
  const free = document.createElement('div'); free.className = 'afree';
  const inp = document.createElement('input'); inp.placeholder = 'Type your answer...';
  const btn = document.createElement('button'); btn.textContent = 'SEND';
  const submit = function () { const v = inp.value.trim(); if (v) say(v); };
  btn.onclick = submit;
  inp.onkeydown = function (e) { if (e.key === 'Enter') { e.preventDefault(); submit(); } };
  free.appendChild(inp); free.appendChild(btn);
  box.appendChild(free);
  ov.appendChild(box);
  document.body.appendChild(ov);
  inp.focus();
}
function renderTodo(items) {
  const el = document.getElementById('todopanel');
  if (!el) return;
  el.innerHTML = '';
  if (!items || !items.length) return;
  items.forEach(function (t) {
    const d = document.createElement('span'); d.className = 'todochip';
    const st = document.createElement('span'); st.className = 'st ' + (t.status === 'completed' ? 'ok' : (t.status === 'in_progress' ? 'run' : ''));
    st.textContent = t.status === 'completed' ? '\u2713' : (t.status === 'in_progress' ? '\u25cf' : '\u25cb');
    const tx = document.createElement('span'); tx.textContent = ' ' + (t.description || '');
    d.appendChild(st); d.appendChild(tx); el.appendChild(d);
  });
}
async function streamRun(messages, chat, anchorMsg) {
  chat = chat || cur;
  abortCtrl = new AbortController();
  startStats();
  let resp;
  try {
    resp = await fetch('/api/stream', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages: messages, mode: chatMode, effort: effortValue() }), signal: abortCtrl.signal });
  } catch (e) { stopStats(); throw new Error('stopped'); }
  if (!resp.ok || !resp.body) {
    stopStats();
    const j = await resp.json().catch(function () { return {}; });
    throw new Error(j.error || 'stream unavailable');
  }
  const reader = resp.body.getReader();
  const dec = new TextDecoder();
  let buf = '', reply = '', calls = [], reason = '';
  let aborted = false, gotEnd = false, startedAt = Date.now();
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
        if (ev === 'start') { if (j.workdir) setWorkdirInUI(j.workdir); setState('idle'); }
        else if (ev === 'delta') { reply += j.text; onDelta(j.text); statsVals.respond_ms = Date.now() - startedAt; }
        else if (ev === 'reason') { reason += j.text; onReason(j.text); setState('thinking'); statsVals.think_ms = Date.now() - startedAt; }
        else if (ev === 'tool') { calls.push(j.call); onTool(j.call); setState('tools'); }
        else if (ev === 'stats') { onStats(j); }
        else if (ev === 'ask') { onAsk(j); setState('thinking'); }
        else if (ev === 'todo') { renderTodo(j.todos); }
        else if (ev === 'error') { doneThinking('Error: ' + j.text); setState('idle'); stopStats(); throw new Error(j.text); }
        else if (ev === 'done') { gotEnd = true; }
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
  if (!gotEnd) throw new Error('The reply was cut off unexpectedly - please try again.');
  if (thinkingRow && thinkingRow.body) thinkingRow.body.innerHTML = fmt(reply);
  doneThinking();
  setState('idle');
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
function setState(s) {
  const el = document.getElementById('state-label');
  if (s === 'thinking' || s === 'tools') el.textContent = s === 'thinking' ? 'THINKING' : 'EXECUTING TOOLS';
  else el.textContent = 'READY';
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
  if (busy) { queueNow(); return; }
  go();
}
function setSendUI() {
  const b = document.getElementById('send');
  if (busy) {
    b.innerHTML = STOP_SVG;
    b.className = 'send busy';
    b.title = msgQueue.length ? ('Stop - ' + msgQueue.length + ' message(s) queued') : 'Stop';
  } else { b.innerHTML = SEND_SVG; b.className = 'send'; b.title = 'Send'; }
}
function refreshQueueUI() {
  setSendUI();
}
function clearQueuedBadge(chat, msg) {
  if (!chat || !msg) return;
  if (cur && chat.id !== cur.id) return;
  const all = chat.messages || [];
  const at = all.indexOf(msg);
  if (at < 0) return;
  let k = -1;
  for (let i = 0; i <= at; i++) if (all[i].role === 'user') k++;
  const row = document.querySelectorAll('.msgrow.user')[k];
  if (!row) return;
  row.classList.remove('queued');
  const b = row.querySelector('.qbadge');
  if (b) b.remove();
}
function queueNow() {
  const parts = buildUserMsg();
  if (!parts) return;
  if (!cur) newChat();
  const chat = cur;
  const raw = parts.length === 1 && parts[0].type === 'text' ? parts[0].text : parts;
  const msg = { role: 'user', content: raw, queued: true };
  chat.messages.push(msg);
  msgQueue.push({ parts: parts, chat: chat, msg: msg });
  addUser(parts, true);
  pendingAtt = [];
  renderPreview();
  document.getElementById('user-input').value = '';
  save();
  refreshQueueUI();
  scrollBottom();
}
function requeuePending() {
  msgQueue = msgQueue.filter(function (q) { return q.chat && q.msg && q.msg.queued; });
  if (!cur) return;
  (cur.messages || []).forEach(function (m) {
    if (m.role !== 'user' || !m.queued) return;
    if (msgQueue.some(function (q) { return q.msg === m; })) return;
    const parts = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content || []);
    if (parts.length) msgQueue.push({ parts: parts, chat: cur, msg: m });
  });
  refreshQueueUI();
}
function stopRun() {
  if (abortCtrl) { const a = abortCtrl; abortCtrl = null; try { a.abort(); } catch (e) {} }
  if (thinkingRow) doneThinking('(stopped by user)');
  setState('idle');
}
function drainQueue() {
  if (busy) return;
  if (!msgQueue.length) return;
  const next = msgQueue.shift();
  if (next.msg) delete next.msg.queued;
  clearQueuedBadge(next.chat, next.msg);
  refreshQueueUI();
  go(next.parts, next.chat, true, next.msg);
}
async function go(forcedParts, chat, alreadyAdded, askedMsg) {
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
  let asked = alreadyAdded ? (askedMsg || null) : null;
  if (!asked) { asked = { role: 'user', content: raw }; target.messages.push(asked); addUser(parts); }

  addThinking();
  setState('thinking');
  pendingAtt = [];
  renderPreview();
  document.getElementById('user-input').value = '';
  try { await streamRun(target.messages.filter(function (m) { return !m.queued; }).slice(), target, asked); }
  catch (err) { doneThinking('Error: ' + err.message); setState('idle'); }
  busy = false;
  setSendUI();
  document.getElementById('user-input').focus();
  target.ts = Date.now();
  save();
  if (cur === target) renderConv(); else renderList();
  drainQueue();
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
  const wrap = document.getElementById('messages');
  const ov = document.createElement('div');
  ov.className = 'dropov';
  ov.innerHTML = '<div class="bx">DROP FILES &#128206; TO ATTACH (images / text)</div>';
  document.body.appendChild(ov);
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
document.getElementById('newchat').onclick = function () { newChat(); setState('idle'); };
const _gSend = document.getElementById('send');
if (_gSend) _gSend.onclick = function () {
  if (busy) { stopRun(); return; }
  handleSubmit({ preventDefault: function () {} });
};
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
function renderBlenderStatus(j) {
  const el = document.getElementById('blstatus');
  if (!el) return;
  const map = {
    ok: ['ok', 'Blender MCP: connected - the Blender tools are available.'],
    bridge: ['mid', 'Blender MCP: bridge is up, but the addon is not enabled.'],
    down: ['off', 'Blender MCP: not connected. Open Blender with the addon enabled.'],
    missing: ['off', 'Blender MCP: not installed (pip install mcp-for-blender).']
  };
  const m = map[j.state || 'down'] || map.down;
  el.className = 'blrow ' + m[0];
  const dot = document.getElementById('bldot');
  if (dot) dot.className = 'bldot ' + (j.state === 'ok' ? 'on' : m[0]);
  el.title = j.detail ? (m[1] + ' (' + j.state + ': ' + j.detail + ')') : m[1];
}
function refreshBlender() {
  fetch('/api/blender').then(function (r) { return r.json(); })
    .then(renderBlenderStatus)
    .catch(function () { renderBlenderStatus({ state: 'down' }); });
}
document.getElementById('ejectbtn').onclick = function () {
  const btn = document.getElementById('ejectbtn');
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = 'EJECTING...';
  btn.style.opacity = '0.5';
  fetch('/api/eject', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      btn.textContent = j.ok ? 'EJECTED' : 'FAILED';
      btn.title = j.detail || j.error || (j.ok ? 'Model unloaded - it reloads on the next message' : 'Eject failed');
      if (typeof setModelStatus === 'function') setModelStatus(false, false);
      setTimeout(function () { btn.textContent = 'EJECT'; btn.disabled = false; btn.style.opacity = '1'; }, 2500);
    })
    .catch(function () {
      btn.textContent = 'FAILED';
      btn.title = 'The eject request failed.';
      setTimeout(function () { btn.textContent = 'EJECT'; btn.disabled = false; btn.style.opacity = '1'; }, 2500);
    });
};
/* ---------- TTS toggle (Piper) ---------- */
function renderTtsBtn(j) {
  const el = document.getElementById('ttsbtn');
  if (!el) return;
  el.textContent = j.enabled ? 'TTS: ON' : 'TTS: OFF';
  el.classList.toggle('on', !!j.enabled);
  el.title = 'Text-to-speech (Piper) - ' + (j.enabled ? 'speech enabled' : 'off, click to enable');
}
function refreshTts() {
  fetch('/api/tts')
    .then(function (r) { return r.json(); })
    .then(renderTtsBtn)
    .catch(function () { renderTtsBtn({ enabled: false }); });
}
document.getElementById('ttsbtn').onclick = function () {
  const el = document.getElementById('ttsbtn');
  const next = !el.classList.contains('on');
  fetch('/api/tts', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: next }) })
    .then(function (r) { return r.json(); })
    .then(renderTtsBtn)
    .catch(function () { alert('TTS toggle failed'); });
};
refreshTts();

/* ---------- MODEL selector ---------- */
function modelLabel(m) {
  return (m.type === 'api' ? '\u2601 ' : '\u25C9 ') + (m.label || m.id);
}
function setModelStatus(ready, loading) {
  const t = document.getElementById('system-status-text');
  const dot = document.getElementById('sdot');
  if (dot) dot.className = 'dot' + (loading ? ' busy' : (ready ? '' : ' idle'));
  if (!t) return;
  t.textContent = loading ? 'LOADING MODEL...' : (ready ? 'ONLINE / LOADED' : 'ONLINE / NOT LOADED');
  t.title = loading ? 'The model is being loaded into RAM/VRAM - this only happens on your first message.'
    : (ready ? 'The model is resident in RAM/VRAM.' : 'The model is not loaded. It loads the first time you send a message.');
}
function renderModels(j) {
  const sel = document.getElementById('modelsel');
  if (!sel) return;
  sel.innerHTML = '';
  (j.models || []).forEach(function (m) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = modelLabel(m) + (m.type === 'local' && m.vision ? ' · vision' : '') +
      (m.type === 'local' && m.available === false ? ' (missing)' : '');
    if (m.id === j.active) o.selected = true;
    sel.appendChild(o);
  });
  sel.classList.toggle('off', !j.managed);
  sel.setAttribute('data-prev', j.active || '');
  const badge = document.getElementById('modelbadge');
  if (badge && j.active_label) badge.textContent = j.active_label;
  const vs = document.getElementById('visionbadge');
  if (vs) vs.textContent = j.vision ? 'mmproj ON' : 'mmproj OFF';
  const fm = document.getElementById('footer-model');
  if (fm && j.active_label) fm.textContent = j.active_label;
  const fc = document.getElementById('footer-caps');
  if (fc) fc.textContent = (j.vision ? 'VISION' : 'TEXT-ONLY') + '+TOOLS';
  if (typeof setModelStatus === 'function') setModelStatus(!!j.ready, !!j.loading);
  window.__lastModels = j;
  renderMmprojList(j);
}
function renderMmprojList(j) {
  const sel = document.getElementById('mmprojfor');
  const hint = document.getElementById('mmprojhint');
  if (!sel) return;
  const keep = sel.value;
  sel.innerHTML = '';
  (j.models || []).filter(function (m) { return m.type === 'local'; })
    .forEach(function (m) {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = (m.label || m.id) + (m.vision ? '  (vision on)' : '');
      sel.appendChild(o);
    });
  if (keep) sel.value = keep;
  const cur = (j.models || []).filter(function (m) { return m.id === sel.value; })[0];
  if (hint) {
    hint.textContent = !cur ? 'No local models yet.'
      : (cur.vision ? 'Projector: ' + String(cur.mmproj).split(/[\\/]/).pop()
                    : 'No projector attached - this model is text-only.');
  }
}
function setMmproj(mmproj) {
  const sel = document.getElementById('mmprojfor');
  if (!sel || !sel.value) { alert('Add a local model first.'); return; }
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'mmproj', id: sel.value, mmproj: mmproj }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) { alert('Could not set the projector:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j);
    })
    .catch(function () { alert('Could not set the projector'); });
}
function refreshModels() {
  fetch('/api/models')
    .then(function (r) { return r.json(); })
    .then(renderModels)
    .catch(function () {});
}
document.getElementById('modelsel').onchange = function () {
  const sel = document.getElementById('modelsel');
  const id = sel.value;
  const prev = sel.getAttribute('data-prev') || '';
  sel.disabled = true;
  sel.title = 'Switching model - a local model restarts the model server, this can take up to a minute...';
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'select', id: id }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      sel.disabled = false;
      renderModels(j);
      if (!j.ok) { alert('Could not switch model: ' + (j.error || 'unknown error')); sel.value = prev; }
    })
    .catch(function () { sel.disabled = false; sel.value = prev; alert('Model switch failed'); });
};
/* ---------- ADD MODEL modal (native file / folder browser) ---------- */
function openAddMdl() { const m = document.getElementById('addmdl'); if (m) m.classList.add('on'); }
function closeAddMdl() { const m = document.getElementById('addmdl'); if (m) m.classList.remove('on'); }
function afterAdd(j, what) {
  if (j.cancelled) return;
  if (!j.ok) { alert('Could not add the model:\\n' + (j.error || 'unknown error')); return; }
  renderModels(j);
  if (what === 'folder') {
    alert('Added ' + ((j.added && j.added.length) || 0) + ' model(s) from that folder' +
          (j.skipped ? ' (' + j.skipped + ' already in the list)' : '') +
          '.\\n\\nPick the one you want from the dropdown.');
  } else if (what === 'file') {
    alert('Model added. Pick it from the dropdown to use it.');
  }
  closeAddMdl();
}
function addModelPick(what) {
  const btn = document.getElementById('addmodelbtn');
  if (btn) { btn.disabled = true; btn.textContent = '...'; }
  fetch('/api/pick_model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ what: what }) })
    .then(function (r) { return r.json(); })
    .then(function (j) { afterAdd(j, what); })
    .catch(function () { alert('Could not open the file browser'); })
    .then(function () { if (btn) { btn.disabled = false; btn.textContent = '+'; } });
}
function addModelApi() {
  const base = (document.getElementById('apibase').value || '').trim();
  const model = (document.getElementById('apimodel').value || '').trim();
  if (!base || !model) { alert('Fill in both the base URL and the model id.'); return; }
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'add', type: 'api', label: model, base_url: base, model: model }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) { alert('Could not add the model:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j); closeAddMdl();
      document.getElementById('apibase').value = '';
      document.getElementById('apimodel').value = '';
    })
    .catch(function () { alert('Add failed'); });
}
document.getElementById('addmodelbtn').onclick = openAddMdl;
const _mdlFile = document.getElementById('mdl-file');
if (_mdlFile) _mdlFile.onclick = function () { addModelPick('file'); };
const _mdlFolder = document.getElementById('mdl-folder');
if (_mdlFolder) _mdlFolder.onclick = function () { addModelPick('folder'); };
const _mdlApi = document.getElementById('mdl-api');
if (_mdlApi) _mdlApi.onclick = addModelApi;
const _mmprojBtn = document.getElementById('mdl-mmproj');
if (_mmprojBtn) _mmprojBtn.onclick = function () {
  const sel = document.getElementById('mmprojfor');
  if (!sel || !sel.value) { alert('Add a local model first.'); return; }
  _mmprojBtn.disabled = true; _mmprojBtn.textContent = '...';
  fetch('/api/pick_model', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ what: 'mmproj', id: sel.value }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j.cancelled) return;
      if (!j.ok) { alert('Could not set the projector:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j);
    })
    .catch(function () { alert('Could not open the file browser'); })
    .then(function () { _mmprojBtn.disabled = false; _mmprojBtn.innerHTML = 'Browse&hellip;'; });
};
const _mmprojClear = document.getElementById('mdl-mmproj-clear');
if (_mmprojClear) _mmprojClear.onclick = function () { setMmproj(''); };
const _mmprojSel = document.getElementById('mmprojfor');
if (_mmprojSel) _mmprojSel.onchange = function () { renderMmprojList(window.__lastModels || { models: [] }); };
const _mdlClose = document.getElementById('addmdl-close');
if (_mdlClose) _mdlClose.onclick = closeAddMdl;
const _mdl = document.getElementById('addmdl');
if (_mdl) _mdl.onclick = function (ev) { if (ev.target === _mdl) closeAddMdl(); };
/* ---------- MODEL SETTINGS modal ---------- */
function cfgFill(j) {
  const models = (j && j.models) || [];
  const act = models.filter(function (m) { return m.id === (j && j.active); })[0];
  const cfg = (act && act.cfg) || (j && j.defaults) || { ctx: 32768, temp: 1, top_p: 0.95, top_k: 20, ngl: 99 };
  const set = function (id, v) { const e = document.getElementById(id); if (e) e.value = v; };
  set('cfg_ctx', cfg.ctx); set('cfg_temp', cfg.temp);
  set('cfg_top_p', cfg.top_p); set('cfg_top_k', cfg.top_k); set('cfg_ngl', cfg.ngl);
  const who = document.getElementById('cfgwho');
  if (who) who.textContent = act ? ('- ' + (act.label || act.id)) : '';
  const hint = document.getElementById('cfghint');
  if (hint) {
    hint.textContent = !act ? 'No local model selected.'
      : (act.type === 'api' ? 'External endpoints are configured by their own server.'
      : 'Saved settings are used the next time this model is loaded (after a switch or EJECT).');
  }
}
function openCfgMdl() {
  const m = document.getElementById('cfgmdl');
  if (!m) return;
  cfgFill(window.__lastModels);
  m.classList.add('on');
}
function closeCfgMdl() { const m = document.getElementById('cfgmdl'); if (m) m.classList.remove('on'); }
const _cfgBtn = document.getElementById('cfgbtn');
if (_cfgBtn) _cfgBtn.onclick = openCfgMdl;
const _cfgClose = document.getElementById('cfg-close');
if (_cfgClose) _cfgClose.onclick = closeCfgMdl;
const _cfgMdl = document.getElementById('cfgmdl');
if (_cfgMdl) _cfgMdl.onclick = function (ev) { if (ev.target === _cfgMdl) closeCfgMdl(); };
const _cfgDefaults = document.getElementById('cfg-defaults');
if (_cfgDefaults) _cfgDefaults.onclick = function () {
  const j = window.__lastModels || {};
  const d = j.defaults || { ctx: 32768, temp: 1, top_p: 0.95, top_k: 20, ngl: 99 };
  const set = function (id, v) { const e = document.getElementById(id); if (e) e.value = v; };
  set('cfg_ctx', d.ctx); set('cfg_temp', d.temp);
  set('cfg_top_p', d.top_p); set('cfg_top_k', d.top_k); set('cfg_ngl', d.ngl);
};
const _cfgSave = document.getElementById('cfg-save');
if (_cfgSave) _cfgSave.onclick = function () {
  const j = window.__lastModels || {};
  const act = (j.models || []).filter(function (m) { return m.id === j.active; })[0];
  if (!act) { alert('No model selected.'); return; }
  const num = function (id) {
    const e = document.getElementById(id);
    return e && e.value !== '' ? Number(e.value) : undefined;
  };
  const cfg = { ctx: num('cfg_ctx'), temp: num('cfg_temp'), top_p: num('cfg_top_p'),
                top_k: num('cfg_top_k'), ngl: num('cfg_ngl') };
  _cfgSave.disabled = true;
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'config', id: act.id, cfg: cfg }) })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (!res.ok) { alert('Could not save the settings:\\n' + (res.error || 'unknown error')); return; }
      renderModels(res);
      cfgFill(res);
      const hint = document.getElementById('cfghint');
      if (hint) hint.textContent = 'Saved. These values apply the next time this model is loaded.';
    })
    .catch(function () { alert('Could not save the settings'); })
    .then(function () { _cfgSave.disabled = false; });
};
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') closeAddMdl();
});
refreshModels();
const inp = document.getElementById('user-input');
inp.addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 160) + 'px'; });
inp.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); handleSubmit(ev); } });
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
init();
</script>
<div class="pvbox" id="pvbox">
  <div class="pvboxhd">
    <b>LIVE PREVIEW</b>
    <span id="pvname"></span>
    <button class="pvboxx" id="pvclose" title="Close the preview">&times;</button>
  </div>
  <iframe id="pvframe" class="pvboxframe" title="HTML preview" sandbox="allow-scripts"></iframe>
</div>
</body>
</html>"""


def _strip_full(record):
    """The tool payload streamed to the UI.

    The big base64 image lives under `image_data` internally (so it is easy to
    pull out and feed to the model) but the UIs render a `preview` key, so
    expose it under that name too - that is what makes a screenshot appear in
    the chat automatically, with no help from the model."""
    if isinstance(record, dict):
        out = dict(record)
        out.pop("full_result", None)
        img = out.get("image_data")
        if img and not out.get("preview"):
            out["preview"] = img
        return out
    return record


CLIENT_GONE = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


def sse(handler, event, obj):
    data = json.dumps(obj, ensure_ascii=False)
    try:
        handler.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        handler.wfile.flush()
    except CLIENT_GONE:
        # the browser closed the tab / stopped reading - nothing to do
        raise


def _valid_chat(c):
    return (isinstance(c, dict) and isinstance(c.get("messages"), list)
            and all(isinstance(m, dict) and m.get("role") in
                    ("user", "assistant", "system", "tool")
                    for m in c["messages"]))


def _read_chat_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    return [c for c in data if _valid_chat(c)]


_SYSINFO_CACHE = {"ts": 0, "data": {}}
_SYSINFO_TTL = 2.0


def _gpu_percent():
    """GPU utilisation from nvidia-smi (cached; None when unavailable)."""
    try:
        exe = shutil.which("nvidia-smi")
        if not exe:
            return None
        out = subprocess.run(
            [exe, "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4,
            creationflags=CREATE_NO_WINDOW).stdout
        vals = [int(x) for x in out.replace("%", "").split() if x.strip().isdigit()]
        if not vals:
            return None
        return max(0, min(100, round(sum(vals) / len(vals))))
    except Exception:
        return None


def _sample_sysinfo():
    data = {"cpu": None, "ram": None, "gpu": None,
            "cpu_cores": os.cpu_count()}
    try:
        import psutil
        data["cpu"] = int(round(psutil.cpu_percent(interval=None)))
        vm = psutil.virtual_memory()
        data["ram"] = int(round(vm.percent))
        data["ram_total"] = int(round(vm.total / (1024 ** 3)))
    except Exception:
        data["cpu"] = None
    data["gpu"] = _gpu_percent()
    _SYSINFO_CACHE["ts"] = time.time()
    _SYSINFO_CACHE["data"] = data
    return data


def _sysinfo_loop():
    """Samples CPU/RAM/GPU on one dedicated thread.

    psutil's cpu_percent() averages since its previous call and returns 0.0
    the first time it is called from a new thread, so the sampling has to stay
    on a single thread instead of running inside request handlers."""
    try:
        import psutil
        psutil.cpu_percent(interval=None)  # discard the first sample
    except Exception:
        pass
    while True:
        try:
            _sample_sysinfo()
        except Exception:
            pass
        time.sleep(1.5)


def _sysinfo():
    """Latest real CPU / RAM / GPU usage for the HUD meters."""
    d = _SYSINFO_CACHE.get("data") or {}
    if d:
        return d
    return {"cpu": None, "ram": None, "gpu": None,
            "cpu_cores": os.cpu_count()}


def _load_chats():
    data = _read_chat_file(CHATS_FILE)
    if data is None:
        data = _read_chat_file(CHATS_FILE + ".bak")
    return data if isinstance(data, list) else []


def _save_chats(chats):
    if not isinstance(chats, list):
        return False
    keep = [c for c in chats if _valid_chat(c)][-200:]
    tmp = CHATS_FILE + ".tmp"
    try:
        os.makedirs(os.path.dirname(CHATS_FILE), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f, ensure_ascii=False, default=str)
        if os.path.exists(CHATS_FILE):
            try:
                os.replace(CHATS_FILE, CHATS_FILE + ".bak")
            except Exception:
                pass
        os.replace(tmp, CHATS_FILE)
        return True
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except CLIENT_GONE:
            self.close_connection = True

    def _serve_preview(self):
        qs = self.path.partition("?")[2]
        params = dict(urllib.parse.parse_qsl(qs))
        rel = params.get("path", "")
        if not rel:
            self._send(400, "preview: missing 'path'", "text/plain")
            return
        try:
            target = _safe_path(rel)
        except (ValueError, PermissionError) as exc:
            self._send(400, str(exc), "text/plain")
            return
        if not target.lower().endswith((".html", ".htm")):
            self._send(400, "preview: only .html / .htm files can be previewed",
                       "text/plain")
            return
        if not os.path.isfile(target):
            self._send(404, "file not found", "text/plain")
            return
        try:
            with open(target, "rb") as fh:
                data = fh.read()
        except Exception as exc:
            self._send(500, "preview read error: %s" % exc, "text/plain")
            return
        try:
            text = data.decode("utf-8")
            text = _inject_base(text, _preview_base_href(target))
            data = text.encode("utf-8")
        except Exception:
            pass
        self._send(200, data, "text/html; charset=utf-8")

    def _serve_preview_asset(self, rel):
        """Serve a non-HTML file from the workspace for a previewed page."""
        rel = urllib.parse.unquote(rel or "").lstrip("/")
        if not rel:
            self._send(400, "preview: missing file", "text/plain")
            return
        try:
            target = _safe_path(rel)
        except (ValueError, PermissionError) as exc:
            self._send(400, str(exc), "text/plain")
            return
        ext = os.path.splitext(target)[1].lower()
        if ext in (".html", ".htm"):
            self._send(400, "preview: HTML files are served from /preview",
                       "text/plain")
            return
        ctype = _PREVIEW_MIME.get(ext)
        if not ctype:
            self._send(415, "preview: unsupported asset type", "text/plain")
            return
        if not os.path.isfile(target):
            self._send(404, "file not found", "text/plain")
            return
        try:
            with open(target, "rb") as fh:
                data = fh.read()
        except Exception as exc:
            self._send(500, "preview read error: %s" % exc, "text/plain")
            return
        self._send(200, data, ctype)

    def do_GET(self):
        _touch_activity()
        path = self.path.split("?")[0]
        if path == "/":
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/chat":
            self._send(200, PAGE_GPT, "text/html; charset=utf-8")
        elif path == "/api/workdir":
            self._send(200, json.dumps({"workdir": WORKDIR}))
        elif path == "/api/chats":
            self._send(200, json.dumps({"chats": _load_chats()}, default=str))
        elif path == "/api/todos":
            with _TODOS_LOCK:
                self._send(200, json.dumps({"todos": _TODOS}))
        elif path == "/api/blender":
            self._send(200, json.dumps(_blender_status_payload()))
        elif path == "/api/tts":
            self._send(200, json.dumps({"enabled": tts_enabled(),
                                        "folder": PIPER_DIR}))
        elif path == "/api/models":
            self._send(200, json.dumps(_public_models(), default=str))
        elif path == "/api/sysinfo":
            self._send(200, json.dumps(_sysinfo(), default=str))
        elif path == "/api/path_policy":
            self._send(200, json.dumps(_path_state(), default=str))
        elif path == "/api/sched_results":
            self._send(200, json.dumps({"events": _sched_take_events()}, default=str))
        elif path == "/preview":
            self._serve_preview()
        elif path.startswith("/previewfile/"):
            self._serve_preview_asset(path[len("/previewfile/"):])
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        _touch_activity()
        try:
            path = self.path.split("?")[0]
            if path not in ("/api", "/api/stream", "/api/workdir",
                            "/api/pick_workdir", "/api/chats",
                            "/api/answer", "/api/effort", "/api/eject",
                            "/api/tts", "/api/models", "/api/pick_model",
                            "/api/path_policy"):
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_REQUEST_BYTES:
                self._send(413, json.dumps(
                    {"error": "request too large (max %d MB)"
                               % (MAX_REQUEST_BYTES // (1024 * 1024))}))
                return
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
            if path == "/api/path_policy":
                if body.get("clear"):
                    state = _path_forget()
                else:
                    state = _path_forget(body.get("path")) if body.get("path") \
                        else _path_policy_set(body.get("policy"))
                self._send(200, json.dumps(state, default=str))
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
            if path == "/api/tts":
                enabled = set_tts_enabled(body.get("enabled"))
                self._send(200, json.dumps({"ok": True, "enabled": enabled}))
                return
            if path == "/api/models":
                action = str(body.get("action") or "select").strip().lower()
                if action == "add":
                    res = _add_model(body)
                elif action == "remove":
                    res = _remove_model(str(body.get("id") or ""))
                elif action == "mmproj":
                    res = _set_model_mmproj(str(body.get("id") or ""),
                                            body.get("mmproj"))
                elif action == "config":
                    res = _set_model_config(str(body.get("id") or ""),
                                            body.get("cfg") or {})
                elif action == "scan":
                    res = _scan_models()
                else:
                    res = _select_model(str(body.get("id") or ""))
                self._send(200, json.dumps(res, default=str))
                return
            if path == "/api/pick_model":
                what = str(body.get("what") or "file").strip().lower()
                if what == "mmproj":
                    chosen = _pick_vision_file()
                    if not chosen:
                        self._send(200, json.dumps({"cancelled": True}))
                        return
                    self._send(200, json.dumps(
                        _set_model_mmproj(str(body.get("id") or ""), chosen),
                        default=str))
                    return
                chosen = (_pick_model_folder() if what == "folder"
                          else _pick_model_file())
                if not chosen:
                    self._send(200, json.dumps({"cancelled": True}))
                    return
                if what == "folder":
                    res = _add_model_folder(chosen)
                else:
                    res = _add_model(
                        {"type": "local", "path": chosen,
                         "label": os.path.splitext(
                             os.path.basename(chosen))[0],
                         "mmproj": _mmproj_for(os.path.dirname(chosen),
                                               chosen)})
                    if res.get("ok") and isinstance(res.get("added"), dict):
                        res["added"] = [res["added"]]
                    res.setdefault("skipped", 0)
                self._send(200, json.dumps(res, default=str))
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
                                      "kind": q.get("kind") or "question",
                                      "question": q.get("question", ""),
                                      "path": q.get("path") or "",
                                      "tool": q.get("tool") or "",
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
                try:
                    handle_chat(messages or [], stream=True, mode=mode,
                                on_reason=lambda t: sse(self, "reason", {"text": t}),
                                on_delta=lambda t: sse(self, "delta", {"text": t}),
                                on_tool=lambda c: sse(self, "tool", {"call": _strip_full(c)}),
                                on_stats=lambda s: sse(self, "stats", s),
                                hooks={"on_ask": do_ask, "on_todo": do_todo})
                except Exception as exc:
                    traceback.print_exc()
                    try:
                        sse(self, "error", {"text": "The model run failed: " + str(exc)})
                    except Exception:
                        pass
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


class BonsaiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address):
        """A browser that closes a tab mid-response is normal, not an error."""
        exc = sys.exc_info()[1]
        if isinstance(exc, CLIENT_GONE + (TimeoutError, OSError)):
            return
        traceback.print_exc()


def main():
    try:
        os.makedirs(WORKDIR, exist_ok=True)
    except Exception as exc:
        print(f"WARN: could not create workdir {WORKDIR}: {exc}", flush=True)

    # The model server is deliberately NOT started here: it loads on the first
    # message you send, so starting the UI costs no VRAM until you actually
    # use it (see _ensure_bonsai, which the chat endpoints call).
    threading.Thread(target=_blender_kickoff, daemon=True).start()
    threading.Thread(target=_sysinfo_loop, daemon=True,
                     name="bonsai-sysinfo").start()
    _sched_load()
    print("BONSAI is READY on http://127.0.0.1:8081", flush=True)
    webbrowser.open(f"http://{HOST}:{PORT}")
    BonsaiServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()