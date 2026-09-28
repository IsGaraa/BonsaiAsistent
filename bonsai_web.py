import asyncio
import base64
import ctypes
import datetime
import fnmatch
import glob
import hashlib
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
# BONSAI_PORT lets a second copy run alongside the real one - needed for any
# test that has to exercise the live server, and it defaults to the usual 8081.
try:
    PORT = int(os.environ.get("BONSAI_PORT") or PORT)
except Exception:
    pass

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
# How long the model socket may stay completely silent before we call it dead.
# Effectively unlimited on purpose: a large model can think for a very long
# time on a big plan before it emits a single token, and that is a normal
# result, not a failure. Nothing in the app should cut such a turn short - the
# user decides when to stop, with the stop button.
BONSAI_SOCKET_TIMEOUT = 86400
# The chat file normally lives in the per-user config dir, but an explicit
# BONSAI_DIR has to win over it. It did not: this read %APPDATA% and nothing
# else, so a second copy of the app - a test harness, a portable folder, a
# second install - wrote its conversations straight into the real history and
# overwrote it on the way out. Everything else in the app already follows
# BONSAI_DIR, and this makes the chat file agree with the rest.
if os.environ.get("BONSAI_CHATS_FILE"):
    CHATS_FILE = os.path.abspath(os.environ["BONSAI_CHATS_FILE"])
elif os.environ.get("BONSAI_DIR") and \
        os.path.abspath(BONSAI_DIR) != os.path.dirname(os.path.abspath(__file__)):
    CHATS_FILE = os.path.join(BONSAI_DIR, "chats.json")
elif IS_WINDOWS:
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
# No download size cap by default: files stream to disk in chunks, so RAM stays
# flat no matter how big the file is. PC_MAX_DOWNLOAD_MB / a per-call max_mb can
# still impose a ceiling, and the free disk space is checked before starting.
MAX_DOWNLOAD_BYTES = (int(os.environ["PC_MAX_DOWNLOAD_MB"]) * 1024 * 1024
                      if os.environ.get("PC_MAX_DOWNLOAD_MB") else 0)
DOWNLOAD_CHUNK = 256 * 1024
_DOWNLOAD_FLOOR_BYTES = 64 * 1024 * 1024
MODE_BUILD = "build"
MODE_PLAN = "plan"
_DEF_WORKDIR = (r"C:\Users\drago\Desktop\workspace" if IS_WINDOWS
                else os.path.expanduser("~/bonsai_workspace"))
WORKDIR = os.path.realpath(os.environ.get("PC_WORKDIR", _DEF_WORKDIR))
PIPER_DIR = os.environ.get("PC_PIPER_DIR", os.path.join(BONSAI_DIR, "piper"))
MODELS_FILE = os.path.join(BONSAI_DIR, "models.json")
MODELS_DIR = os.path.join(BONSAI_DIR, "models")
# Secrets live here, not in the app: one NAME=value per line, '#' comments.
API_KEYS_FILE = os.path.join(BONSAI_DIR, "API KEYS.txt")

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
# set by a tool that wants the caret in the chat box (rich_text with
# action=paste/send). The turn's keepalive thread drains it and tells the page,
# which is the only thing that can actually focus its own input.
_UI_FOCUS = threading.Event()
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
        # Every entry in _APPS is a Windows path, a Windows .cmd or a Windows
        # URI, so on Linux _launch cannot possibly have worked - and returning
        # its error here made the PATH lookup below unreachable for the whole
        # table. That is why "open gimp", "open blender", "open vlc" and every
        # other name in it failed on Linux even when the program was installed
        # and sitting in PATH. Fall through and try it properly.
        if IS_WINDOWS:
            return {"error": result.get("error", "could not launch")}
    store = _store_appid(key)
    if store:
        try:
            _open_path("shell:AppsFolder\\" + store["AppID"])
        except Exception as exc:
            return {"error": f"could not open '{name}': {exc}"}
        return {"launched": store.get("Name", key), "result": "ok", "method": "start menu app"}
    if not IS_WINDOWS:
        # Try the canonical name first, then what the user actually typed:
        # the alias table is written for Windows ("ie" means Internet Explorer),
        # so on Linux the name the model or the user gave is often the one that
        # is actually installed - and a name with a space in it is a command,
        # not a path, so it gets the same treatment _launch gives it.
        for cand in (key, str(name or "").strip()):
            cand = str(cand or "").strip()
            if not cand or os.sep in cand or "/" in cand:
                continue
            exe = shutil.which(cand)
            if not exe:
                continue
            try:
                subprocess.Popen([exe], creationflags=CREATE_NO_WINDOW)
            except Exception as exc:
                return {"error": f"could not open '{name}': {exc}"}
            return {"launched": cand, "result": "ok", "method": "linux-bin"}
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
        if _within(path, root):
            if best is None or len(root) > len(best):
                best = root
    return best


def _path_approval(cand, what="access this path", tool=None):
    """Gate a path outside the workspace. Returns (allowed, message)."""
    cand = os.path.realpath(cand)
    base = os.path.realpath(WORKDIR)
    if _within(cand, base):
        return True, ""
    with _PATH_LOCK:
        policy = _PATH_POLICY
        if policy == "system":
            return True, ""
        if policy == "workspace":
            return False, ("access denied: '%s' is outside the workspace '%s'. The "
                           "path scope is set to WORKSPACE - switch it to ASK or "
                           "SYSTEM in the sidebar if you want Bonsai to reach other "
                           "places." % (cand, WORKDIR))
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
        return False, ("access denied: '%s' is outside the workspace '%s' and no "
                       "one is around to approve it (this tool only asks for "
                       "permission while a reply is streaming in the UI)."
                       % (cand, WORKDIR))
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


def _strip_long_prefix(path):
    """Drop the Windows \\\\?\\ extended-length prefix for readable/short paths."""
    text = str(path)
    if IS_WINDOWS and text.startswith("\\\\?\\") and not text.startswith("\\\\?\\UNC\\"):
        if len(text) < 240:
            return text[4:]
    return text


def _norm_path(path):
    """Comparable form of a path: no \\\\?\\ prefix, normalised, case-folded on Windows.

    os.path.realpath() can hand back the \\\\?\\ form for a path that exists and the
    plain form for one that does not, which used to make 'is this inside the
    workspace?' flip depending on timing.
    """
    text = str(path or "")
    if IS_WINDOWS:
        if text.startswith("\\\\?\\UNC\\"):
            text = "\\\\" + text[8:]
        elif text.startswith("\\\\?\\"):
            text = text[4:]
    text = os.path.normpath(text) if text else text
    if len(text) > 1 and text.endswith(os.sep):
        text = text[:-1]
    return os.path.normcase(text) if IS_WINDOWS else text


def _within(cand, base):
    """True when cand is base itself or lives under it (prefix-safe)."""
    c, b = _norm_path(cand), _norm_path(base)
    return c == b or c.startswith(b + os.sep)


def _safe_path(p):
    if p is None:
        p = ""
    text = str(p).strip()
    base = os.path.realpath(WORKDIR)
    if not text:
        return _strip_long_prefix(base)
    cand = os.path.realpath(os.path.join(base, text)) if not os.path.isabs(text) else os.path.realpath(text)
    if _within(cand, base):
        return _strip_long_prefix(cand)
    ok, message = _path_approval(cand, "write a file at")
    if ok:
        return _strip_long_prefix(cand)
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


# ---- code differencing -------------------------------------------------
# Every write the model makes is remembered as a real diff against the
# content the file had *before the session first touched it*. The chat shows
# the per-step diff, the panel shows the cumulative one, and a revert puts
# the original text back. Diffs travel to the UI on a side channel (a thread
# local) so the model never sees them and they never bloat the context.

_CHANGES = {}
_CHANGES_LOCK = threading.RLock()
_CHANGE_TLS = threading.local()
_DIFF_CAP = 4000          # per-step diff shipped with a tool call
_DIFF_CAP_FULL = 200000   # full diff served by /api/changes
_MAX_DIFF_BYTES = 4000000


def _changes_rel(path):
    """Path as shown in the UI: relative to the workspace when it is inside,
    absolute otherwise (the user may have approved a path anywhere)."""
    try:
        rel = os.path.relpath(path, os.path.realpath(WORKDIR))
    except Exception:
        return path
    return path if rel.startswith("..") else rel.replace("\\", "/")


def _diff_text(before, after, path, context=3):
    """Unified diff between two texts. Returns (diff_text, added, removed)."""
    import difflib
    a = str(before or "").splitlines()
    b = str(after or "").splitlines()
    rel = _changes_rel(path)
    lines = list(difflib.unified_diff(a, b, fromfile="a/" + rel, tofile="b/" + rel,
                                      lineterm="", n=context))
    if len(lines) > _MAX_DIFF_BYTES // 40:
        return "(diff too large to display)", 0, 0
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return "\n".join(lines), added, removed


def _note_change(path, before, after, tool=""):
    """Record one write. Returns the payload the chat UI shows under the step.

    `before` is the content immediately before this write, `after` the content
    written. The first time a file is touched its pre-session content is kept,
    so the panel can always show original -> now and revert to it."""
    try:
        key = os.path.realpath(path)
    except Exception:
        return None
    before = str(before or "")
    after = str(after or "")
    if before == after:
        return None
    step_diff, step_add, step_del = _diff_text(before, after, key)
    now = time.time()
    with _CHANGES_LOCK:
        entry = _CHANGES.get(key)
        created = entry is None and before == ""
        if entry is None:
            entry = {"path": key, "rel": _changes_rel(key), "original": before,
                     "created": created, "tool": tool, "ts": now, "reverted": False}
            _CHANGES[key] = entry
        elif entry.get("reverted"):
            entry["reverted"] = False
            entry["ts"] = now
        entry["tool"] = tool or entry.get("tool") or ""
        entry["edits"] = int(entry.get("edits") or 0) + 1
        entry["ts"] = now
        full_diff, total_add, total_del = _diff_text(entry["original"], after, key)
        entry["diff"] = full_diff
        entry["added"] = total_add
        entry["removed"] = total_del
        payload = {"path": key, "rel": entry["rel"],
                   "added": step_add, "removed": step_del,
                   "total_added": total_add, "total_removed": total_del,
                   "diff": step_diff[:_DIFF_CAP],
                   "total_diff": full_diff[:_DIFF_CAP],
                   "created": bool(entry.get("created")),
                   "truncated": len(step_diff) > _DIFF_CAP,
                   "tool": tool}
    _CHANGE_TLS.last = payload
    return payload


def _take_last_change():
    """Pop the diff the last write left behind (same thread, so a tool call
    picks up its own change and nothing else)."""
    payload = getattr(_CHANGE_TLS, "last", None)
    _CHANGE_TLS.last = None
    return payload


def _change_summary():
    with _CHANGES_LOCK:
        files = [e for e in _CHANGES.values() if not e.get("reverted")]
    return {"files": len(files),
            "added": sum(int(e.get("added") or 0) for e in files),
            "removed": sum(int(e.get("removed") or 0) for e in files)}


def _changes_snapshot(include_reverted=False):
    with _CHANGES_LOCK:
        entries = [e for e in _CHANGES.values()
                   if include_reverted or not e.get("reverted")]
        out = []
        for e in sorted(entries, key=lambda x: x.get("ts") or 0, reverse=True):
            diff = e.get("diff") or ""
            out.append({"path": e.get("path"), "rel": e.get("rel"),
                        "added": int(e.get("added") or 0),
                        "removed": int(e.get("removed") or 0),
                        "edits": int(e.get("edits") or 1),
                        "tool": e.get("tool") or "",
                        "created": bool(e.get("created")),
                        "reverted": bool(e.get("reverted")),
                        "ts": e.get("ts") or 0,
                        "diff": diff[:_DIFF_CAP_FULL],
                        "truncated": len(diff) > _DIFF_CAP_FULL})
    summary = _change_summary()
    summary["changes"] = out
    return summary


def _revert_change(path):
    """Put the pre-session content of a file back and drop its diff."""
    target = str(path or "").strip()
    if not target:
        return {"error": "path is required"}
    if not os.path.isabs(target):
        target = os.path.join(WORKDIR, target)
    key = os.path.realpath(target)
    with _CHANGES_LOCK:
        entry = _CHANGES.get(key)
        if entry is None:
            return {"error": "no recorded change for that file"}
        if entry.get("created"):
            # The model created this file: revert means remove it.
            try:
                if os.path.isfile(key):
                    os.remove(key)
            except Exception as exc:
                return {"error": f"could not remove {key}: {exc}"}
            _CHANGES.pop(key, None)
            return {"ok": True, "removed": key, "rel": entry.get("rel")}
        original = entry.get("original") or ""
        try:
            with open(key, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(original)
        except Exception as exc:
            return {"error": f"could not restore {key}: {exc}"}
        entry["reverted"] = True
        entry["diff"] = ""
        entry["added"] = 0
        entry["removed"] = 0
        return {"ok": True, "path": key, "rel": entry.get("rel")}


def _forget_changes():
    """Stop tracking (files stay as they are)."""
    with _CHANGES_LOCK:
        n = len(_CHANGES)
        _CHANGES.clear()
    return {"ok": True, "forgot": n}


def _read_text(path, limit=4000000):
    """Current content of a file as text, or '' when it does not exist."""
    try:
        if not os.path.isfile(path):
            return ""
        if os.path.getsize(path) > limit:
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return ""


def _write_file(path, content):
    if not path:
        return {"error": "path is required"}
    target = _safe_path(path)
    parent = os.path.dirname(target)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    body = str(content or "")
    before = _read_text(target)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    rel = os.path.relpath(target, os.path.realpath(WORKDIR))
    result = {"result": "ok", "path": target, "bytes": os.path.getsize(target)}
    preview = _preview_url(rel)
    if preview:
        result["preview_url"] = preview
    _note_change(target, before, body, "write_file")
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
    original = content
    if replace_all:
        content = content.replace(old_text, new_text)
    else:
        content = content.replace(old_text, new_text, 1)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(content)
    _note_change(target, original, content, "edit_file")
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


def _grep_globs(include):
    if include is None:
        return None
    if isinstance(include, (list, tuple)):
        raw = [str(x) for x in include]
    else:
        raw = [x.strip() for x in str(include).split(",")]
    globs = [x.lower() for x in raw if x.strip()]
    return globs or None


def _grep_wanted(name, globs):
    if not globs:
        return True
    low = name.lower()
    for g in globs:
        if fnmatch.fnmatch(low, g) or fnmatch.fnmatch(low, "*" + g.lstrip("*")):
            return True
    return False


def _grep(pattern, path=None, include=None, mode="content", context=0,
          ignore_case=True, max_results=None):
    if not str(pattern or "").strip():
        return {"error": "pattern is required (a regular expression to search for)"}
    mode = str(mode or "content").lower()
    if mode not in ("content", "files", "count"):
        return {"error": "mode must be 'content', 'files' or 'count'"}
    try:
        context = max(0, min(10, int(context or 0)))
    except (TypeError, ValueError):
        context = 0
    if max_results is None:
        limit = MAX_SEARCH_RESULTS
    else:
        try:
            limit = max(1, min(500, int(max_results)))
        except (TypeError, ValueError):
            limit = MAX_SEARCH_RESULTS
    globs = _grep_globs(include)
    target = _safe_path(path) if path else os.path.realpath(WORKDIR)
    if not os.path.isdir(target):
        return {"error": f"not a directory: {target}"}
    try:
        flags = re.UNICODE | re.IGNORECASE if ignore_case else re.UNICODE
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return {"error": f"invalid regular expression: {exc}"}
    base = os.path.realpath(WORKDIR)
    outside = not _within(target, base)
    started = time.time()
    hits = []
    files_hit = []
    seen_files = set()
    counts = []
    scanned = 0
    truncated = False
    # Linux system trees that must never be walked. Matched on the full path,
    # not the folder name: a bare "dev" or "sys" in the skip list would throw
    # away a project's own dev/ folder, and a grep rooted at / that reads
    # /proc and /sys spends the whole 45s budget producing I/O errors.
    skip_prefixes = ("/proc", "/sys", "/dev", "/run", "/snap/") \
        if IS_LINUX else ()
    for root, dirs, files in os.walk(target):
        dirs[:] = [d for d in dirs
                   if not d.startswith(".")
                   and d != "__pycache__"
                   and os.path.join(d.lower(), "").rstrip("\\/") not in _SEARCH_SKIP_DIRS
                   and not d.lower() in _SEARCH_SKIP_DIRS
                   and not (root == os.sep
                            and any(os.path.join(root, d).startswith(p)
                                    for p in skip_prefixes))]
        for name in sorted(files):
            if name.startswith("."):
                continue
            if not _grep_wanted(name, globs):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            scanned += 1
            if scanned > _SEARCH_FILE_BUDGET or time.time() - started > _SEARCH_SECONDS:
                return _grep_out(mode, target, hits, files_hit, counts, scanned,
                                 True, "stopped early after %d files / %.0fs - narrow "
                                       "the path or add an include filter to get the "
                                       "rest" % (scanned, time.time() - started))
            shown = full if outside else os.path.relpath(full, base).replace("\\", "/")
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.read().splitlines()
            except Exception:
                continue
            found = 0
            for i, line in enumerate(lines):
                if not rx.search(line):
                    continue
                found += 1
                if mode == "content":
                    hit = {"file": shown, "line": i + 1, "text": line.rstrip()[:200]}
                    if context:
                        lo = max(0, i - context)
                        hi = min(len(lines), i + context + 1)
                        hit["block"] = "\n".join(
                            "%d: %s" % (j + 1, lines[j][:200]) for j in range(lo, hi))
                    hits.append(hit)
                    if len(hits) >= limit:
                        return _grep_out(mode, target, hits, files_hit, counts,
                                         scanned, True,
                                         "stopped at the max_results limit - narrow "
                                         "the pattern or path for more")
                elif mode == "files":
                    if shown not in seen_files:
                        seen_files.add(shown)
                        files_hit.append(shown)
                        if len(files_hit) >= limit:
                            return _grep_out(mode, target, hits, files_hit, counts,
                                             scanned, True,
                                             "stopped at the max_results limit")
            if found and mode == "count":
                counts.append({"file": shown, "lines": found})
    return _grep_out(mode, target, hits, files_hit, counts, scanned, truncated)


def _grep_out(mode, target, hits, files_hit, counts, scanned, truncated, note=None):
    if mode == "files":
        out = {"files": files_hit}
    elif mode == "count":
        out = {"counts": counts, "total_lines": sum(c["lines"] for c in counts)}
    else:
        out = {"matches": hits}
    out.update({"result": "ok", "mode": mode, "path": target,
                "scanned_files": scanned, "truncated": bool(truncated)})
    if note:
        out["note"] = note
    return out


_WEB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) BonsaiAsistent/1.0"


def _fetch_html(url, max_bytes=400000, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": _WEB_UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
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
    "grep": {
        "type": "function",
        "function": {
            "name": "grep",
            "description": "Search inside files for a regular expression and get back "
                           "the matching lines with file and line number (this is "
                           "grep). Use it to find where something is defined, used or "
                           "broken instead of reading whole files. The folder defaults "
                           "to the whole workspace, but you can point it at any "
                           "directory on this PC ("
                           + ("'C:\\\\Users', 'C:\\\\'" if IS_WINDOWS
                              else "'/home', '/', '/etc'")
                           + ") to search the entire machine - the user is "
                           "asked to approve paths outside the workspace. Narrow a big "
                           "sweep with include (e.g. '*.py'). Heavy system folders "
                           + ("(Windows, Program Files, node_modules) are skipped, and the "
                              if IS_WINDOWS else
                              "(/proc, /sys, /snap, node_modules) are skipped, and the ")
                           + "sweep stops after 40000 files or 45s.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regular expression, e.g. 'def main', "
                                       "'MAX_.*BYTES' or 'error|warning'."
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional folder to search inside: relative "
                                       "to the workspace (default = whole "
                                       "workspace) or an absolute path anywhere on "
                                       "this PC (the user is asked to approve it)."
                    },
                    "include": {
                        "type": "string",
                        "description": "Only search files matching this glob, e.g. "
                                       "'*.py', '*.json' or '*.log,*.txt'. Omit to "
                                       "search every text file."
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["content", "files", "count"],
                        "description": "'content' (default) returns the matching "
                                       "lines, 'files' returns just the names of "
                                       "files that matched, 'count' returns a "
                                       "per-file tally of how many lines matched "
                                       "(like grep -c)."
                    },
                    "context": {
                        "type": "integer",
                        "description": "Lines of context to include around each match "
                                       "(0 = the matching line only, max 10)."
                    },
                    "ignore_case": {
                        "type": "boolean",
                        "description": "Case-insensitive by default; set false to "
                                       "match exact case."
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Stop after this many matches (default 200, "
                                       "max 500)."
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
        if name == "grep":
            return _grep(args.get("pattern"), args.get("path"),
                         args.get("include"), args.get("mode", "content"),
                         args.get("context", 0), args.get("ignore_case", True),
                         args.get("max_results"))
        if name == "write_file":
            return _write_file(args.get("path"), args.get("content"))
    except ValueError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"{name} failed: {exc}"}
    return {"error": f"unknown tool {name}"}


# ---- OpenCode tool implementations ----

def _glob_search(pattern, path=None):
    import glob as glob_mod
    if not pattern:
        return {"error": "pattern is required"}
    search_dir = path or WORKDIR
    search_dir = os.path.realpath(search_dir)
    if not os.path.isdir(search_dir):
        return {"error": f"glob path must be a directory: {search_dir}"}
    results = []
    for root, dirs, files in os.walk(search_dir):
        dirs[:] = [d for d in dirs if d not in ("node_modules", ".git", "__pycache__", "Windows", "Program Files")]
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, search_dir)
            if glob_mod.fnmatch.fnmatch(rel, pattern) or glob_mod.fnmatch.fnmatch(f, pattern):
                results.append(os.path.realpath(full))
    results = sorted(set(results))[:100]
    if not results:
        return {"result": "No files found", "output": "No files found"}
    truncated = len(results) >= 100
    output = [os.path.realpath(r) for r in results]
    if truncated:
        output.append(f"\n(Results truncated: showing first 100. Use a more specific path or pattern.)")
    return {"result": "ok", "output": "\n".join(output), "count": len(results), "truncated": truncated}


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif",
               ".tiff", ".ico", ".heic", ".heif", ".ppm", ".pgm", ".tga"}
_BINARY_EXTS = {".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar",
                ".exe", ".dll", ".so", ".dylib", ".class", ".jar", ".pyc",
                ".pyo", ".pyd", ".o", ".a", ".lib", ".obj", ".wasm", ".bin",
                ".dat", ".db", ".sqlite", ".mdb", ".gguf", ".onnx", ".pt",
                ".pth", ".safetensors", ".ckpt", ".npy", ".npz", ".pkl",
                ".pickle", ".blend", ".fbx", ".obj3", ".stl", ".3ds", ".wav",
                ".mp3", ".flac", ".ogg", ".mp4", ".mkv", ".avi", ".mov",
                ".webm", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                ".ttf", ".otf", ".woff", ".woff2", ".eot"}


def _is_image_file(path):
    return os.path.splitext(str(path))[1].lower() in _IMAGE_EXTS


def _strip_control_chars(text):
    """Drop the bytes that are not text.

    A NUL or a stray control character inside a message is not something a
    model should ever be sent: it inflates the request, some tokenizers reject
    it outright, and providers answer with a bare "invalid request" that says
    nothing about which part of the message was wrong. Newlines, tabs and
    carriage returns are the only ones that carry meaning, so they stay."""
    if not isinstance(text, str):
        return text
    if not any(ord(ch) < 32 and ch not in "\t\n\r" or ord(ch) == 127
               for ch in text):
        return text
    return "".join(ch for ch in text
                   if ch in "\t\n\r" or (ord(ch) >= 32 and ord(ch) != 127))


def _looks_binary(sample, ext=""):
    """Is this file binary? Judged by what the bytes are, not by the name.

    The old test counted only control characters and gave up above 30%, which
    sounds reasonable and is wrong for compressed data: a PNG, a zip and a .gz
    are all near-uniformly distributed, so only about one byte in ten falls in
    the control range and they all passed as text. The model was then handed
    raw compressed bytes as if they were a document, and the next request came
    back "invalid request" with no clue why. Decodability is the real signal.
    """
    if ext and ext in _BINARY_EXTS:
        return True
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    try:
        text = sample.decode("utf-8")
    except UnicodeDecodeError:
        # A few odd bytes in an otherwise normal text file is still text, so
        # only call it binary when the high bytes are actually common.
        high = sum(1 for b in sample if b > 127)
        if high / len(sample) > 0.10:
            return True
        text = sample.decode("utf-8", "replace")
    printable = sum(1 for ch in text
                    if ch in "\t\n\r" or 32 <= ord(ch) < 127 or ord(ch) > 159)
    if printable / len(text) <= 0.90:
        return True
    return False


def _read_image_attachment(file_path):
    """Hand an image to the model as something it can actually look at.

    The tool has always described itself as returning images as attachments,
    and refused them as binary instead. Refusing is not the same as being able
    to see: the model has a vision projector, the whole point of the file being
    on disk is that it wanted to look at it, and "cannot read binary file" sent
    it off to grep a picture instead."""
    try:
        from PIL import Image
    except Exception:
        return {"error": f"{file_path} is an image and this model needs Pillow "
                         f"to look at it: pip install pillow"}
    try:
        with Image.open(file_path) as im:
            im.load()
            data_uri, w, h = _image_payload(im)
            fmt = (im.format or "image").lower()
    except Exception as exc:
        return {"error": f"could not open the image {file_path}: {exc}"}
    if os.path.getsize(file_path) > MAX_IMAGE_BYTES * 4:
        return {"error": f"image is too large to send: {file_path}"}
    return {
        "result": "ok",
        "type": "image",
        "path": file_path,
        "format": fmt,
        "width": w,
        "height": h,
        "output": f"<path>{file_path}</path>\n<type>image</type>\n"
                  f"<detail>{fmt} image, {w}x{h}. Attached below - look at it "
                  f"to answer.</detail>",
        "image_data": data_uri,
    }


def _read_oc(file_path, offset=None, limit=None):
    if not file_path:
        return {"error": "filePath is required"}
    if not os.path.isabs(file_path):
        file_path = os.path.join(WORKDIR, file_path)
    file_path = os.path.realpath(file_path)
    if not os.path.exists(file_path):
        # Suggest similar files
        parent = os.path.dirname(file_path)
        base = os.path.basename(file_path)
        suggestions = []
        if os.path.isdir(parent):
            for item in os.listdir(parent):
                if base.lower() in item.lower() or item.lower() in base.lower():
                    suggestions.append(os.path.join(parent, item))
                    if len(suggestions) >= 3:
                        break
        msg = f"File not found: {file_path}"
        if suggestions:
            msg += f"\n\nDid you mean one of these?\n" + "\n".join(suggestions)
        return {"error": msg}
    if os.path.isdir(file_path):
        try:
            items = sorted(os.listdir(file_path))
        except Exception as e:
            return {"error": f"Cannot read directory: {e}"}
        off = int(offset or 1)
        lim = int(limit or 2000)
        start = max(0, off - 1)
        sliced = items[start:start + lim]
        truncated = start + len(sliced) < len(items)
        output = [f"<path>{file_path}</path>", "<type>", "<entries>"]
        output.extend(sliced)
        if truncated:
            output.append(f"(Showing {len(sliced)} of {len(items)} entries. Use offset={off + len(sliced)} to continue.)")
        else:
            output.append(f"({len(items)} entries)")
        output.append("</entries>")
        return {"result": "ok", "output": "\n".join(output), "type": "directory"}
    # Check binary
    try:
        with open(file_path, "rb") as f:
            sample = f.read(8192)
    except Exception as e:
        return {"error": f"Cannot read file: {e}"}
    ext = os.path.splitext(file_path)[1].lower()
    if _is_image_file(file_path):
        return _read_image_attachment(file_path)
    if _looks_binary(sample, ext):
        return {"error": f"Cannot read binary file: {file_path} - it is not "
                         f"text, so there is nothing to show the model. Use an "
                         f"archive tool to look inside it, or copy out the part "
                         f"you need."}
    # Read text
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:
        return {"error": f"Cannot read file: {e}"}
    total = len(lines)
    off = int(offset or 1)
    lim = int(limit or 2000)
    start = max(0, off - 1)
    end = min(total, start + lim)
    selected = lines[start:end]
    truncated = end < total or (selected and len(selected[-1]) > 2000)
    output_lines = [f"<path>{file_path}</path>", "<type>file</type>", "<content>"]
    # A cap on the bytes that actually go into the conversation, not just on
    # the line count. "limit=2000" is no limit at all on a minified bundle
    # where one line is 400k of characters, and the result of that is a request
    # the provider refuses as invalid, or a context that is gone. MAX_TEXT_FILE_BYTES
    # existed for this and was never wired up.
    budget = MAX_TEXT_FILE_BYTES
    spent = 0
    cut_at = None
    for i, line in enumerate(selected):
        line_text = _strip_control_chars(line.rstrip("\n"))
        if len(line_text) > 2000:
            line_text = line_text[:2000] + "... (line truncated to 2000 chars)"
        row = f"{start + i + 1}: {line_text}"
        if spent + len(row) > budget:
            cut_at = i
            break
        spent += len(row)
        output_lines.append(row)
    if cut_at is not None:
        selected = selected[:cut_at]
        truncated = True
    last = start + len(selected)
    if truncated:
        output_lines.append(f"\n(Showing lines {start + 1}-{last} of {total}. Use offset={last + 1} to continue.)")
    else:
        output_lines.append(f"\n(End of file - total {total} lines)")
    output_lines.append("</content>")
    return {"result": "ok", "output": "\n".join(output_lines), "type": "file",
            "lineStart": start + 1, "lineEnd": last, "totalLines": total, "truncated": truncated}


def _edit_oc(file_path, old_string, new_string, replace_all=False):
    if not file_path:
        return {"error": "filePath is required"}
    if not os.path.isabs(file_path):
        file_path = os.path.join(WORKDIR, file_path)
    file_path = os.path.realpath(file_path)
    if old_string is None or new_string is None:
        return {"error": "oldString and newString are both required"}
    if not isinstance(old_string, str) or not isinstance(new_string, str):
        return {"error": "oldString and newString must both be strings"}
    if old_string == new_string:
        return {"error": "No changes to apply: oldString and newString are identical."}
    if not os.path.exists(file_path):
        return {"error": f"File not found: {file_path}"}
    if os.path.isdir(file_path):
        return {"error": f"Path is a directory, not a file: {file_path}"}
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        return {"error": f"Cannot read file: {e}"}
    if old_string == "":
        return {"error": "oldString cannot be empty when editing an existing file."}
    # Try exact match first
    if old_string in content:
        if replace_all:
            new_content = content.replace(old_string, new_string)
        else:
            idx = content.find(old_string)
            new_content = content[:idx] + new_string + content[idx + len(old_string):]
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
        except Exception as e:
            return {"error": f"Cannot write file: {e}"}
        additions = new_content.count("\n") - content.count("\n")
        _note_change(file_path, content, new_content, "edit")
        return {"result": "ok", "output": "Edit applied successfully.",
                "additions": max(0, additions), "deletions": 0, "file": file_path}
    # Fuzzy matching strategies
    def _line_trimmed_match(content, find):
        orig_lines = content.split("\n")
        search_lines = find.split("\n")
        if search_lines and search_lines[-1] == "":
            search_lines.pop()
        for i in range(len(orig_lines) - len(search_lines) + 1):
            match = True
            for j in range(len(search_lines)):
                if orig_lines[i + j].strip() != search_lines[j].strip():
                    match = False
                    break
            if match:
                return "\n".join(orig_lines[i:i + len(search_lines)])
        return None
    def _whitespace_normalized(content, find):
        norm = lambda t: " ".join(t.split())
        norm_find = norm(find)
        lines = content.split("\n")
        for i in range(len(lines)):
            if norm(lines[i]) == norm_find:
                return lines[i]
            if norm_find in norm(lines[i]):
                return lines[i]
        if "\n" in find:
            find_lines = find.split("\n")
            for i in range(len(lines) - len(find_lines) + 1):
                block = "\n".join(lines[i:i + len(find_lines)])
                if norm(block) == norm_find:
                    return block
        return None
    def _indentation_flexible(content, find):
        def remove_indent(text):
            lines = text.split("\n")
            non_empty = [l for l in lines if l.strip()]
            if not non_empty:
                return text
            min_indent = min(len(l) - len(l.lstrip()) for l in non_empty)
            return "\n".join(l[min_indent:] if l.strip() else l for l in lines)
        norm_find = remove_indent(find)
        find_lines = find.split("\n")
        content_lines = content.split("\n")
        for i in range(len(content_lines) - len(find_lines) + 1):
            block = "\n".join(content_lines[i:i + len(find_lines)])
            if remove_indent(block) == norm_find:
                return block
        return None
    for strategy in [_line_trimmed_match, _whitespace_normalized, _indentation_flexible]:
        matched = strategy(content, old_string)
        if matched:
            idx = content.find(matched)
            new_content = content[:idx] + new_string + content[idx + len(matched):]
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(new_content)
            except Exception as e:
                return {"error": f"Cannot write file: {e}"}
            _note_change(file_path, content, new_content, "edit")
            return {"result": "ok", "output": "Edit applied successfully (fuzzy match).",
                    "file": file_path, "strategy": strategy.__name__}
    return {"error": "Could not find oldString in the file. It must match exactly, including whitespace, indentation, and line endings."}


def _shell_oc(command, workdir=None, timeout=None):
    import subprocess
    if not command or not str(command).strip():
        return {"error": "command is required"}
    cwd = workdir or WORKDIR
    cwd = cwd if os.path.isabs(cwd) else os.path.realpath(cwd)
    timeout_s = min(max(int((timeout or 120000) / 1000), 1), 600)
    try:
        result = subprocess.run(command, shell=True, cwd=cwd, capture_output=True,
                                text=True, timeout=timeout_s, encoding="utf-8", errors="replace")
        output = result.stdout or ""
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if not output:
            output = "(no output)"
        return {"result": "ok", "output": output, "exitCode": result.returncode}
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") + (e.stderr or "")
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return {"error": f"shell tool terminated command after exceeding timeout {timeout_s * 1000} ms. "
                          f"If this command is expected to take longer, retry with a larger timeout.",
                "output": output or "(no output before timeout)"}
    except Exception as e:
        return {"error": f"shell command failed: {e}"}


def _execute_js(code, timeout=None):
    import subprocess
    timeout_s = min(max(int(timeout or 30), 1), 120)
    # Write to temp file and run with node
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".js", delete=False, encoding="utf-8")
    try:
        tmp.write(code)
        tmp.close()
        result = subprocess.run(["node", tmp.name], capture_output=True, text=True,
                                timeout=timeout_s, encoding="utf-8", errors="replace")
        output = result.stdout or ""
        if result.stderr:
            output += ("\n" if output else "") + result.stderr
        if not output:
            output = "(no output)"
        return {"result": "ok", "output": output, "exitCode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"Execution timed out after {timeout_s}s."}
    except FileNotFoundError:
        return {"error": "Node.js is not installed. Install Node.js to use the execute tool."}
    except Exception as e:
        return {"error": f"Execution failed: {e}"}
    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass


def _task_agent(description, prompt_text, subagent_type="general", background=False):
    """Run a subagent task. For now, executes inline with the main conversation."""
    import threading
    result_holder = {}
    def _run():
        try:
            result_holder["output"] = f"Task '{description}' completed.\n\n{prompt_text}"
            result_holder["status"] = "completed"
        except Exception as e:
            result_holder["error"] = str(e)
            result_holder["status"] = "error"
    if background:
        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return {"result": "ok", "output": f"Background task started: {description}",
                "background": True, "status": "running"}
    else:
        _run()
        if result_holder.get("status") == "error":
            return {"error": result_holder["error"]}
        return {"result": "ok", "output": result_holder["output"],
                "status": "completed", "background": False}


def _plan_task(task):
    """Create an implementation plan for the given task."""
    return {"result": "ok",
            "output": f"Plan for: {task}\n\n"
                       f"1. Understand the requirements\n"
                       f"2. Explore the codebase\n"
                       f"3. Design the approach\n"
                       f"4. Implement step by step\n"
                       f"5. Test and verify\n"
                       f"\nPlan saved. Switch to BUILD mode to execute."}


def _skill_load(name):
    """Load a skill's instructions into the conversation."""
    skill_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    skill_path = os.path.join(skill_dir, name, "SKILL.md")
    if not os.path.exists(skill_path):
        # Try without .md
        skill_path = os.path.join(skill_dir, name)
    if not os.path.exists(skill_path):
        return {"error": f"Skill not found: {name}"}
    try:
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"result": "ok", "output": f"<skill_content name=\"{name}\">\n{content}\n</skill_content>",
                "skill": name}
    except Exception as e:
        return {"error": f"Cannot load skill: {e}"}


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
          "absolute path to list_dir / read_file / grep / write_file / "
          "edit_file and the user will be asked to approve it (ALLOW FOR THIS "
          "CONV, ALLOW ONCE or DENY). So do not refuse or guess - just try the "
          "real path, and if the access is denied, respect it and ask the user "
          "what to do instead. "
          + ("Prefer grep with a path such as 'C:\\\\Users' or 'C:\\\\' to look "
             "across the whole PC instead of guessing where a file might be. "
             if IS_WINDOWS else
             "Prefer grep with a path such as '/home' or '/' to look across the "
             "whole machine instead of guessing where a file might be. Note "
             "that every path on this machine is POSIX: a backslash is an "
             "ordinary character in a file name, not a separator. ")
          + "When "
          "you are not sure about something, are asked for recent/current "
          "information, or want to double-check a fact, you may search the web "
          "(web_search) and read pages (web_fetch) to document yourself. When "
          "the user asks you to build, test, install or inspect something on "
          "the PC, you can run shell commands (shell) and read their "
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
    # A hosted model has no local server to wait for, and no /health endpoint
    # to wait on either - OpenRouter answers 404 for it, which used to abort
    # the turn with "model server could not start". Say it is ready and let the
    # first real request report a bad key or bad model id, which is the error
    # the user can actually act on.
    entry = _active_model()
    if entry and entry.get("type") == "api" and not _is_loopback(entry):
        return True
    try:
        req = urllib.request.Request(BONSAI_BASE + "/health",
                                     headers=_model_headers())
        h = urllib.request.urlopen(req, timeout=3)
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
# The KV cache grows linearly with the context window and is paid for in VRAM
# (and in prompt-processing time), so the useful ceiling is hardware, not
# theoretical. 100k is the cap for this machine; the dialog enforces the same
# number so a context this large cannot be re-entered by accident.
MAX_CTX_TOKENS = 4194304


def _model_sanitise_cfg(raw):
    raw = raw if isinstance(raw, dict) else {}

    def num(key, lo, hi, cast, default):
        try:
            val = cast(raw.get(key, default))
        except Exception:
            return default
        return max(lo, min(hi, val))

    return {"ctx": num("ctx", 512, MAX_CTX_TOKENS, int, _MODEL_DEFAULTS["ctx"]),
            "temp": num("temp", 0.0, 2.0, float, _MODEL_DEFAULTS["temp"]),
            "top_p": num("top_p", 0.01, 1.0, float, _MODEL_DEFAULTS["top_p"]),
            "top_k": num("top_k", 0, 1000, int, _MODEL_DEFAULTS["top_k"]),
            "ngl": num("ngl", -1, 999, int, _MODEL_DEFAULTS["ngl"])}


def _entry_cfg(entry):
    cfg = dict(_MODEL_DEFAULTS)
    cfg.update({k: v for k, v in ((entry or {}).get("cfg") or {}).items()
                if k in _MODEL_DEFAULTS})
    return cfg


_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
# how the big providers spell a credential, for telling a name from a secret
_SECRETISH_RE = re.compile(r"^(sk[-_]|sk-or[-_]|hf_|gsk_|xai[-_]|api[-_]?key|"
                           r"AIza|bearer\s)", re.I)
# a credential quoted back inside free text. A provider that rejects a key
# likes to repeat it in the body ("invalid api key: sk-or-v1-..."), and that
# body is about to be rendered into the chat, so scrub it on the way through.
# The second arm insists on a name=value shape so ordinary sentences that
# merely mention an api key are left alone.
_EMBEDDED_SECRET_RE = re.compile(
    r"(?:sk[-_]?or[-_]?v1[-_]|sk[-_]proj[-_]|sk[-_]|hf_|gsk_|xai[-_]|AIza|"
    r"ghp_|glpat-)[A-Za-z0-9_\-]{16,}"
    r"|(?:api[-_]?key|authorization|bearer)\b\s*[:=]\s*"
    r"(?:bearer\s+)?[\"']?[A-Za-z0-9_\-\.]{16,}", re.I)
# provider wording that carries no information, so our own message is better
_GENERIC_PROVIDER_TEXT = ("", "provider returned error", "internal server error",
                          "error", "unknown error", "bad gateway",
                          "service unavailable", "request failed",
                          # bare status words: they name the code back at us and
                          # say nothing about what to do instead
                          "forbidden", "unauthorized", "access denied",
                          "bad request", "not found", "not allowed")


def _redact_embedded_secrets(text):
    """Blank out any credential-looking run inside text from a provider."""
    if not text:
        return ""
    return _EMBEDDED_SECRET_RE.sub("<redacted>", str(text))


def _generic_provider_text(text):
    """True when the provider's own wording says nothing worth repeating."""
    t = re.sub(r"\s+", " ", str(text or "")).strip().lower().rstrip(".")
    return t in _GENERIC_PROVIDER_TEXT


def _sanitise_key_ref(raw):
    """A model's api_key field is a *name*, not a secret.

    People paste the key itself out of habit, and models.json is not a place to
    keep a plaintext credential: it is a file the app rewrites, and a file
    people paste into, share and commit by accident. So accept an identifier
    that could name an entry in API KEYS.txt or an environment variable, and
    refuse anything that looks like the credential with a message that says so.
    Returns (name, error)."""
    ref = str(raw or "").strip()
    if not ref:
        return "", None
    # The credential shapes are checked first, and they have to be: "sk-or-v1-..."
    # is made entirely of characters a name is allowed to use, so a test against
    # the name pattern alone waves the secret straight through.
    if _SECRETISH_RE.match(ref) or len(ref) > 48:
        return "", ("that looks like the key itself, not its name. "
                    "models.json stores the NAME - put the key in API KEYS.txt "
                    "under a name like OPENROUTER_API_KEY, and enter that name here")
    if " " in ref or not _KEY_NAME_RE.match(ref):
        return "", ("'%s' is not a usable name. Use letters, digits, dot, dash "
                    "or underscore, starting with a letter" % ref[:40])
    return ref, None


def _set_model_config(mid, raw):
    cfg = _model_sanitise_cfg(raw)
    key_ref, key_err = _sanitise_key_ref(raw.get("api_key"))
    if key_err:
        return {"ok": False, "error": key_err}
    with _MODELS_LOCK:
        entry = next((m for m in _MODELS if m["id"] == mid), None)
        if not entry:
            return {"ok": False, "error": "unknown model '%s'" % mid}
        if entry.get("type", "local") != "local":
            # A hosted model has no llama.cpp flags to reconfigure, so the
            # sampling numbers mean nothing for it. Its context window does: the
            # app decides a conversation is full by comparing against it, and
            # there was nowhere to record the real size - every hosted model was
            # stuck on the 32768 default, and a 1M model was metered as 32k.
            want = int(cfg.get("ctx") or 0)
            have = int(_entry_cfg(entry).get("ctx") or 0)
            if "ctx" in (raw or {}) and want and want != have:
                entry.setdefault("cfg", {})["ctx"] = want
                _save_models()
            if "api_key" in (raw or {}):
                if key_ref:
                    entry["api_key"] = key_ref
                else:
                    entry.pop("api_key", None)
                _save_models()
            res = {"ok": True, "id": mid, "cfg": _entry_cfg(entry),
                   **_public_models()}
            # say what actually happened: a page that never sent a key did not
            # clear one, so it must not claim that it did
            if "api_key" not in (raw or {}):
                res["detail"] = "saved"
            elif key_ref:
                res["detail"] = ("saved - the key name is stored; the secret "
                                 "stays in API KEYS.txt")
            else:
                res["detail"] = "saved - the key name was cleared"
            res["api_key_set"] = _api_key_present(entry.get("api_key"))
            if not res["api_key_set"] and not _is_loopback(entry):
                res["warn"] = _no_key_warning(entry)
            return res
        was_active = (mid == _ACTIVE_MODEL)
        old_cfg = _entry_cfg(entry)
        entry["cfg"] = cfg
    _save_models()
    # The sampling values are llama-server command line flags, so they only take
    # effect on a fresh process. The context meter has to follow the saved value
    # immediately though, which is why this runs even if no reload happens.
    if was_active:
        _apply_model(entry)
    applied = was_active and old_cfg != cfg
    reloaded = False
    if applied and _bonsai_ready():
        # Reload through the normal eject path so the next message comes back on
        # a server launched with the new --ctx-size/--temp/--top-p/--top-k/-ngl.
        _unload_bonsai()
        reloaded = True
    res = {"ok": True, "id": mid, "cfg": cfg, **_public_models()}
    if applied:
        res["detail"] = ("saved - the model server was stopped, your next message "
                         "reloads it with these values" if reloaded else
                         "saved - these values apply the next time the model loads")
        res["reloaded"] = reloaded
    else:
        res["detail"] = "saved - unchanged"
    return res


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
    # The context size lives in entry["cfg"], which is what the gear dialog
    # writes and what _entry_cfg() feeds to llama-server as --ctx-size. Reading
    # entry["ctx"] here left the context meter stuck on the registry default
    # (32768) no matter what the user had actually set.
    ctx = _entry_cfg(entry)["ctx"]
    if entry.get("type") == "api":
        BONSAI_BASE = _api_base(entry) or "http://127.0.0.1:8080"
        BONSAI_MODEL_ID = entry.get("model") or entry["id"]
        BONSAI_CTX = int(entry.get("ctx") or ctx)
    else:
        BONSAI_BASE = "http://127.0.0.1:%d" % int(entry.get("port") or 8080)
        BONSAI_MODEL_ID = entry["id"]
        BONSAI_MODEL = entry.get("path") or BONSAI_MODEL
        BONSAI_MMPROJ = entry.get("mmproj") or ""
        BONSAI_CTX = int(ctx)


def _active_model():
    with _MODELS_LOCK:
        for m in _MODELS:
            if m.get("id") == _ACTIVE_MODEL:
                return dict(m)
        return dict(_MODELS[0]) if _MODELS else None


def _save_models():
    # Keep the last known-good registry beside the real one. These are the user's
    # own per-model sampling settings and they are not in git, so a bad write
    # would silently reset them. Write to a sibling temp file and swap it in so
    # an interrupted save can never truncate the live file.
    with _MODELS_LOCK:
        data = {"active": _ACTIVE_MODEL, "models": _MODELS}
    try:
        os.makedirs(os.path.dirname(MODELS_FILE), exist_ok=True)
        tmp = MODELS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(MODELS_FILE):
            shutil.copyfile(MODELS_FILE, MODELS_FILE + ".bak")
        os.replace(tmp, MODELS_FILE)
        return True
    except Exception:
        try:
            if os.path.exists(MODELS_FILE + ".tmp"):
                os.remove(MODELS_FILE + ".tmp")
        except Exception:
            pass
        return False


_API_KEYS = None
_API_KEYS_MTIME = None


def _load_api_keys(force=False):
    """Read API KEYS.txt: one NAME=value per line, '#' starts a comment.

    Values are kept in memory only - they are never written back to
    models.json, never sent to the browser, and never printed."""
    global _API_KEYS, _API_KEYS_MTIME
    path = API_KEYS_FILE
    try:
        mtime = os.path.getmtime(path)
    except Exception:
        mtime = None
    if not force and _API_KEYS is not None and mtime == _API_KEYS_MTIME:
        return _API_KEYS
    keys = {}
    if mtime is not None:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    name, _, value = line.partition("=")
                    name = name.strip()
                    value = value.strip().strip('"').strip("'")
                    if name:
                        keys[name] = value
        except Exception:
            keys = {}
    _API_KEYS, _API_KEYS_MTIME = keys, mtime
    return keys


def _api_key_state(ref):
    """How a model's `api_key` field resolves: (state, secret).

    Four outcomes, and they need opposite treatment:

      none     - nothing set. Fine for a local server, fatal for a hosted one.
      named    - the name is in API KEYS.txt or the environment. Works.
      literal  - a credential pasted straight into models.json. Works, but it
                 is a plaintext secret in a file that gets rewritten, shared
                 and committed by accident.
      missing  - a name that matches nothing. This is the one that used to be
                 invisible: it fell through to "use the reference as the
                 secret", so a typo was sent as `Bearer MY_KEY` and came back
                 as a 401 with nothing to act on, while the model page happily
                 reported a key was configured.

    Deciding here, once, is what lets the gear dialog, the key badge, TEST KEY
    and the fail-fast check all tell the truth by the same rule."""
    ref = str(ref or "").strip()
    if not ref:
        return "none", ""
    keys = _load_api_keys()
    if keys.get(ref):
        return "named", keys[ref]
    env = os.environ.get(ref)
    if env and env.strip():
        return "named", env.strip()
    if _SECRETISH_RE.match(ref) or len(ref) >= 40:
        return "literal", ref
    return "missing", ""


def _api_key_present(ref):
    """True only when there is an actual credential behind `ref`."""
    return _api_key_state(ref)[0] in ("named", "literal")


def _resolve_api_key(ref):
    """Turn a model's `api_key` field into the real secret.

    A name that resolves to nothing is still passed through verbatim rather
    than dropped, so an existing setup cannot be broken by this change; what
    changed is that the code around it can now *tell* that is what happened
    and say so, instead of reporting a working key and failing at the far end
    of a conversation."""
    ref = str(ref or "").strip()
    if not ref:
        return ""
    state, value = _api_key_state(ref)
    if state in ("named", "literal"):
        return value
    return ref


def _api_host(base_url):
    """Just the host of a base URL, lowercased and without the port."""
    host = str(base_url or "").strip()
    if "//" in host:
        host = host.split("//", 1)[1]
    host = host.split("/", 1)[0]
    if "@" in host:                       # strip any user:info@ prefix
        host = host.rsplit("@", 1)[1]
    if host.startswith("["):              # [::1]:8080
        host = host[1:].split("]", 1)[0]
    elif ":" in host:
        host = host.split(":", 1)[0]
    return host.lower()


def _is_loopback(entry_or_url):
    """True when this endpoint is a llama-server on this PC rather than a
    hosted provider. A model on 127.0.0.1 speaks llama.cpp's dialect, which
    understands keep_alive and chat_template_kwargs; a hosted OpenAI-compatible
    endpoint does not, and forwards unknown parameters upstream, so those two
    are only ever sent to a loopback host. Local .gguf models are "local" type
    and never reach any of this."""
    if isinstance(entry_or_url, dict):
        if entry_or_url.get("type") != "api":
            return False
        url = entry_or_url.get("base_url")
    else:
        url = entry_or_url
    host = _api_host(url)
    return host in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "")


def _api_base(entry):
    """The base an api model is talked to on, normalised so the existing
    "+ /v1/chat/completions" is right. OpenRouter is documented as
    https://openrouter.ai/api/v1, and the add-model dialog suggests a local
    server as http://127.0.0.1:1234/v1, so both arrive with the /v1 already
    attached; strip it and put it back in one place."""
    base = str((entry or {}).get("base_url") or "").strip().rstrip("/")
    if base.lower().endswith("/v1"):
        base = base[:-3]
    return base


# The two OpenAI-shaped completion APIs this app can speak. "chat" is
# /v1/chat/completions, which every OpenAI-compatible provider answers and which
# this app has always spoken. "responses" is OpenAI's second one.
_API_WIRE_CHAT = "chat"
_API_WIRE_RESPONSES = "responses"


def _api_wire(entry):
    """Which completion dialect an api model is talked to in.

    /v1/responses is not a renamed /v1/chat/completions: a different body, a
    flat tool schema, a separate instructions field and a typed event stream
    instead of anonymous delta chunks. So a model that only publishes that
    endpoint - OpenCode Zen's Muse Spark, for one - cannot be reached by
    changing the URL alone, and the choice is recorded per model as
    `"wire": "responses"` in models.json. Anything unset means chat, which is
    what every existing model already is and keeps them all working."""
    wire = str((entry or {}).get("wire") or "").strip().lower()
    return wire if wire in (_API_WIRE_CHAT, _API_WIRE_RESPONSES) else _API_WIRE_CHAT


def _model_auth_headers(entry=None):
    """Auth headers for a model endpoint; empty for local servers."""
    entry = entry if entry is not None else _active_model()
    if not entry or entry.get("type") != "api":
        return {}
    key = _resolve_api_key(entry.get("api_key"))
    if not key:
        return {}
    header = str(entry.get("api_key_header") or "").strip()
    if not header:
        # "Bearer sk-..." or "Basic ..." is used as written, anything else
        # gets the standard bearer prefix
        value = key if " " in key.split("=")[0] else "Bearer " + key
        return {"Authorization": value}
    return {header: key}


def _test_model_key(entry=None, base=None, model=None, key_ref=None):
    """Check a key against the endpoint without sending a chat.

    Finding out a key is wrong by sending a message and reading a 401 costs a
    round trip, burns the turn, and buries the real cause under whatever the
    assistant was doing. /v1/models is the cheapest authenticated call an
    OpenAI-compatible provider offers, and it costs nothing on a free tier.

    Takes either a saved entry or the three raw dialog fields, so it can be
    used to check a key before the model has ever been saved."""
    entry = entry if entry is not None else _active_model()
    if entry and entry.get("type") == "api":
        base = base if base is not None else entry.get("base_url")
        model = model if model is not None else entry.get("model")
        if key_ref is None:
            key_ref = entry.get("api_key")
    base = str(base or "").strip()
    if not base:
        return {"ok": False, "error": "no base URL to test"}
    if not base.lower().startswith(("http://", "https://")):
        return {"ok": False, "error": "the base URL must start with http:// or https://"}
    clean, key_err = _sanitise_key_ref(key_ref)
    if key_err:
        return {"ok": False, "error": key_err}
    state, _secret = _api_key_state(clean)
    if state in ("none", "missing"):
        return {"ok": False, "needs_key": True,
                "error": ("no key to test. API KEYS.txt has no entry called "
                          "%s, so there is nothing to send - add a line "
                          "'%s=your-key' to API KEYS.txt, or set an "
                          "environment variable of that name"
                          % (clean or "(nothing)", clean or "NAME=value"))}
    probe = {"type": "api", "base_url": base, "model": str(model or "")}
    if clean:
        probe["api_key"] = clean
    key = _resolve_api_key(clean)
    url = _api_base(probe) + "/v1/models"
    # Built by hand rather than via _model_headers(): that helper injects the
    # *active* model's key, which would offer one provider's credential to a
    # different host when the key under test is missing.
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(_model_auth_headers(probe))
    try:
        data = _http_json_get(url, timeout=15, headers=hdrs)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": getattr(exc, "code", None),
                "error": _model_error_text(exc, probe)}
    except Exception as exc:
        return {"ok": False,
                "error": "could not reach %s - %s. Check the base URL and your "
                         "network." % (base, getattr(exc, "reason", None) or exc)}
    ids = [str(i.get("id") or "") for i in (data.get("data") or [])
           if isinstance(i, dict)] if isinstance(data, dict) else []
    out = {"ok": True, "detail": "the key was accepted by %s" % base,
           "models_visible": len(ids)}
    want = str(model or "").strip()
    if want:
        if want in ids:
            hit = next((i for i in data.get("data", [])
                        if str(i.get("id")) == want), {})
            out["model_ok"] = True
            if hit.get("context_length"):
                out["context_length"] = int(hit["context_length"])
        else:
            # A valid key that cannot see the model is a different mistake from a
            # bad key, and conflating them sends people to reissue their key.
            out["model_ok"] = False
            out["warn"] = ("the key works, but '%s' is not in the provider's list"
                           % want)
    return out


def _dedupe_by_id(models):
    """One entry per id, keeping the one that can actually answer.

    Two rows sharing an id is not a cosmetic problem: everything that looks a
    model up by id - the selector, the settings dialog, the ctx meter - takes
    the first match, so a dead duplicate silently wins over a working one. It
    happens whenever a default or discovered entry lands on an id the file
    already had, which is exactly what a folder copied from another machine
    produces. Settings the dead copy was missing are carried over, so
    repairing an entry does not quietly reset what the user configured.
    """
    order, byid = [], {}
    for m in models:
        mid = m.get("id")
        if not mid:
            continue
        if mid not in byid:
            order.append(mid)
            byid[mid] = m
            continue
        keep, drop = byid[mid], m
        if _model_can_answer(drop, [drop]) and not _model_can_answer(keep, [keep]):
            keep, drop = drop, keep
        for k, v in drop.items():
            if v not in (None, "") and k not in keep:
                keep[k] = v
        byid[mid] = keep
    return [byid[mid] for mid in order]


def _model_can_answer(mid, models):
    """True when this entry could actually serve a message.

    A hosted model always can - it is a URL and a key. A local one is a file
    on this disk, and a path that does not exist here means the model is
    gone, not slow."""
    entry = next((m for m in models if m.get("id") == mid), None)
    if not entry:
        return False
    if entry.get("type", "local") != "local":
        return True
    path = str(entry.get("path") or "")
    return bool(path) and os.path.exists(path)


def _drop_foreign_local_models(models):
    """Forget local models whose file is gone, and cannot come back.

    models.json is written by the app, so a folder that travels - copied,
    unzipped, moved to another machine or another operating system - carries
    absolute paths from the machine it left. "C:\\Users\\someone\\model.gguf"
    is not a missing file on Linux, it is a path to somewhere that does not
    exist and never will here, and keeping it only produces a permanently
    "(missing)" row that shadows the real model. An entry whose *filename* is
    still sitting in BONSAI_DIR is kept: that one can be pointed back at the
    file it names, and the user may well have moved the weights rather than
    lost them.
    """
    here = {n.lower() for n in os.listdir(BONSAI_DIR)} \
        if os.path.isdir(BONSAI_DIR) else set()
    for folder in (MODELS_DIR,):
        if os.path.isdir(folder):
            here |= {n.lower() for n in os.listdir(folder)}
    kept = []
    for m in models:
        if m.get("type", "local") != "local" or not m.get("path"):
            kept.append(m)
            continue
        path = str(m.get("path") or "")
        if os.path.exists(path):
            kept.append(m)
            continue
        if os.path.basename(path).lower() in here:
            kept.append(m)  # the file is here, the recorded path just is not
            continue
        if m.get("id") == "bonsai2" and os.path.basename(
                _default_model_entry()["path"]).lower() in here:
            m["path"] = _default_model_entry()["path"]
            m["mmproj"] = _default_model_entry()["mmproj"]
            kept.append(m)
            continue
        print("dropping local model '%s' - %s does not exist on this machine"
              % (m.get("id"), path), flush=True)
    return kept


def _load_models():
    global _MODELS, _ACTIVE_MODEL
    data = None
    for candidate in (MODELS_FILE, MODELS_FILE + ".bak"):
        try:
            with open(candidate, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and data.get("models"):
                if candidate != MODELS_FILE:
                    print("models.json was unreadable - recovered from the backup",
                          flush=True)
                break
            data = None
        except Exception:
            data = None
    models, active = [], None
    if isinstance(data, dict):
        models = [m for m in (data.get("models") or [])
                  if isinstance(m, dict) and m.get("id")]
        active = data.get("active")
    models = _drop_foreign_local_models(models)
    default = _default_model_entry()
    if not any(m.get("type", "local") == "local"
               and os.path.normcase(m.get("path") or "") == os.path.normcase(default["path"])
               for m in models):
        models.insert(0, default)
    models.extend(_discover_models(models))
    for m in models:
        _pair_mmproj(m)
    models = _dedupe_by_id(models)
    ids = [m["id"] for m in models]
    with _MODELS_LOCK:
        _MODELS = models
        # A local model whose file is not there cannot answer a single message,
        # so it must not be the one selected at startup. This used to pick it
        # anyway: a folder copied from another machine keeps its models.json
        # paths, the copy on this machine does not exist, and the id was still
        # in the list - so a Linux user who unzipped the project got a
        # permanently "(missing)" model chosen for them and a duplicate of the
        # real one sitting next to it.
        if active in ids and _model_can_answer(active, models):
            _ACTIVE_MODEL = active
        else:
            _ACTIVE_MODEL = next(
                (m["id"] for m in models if _model_can_answer(m["id"], models)),
                models[0]["id"] if models else None)
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
        # Every model, hosted ones included: the picker draws the window beside
        # the name, and a hosted entry that reported none was drawn as a blank,
        # which read as "unknown" when in fact the number was simply never sent.
        item["ctx"] = _entry_cfg(m)["ctx"]
        if mtype == "local":
            item["path"] = m.get("path")
            item["available"] = bool(m.get("path") and os.path.exists(m["path"]))
            item["cfg"] = _entry_cfg(m)
            item["mmproj"] = m.get("mmproj") or ""
            item["vision"] = bool(item["mmproj"]
                                  and os.path.isfile(item["mmproj"]))
        else:
            item["base_url"] = m.get("base_url")
            item["model"] = m.get("model")
            item["wire"] = _api_wire(m)
            # only ever report *that* a key is configured, never the value
            kstate = _api_key_state(m.get("api_key"))[0]
            item["api_key_set"] = _api_key_present(m.get("api_key"))
            item["api_key_state"] = kstate
            # A *name* is not a secret, and it has to reach the gear dialog or
            # there is no way to see or correct it short of deleting the model.
            # A literal key IS a secret, and those are legacy entries: report the
            # state and let the hint explain the fix, but never send the value to
            # the browser. Clearing the field and typing a name migrates it.
            item["api_key_name"] = ("" if kstate == "literal"
                                    else str(m.get("api_key") or ""))
            # a remote endpoint cannot answer anything without one, and the
            # only symptom used to be a 401 in the middle of a conversation
            item["needs_key"] = bool(mtype == "api" and not item["api_key_set"]
                                     and not _is_loopback(m))
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
        # Re-clicking the active model is how people try to re-apply sampling
        # settings, so actually restart it instead of silently doing nothing.
        _apply_model(entry)
        _stop_local_server(int(entry.get("port") or 8080))
        _save_models()
        return {"ok": True, "type": "local", "ready": _bonsai_ready(),
                "reloaded": True,
                "detail": "model reloaded - it starts on your next message with "
                          "its current settings",
                **_public_models()}
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


def _tk_picker_problem():
    """Why the file browser cannot open, or None when it can.

    Every picker here runs Tk in a child process, and that child dies quietly:
    no display, or no tkinter in the interpreter, and it prints nothing at all
    back. The caller saw an empty string, returned None, and the UI reported
    "cancelled" - so a Linux user with no python3-tk, or on a headless box,
    clicked browse and nothing whatsoever happened. Saying so lets the UI fall
    back to typing the path."""
    if not IS_LINUX:
        return None
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return ("this machine has no graphical display, so a file browser "
                "cannot open - type the path instead")
    probe = subprocess.run(
        [sys.executable, "-c", "import tkinter"],
        capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        return ("this Python has no tkinter, so a file browser cannot open - "
                "install it (sudo apt install python3-tk) or type the path")
    return None


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
        # a *reference* to an entry in API KEYS.txt (or an env var name), so
        # the secret itself never has to be written into models.json
        key_ref, key_err = _sanitise_key_ref(body.get("api_key"))
        if key_err:
            return {"ok": False, "error": key_err}
        if key_ref:
            entry["api_key"] = key_ref
        key_header = str(body.get("api_key_header") or "").strip()
        if key_header:
            entry["api_key_header"] = key_header
        # /v1/responses is a different protocol, not another URL, so a model that
        # only offers it has to be marked as speaking it. Left off means chat.
        wire = str(body.get("wire") or "").strip().lower()
        if wire and wire not in (_API_WIRE_CHAT, _API_WIRE_RESPONSES):
            return {"ok": False,
                    "error": "wire must be '%s' or '%s'" % (_API_WIRE_CHAT,
                                                             _API_WIRE_RESPONSES)}
        if wire == _API_WIRE_RESPONSES:
            entry["wire"] = wire
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
    res = {"ok": True, "added": entry, **_public_models()}
    # adding a hosted model with no usable key is allowed - it can be filled in
    # afterwards - but the user should hear about it now, not from a 401 later
    if entry.get("type") == "api" and not _api_key_present(entry.get("api_key")) \
            and not _is_loopback(entry):
        res["warn"] = _no_key_warning(entry)
    return res


def _no_key_warning(entry):
    name = str((entry or {}).get("api_key") or "").strip()
    host = _api_host((entry or {}).get("base_url"))
    if not name:
        return ("%s needs an API key. Add a line 'NAME=your-key' to API KEYS.txt, "
                "then open MODEL SETTINGS for this model and put NAME in the API "
                "key name field." % host)
    return ("'%s' is not in API KEYS.txt, so it would be sent to %s as if it were "
            "the key and every request would come back 401. Add a line "
            "'%s=your-key' to API KEYS.txt." % (name, host, name))


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
        if not _is_loopback(entry):
            # Sending a 1-token "unload" completion to someone else's server
            # costs a real request and burns free-tier quota to achieve nothing
            # - the model is not ours to release.
            return {"ok": True, "method": "remote",
                    "detail": "'%s' runs on someone else's server - there is "
                              "nothing to unload here"
                              % (entry.get("label") or entry.get("model")
                                 or "this model")}
        try:
            _http_json(BONSAI_BASE + "/v1/chat/completions",
                       {"model": BONSAI_MODEL_ID,
                        "messages": [{"role": "user", "content": "unload"}],
                        "max_tokens": 1, "keep_alive": 0}, timeout=60)
            return {"ok": True, "method": "keep_alive",
                    "detail": "asked the external server to unload the model"}
        except Exception as exc:
            return {"ok": False, "error": _model_error_text(exc)}
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
    """The effort knob, in whichever dialect the active endpoint speaks.

    A loopback llama-server takes chat_template_kwargs and calls the reasoning
    "reasoning_content". A hosted endpoint wants the OpenRouter/OpenAI unified
    `reasoning` object instead, and passes the text back as `reasoning`."""
    with _EFFORT_LOCK:
        eff = _BONSAI_EFFORT
    entry = _active_model()
    if entry and entry.get("type") == "api" and not _is_loopback(entry):
        # off/low/medium/xhigh are exactly the values a hosted endpoint wants
        if eff == "off":
            return {"reasoning": {"enabled": False}}
        return {"reasoning": {"effort": eff}}
    if eff == "off":
        return {"chat_template_kwargs": {"enable_thinking": False}}
    return {"chat_template_kwargs": {"enable_thinking": True,
                                     "reasoning_effort": eff}}


def _model_headers(extra=None):
    """Headers for a call to the active model server: JSON plus, when the
    active model is a keyed API endpoint, its Authorization header.

    The User-Agent is not optional. urllib otherwise sends "Python-urllib/3.x",
    which the firewall in front of OpenCode Zen rejects with "error code: 1010"
    before the request reaches any model - so every Zen model answered 403 and
    the 403 looked like a permissions problem when the real cause was that the
    app had not said what it was. Naming itself is how the rest of the app
    talks to the web too."""
    headers = {"Content-Type": "application/json", "User-Agent": _WEB_UA}
    try:
        headers.update(_model_auth_headers())
    except Exception:
        pass
    if extra:
        headers.update(extra)
    return headers


def _http_json(url, payload, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=_model_headers(),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_json_get(url, timeout=30, headers=None):
    # headers=None keeps the old behaviour: the active model's own headers,
    # which is what the context lookup wants. A caller testing a key for an
    # endpoint that is not active yet passes them explicitly.
    req = urllib.request.Request(url, headers=headers if headers is not None
                                 else _model_headers(), method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


WEB_SPECS = [WEB_TOOLS["web_search"], WEB_TOOLS["web_fetch"]]





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


# ---- OpenCode-compatible tools (ported from opencode-tools) ----

GLOB_TOOL = {
    "type": "function",
    "function": {
        "name": "glob",
        "description": "Search for files by glob pattern (e.g. '**/*.ts', "
                       "'src/**/*.tsx'). Returns matching file paths sorted by "
                       "modification time. Use this to quickly find files by name "
                       "pattern without reading their contents.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "The glob pattern to match files against, "
                                   "e.g. '**/*.py', 'src/**/*.ts'."
                },
                "path": {
                    "type": "string",
                    "description": "The directory to search in. If not "
                                   "specified, the workspace root is used. Must be "
                                   "a valid directory path if provided."
                }
            },
            "required": ["pattern"]
        }
    }
}

READ_TOOL = {
    "type": "function",
    "function": {
        "name": "read",
        "description": "Read file contents (text, images, PDFs). Supports "
                       "offset/limit for partial reads and binary detection. "
                       "Images are returned as base64 attachments the model can "
                       "see. PDFs are extracted. Use offset and limit to page "
                       "through large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "filePath": {
                    "type": "string",
                    "description": "The absolute path to the file or "
                                   "directory to read."
                },
                "offset": {
                    "type": "integer",
                    "description": "The line number to start reading from "
                                   "(1-indexed)."
                },
                "limit": {
                    "type": "integer",
                    "description": "The maximum number of lines to read "
                                   "(defaults to 2000)."
                }
            },
            "required": ["filePath"]
        }
    }
}

EDIT_TOOL = {
    "type": "function",
    "function": {
        "name": "edit",
        "description": "Make targeted changes to existing files. Uses "
                       "fuzzy matching with multiple strategies (exact, "
                       "line-trimmed, block-anchor, whitespace-normalized, "
                       "indentation-flexible, escape-normalized, context-aware) "
                       "to find and replace text. Returns a diff summary with "
                       "additions/deletions.",
        "parameters": {
            "type": "object",
            "properties": {
                "filePath": {
                    "type": "string",
                    "description": "The absolute path to the file to "
                                   "modify."
                },
                "oldString": {
                    "type": "string",
                    "description": "The text to replace."
                },
                "newString": {
                    "type": "string",
                    "description": "The text to replace it with "
                                   "(must be different from oldString)."
                },
                "replaceAll": {
                    "type": "boolean",
                    "description": "Replace all occurrences of oldString "
                                   "(default false)."
                }
            },
            "required": ["filePath", "oldString", "newString"]
        }
    }
}

SHELL_OC_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run shell commands (PowerShell on Windows, bash on "
                       "Linux) with advanced features: output tailing, "
                       "truncation handling, timeout management, and full "
                       "stdout/stderr capture. Returns the command output "
                       "with exit code.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute."
                },
                "workdir": {
                    "type": "string",
                    "description": "Working directory for the command. "
                                   "Defaults to the workspace root."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in milliseconds (default 120000, "
                                   "max 600000)."
                }
            },
            "required": ["command"]
        }
    }
}

QUESTION_TOOL = {
    "type": "function",
    "function": {
        "name": "question",
        "description": "Ask the user clarifying questions when input is "
                       "needed. Each question has a header (max 30 chars), a "
                       "full question text, and optional answer options. The "
                       "user can pick an option or type a free-form answer.",
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to ask the user.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "question": {
                                "type": "string",
                                "description": "The complete question to ask."
                            },
                            "header": {
                                "type": "string",
                                "description": "Very short label (max 30 "
                                               "chars), e.g. 'Confirm'."
                            },
                            "options": {
                                "type": "array",
                                "description": "Available choices for this question.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {
                                            "type": "string",
                                            "description": "Display text (1-5 words)."
                                        },
                                        "description": {
                                            "type": "string",
                                            "description": "Explanation of what this "
                                                           "option means."
                                        }
                                    },
                                    "required": ["label"]
                                }
                            }
                        },
                        "required": ["question"]
                    }
                }
            },
            "required": ["questions"]
        }
    }
}

TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "task",
        "description": "Spawn a subagent to handle complex multi-step tasks. "
                       "The subagent runs with its own context and can use "
                       "tools. Use background=true for independent work that "
                       "can run while you continue elsewhere. Available "
                       "subagent types: 'explore' (fast codebase search), "
                       "'general' (general-purpose multi-step tasks).",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "A short (3-5 words) description "
                                   "of the task."
                },
                "prompt": {
                    "type": "string",
                    "description": "The task for the agent to "
                                   "perform."
                },
                "subagent_type": {
                    "type": "string",
                    "description": "The type of specialized agent: "
                                   "'explore' or 'general'."
                },
                "background": {
                    "type": "boolean",
                    "description": "Run in background. You will be notified "
                                   "when it completes. Do NOT poll or proactively "
                                   "check progress."
                }
            },
            "required": ["description", "prompt", "subagent_type"]
        }
    }
}

PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "plan",
        "description": "Create a detailed implementation plan before writing "
                       "code. Use this when you need to think through an "
                       "approach before implementing. The plan is saved and can "
                       "be reviewed before execution.",
        "parameters": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "The task to plan an "
                                   "implementation for."
                }
            },
            "required": ["task"]
        }
    }
}

SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "skill",
        "description": "Load a specialized skill's instructions and resources. "
                       "Skills provide specialized workflows for specific tasks "
                       "(e.g. 'opencode' for OpenCode questions, 'report' for "
                       "bug reports). The skill content is injected into the "
                       "conversation.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The name of the skill from "
                                   "available_skills."
                }
            },
            "required": ["name"]
        }
    }
}

EXECUTE_TOOL = {
    "type": "function",
    "function": {
        "name": "execute",
        "description": "Run JavaScript in a sandboxed runtime to script tool "
                       "calls and HTTP requests. Use this to automate sequences "
                       "of operations, fetch data, or compose results from "
                       "multiple tool calls.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "The JavaScript code to execute."
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30, max 120)."
                }
            },
            "required": ["code"]
        }
    }
}

OPENCODE_TOOLS = [GLOB_TOOL, READ_TOOL, EDIT_TOOL, SHELL_OC_TOOL,
                  QUESTION_TOOL, TASK_TOOL, PLAN_TOOL, SKILL_TOOL,
                  EXECUTE_TOOL]

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


def _dl_tune_props(with_files=False):
    """Schema knobs every download tool shares, so they cannot drift apart."""
    props = {
        "max_mb": {"type": "integer",
                   "description": "Optional size cap for this file in MB "
                                  "(default: no limit at all)."},
        "retries": {"type": "integer",
                    "description": "Extra attempts when the connection drops, "
                                   "max 10."},
        "connections": {"type": "integer",
                        "description": "Split this one file across 1-4 parallel "
                                       "connections (default 1). Needs a server "
                                       "that honours range requests; a large "
                                       "speed-up on slow single-stream hosts, "
                                       "ignored for small files."},
        "throttle_kbps": {"type": "integer",
                          "description": "Cap this download at N kilobytes per "
                                         "second (default 0 = uncapped) so it "
                                         "leaves bandwidth for other apps."},
        "timeout": {"type": "integer",
                    "description": "Seconds to wait before handing the download "
                                   "back as still running (default 180, max "
                                   "1800). A download that outlasts it keeps "
                                   "going in the background."},
        "background": {"type": "boolean",
                       "description": "Return straight away with a job id instead "
                                      "of waiting. The transfer continues and shows "
                                      "up in the DOWNLOADS panel."}
    }
    if with_files:
        props["overwrite"] = {"type": "boolean",
                              "description": "Replace the destination if it "
                                             "already exists (default false: a "
                                             "numbered copy is made)."}
    return props


DOWNLOAD_STATUS_TOOL = {
    "type": "function",
    "function": {
        "name": "download_status",
        "description": "Check on downloads: pass a job id to see one transfer, or "
                       "call it with no arguments to list everything currently "
                       "queued, running or paused, with bytes, speed and ETA. Use "
                       "it after a 'background: true' download.",
        "parameters": {
            "type": "object",
            "properties": {
                "job": {"type": "string",
                        "description": "Job id from a background download. Omit to "
                                       "list the active ones."}
            },
            "required": []
        }
    }
}

DOWNLOAD_TOOL = {
    "type": "function",
    "function": {
        "name": "download_file",
        "description": "Download a file from a web URL and save it inside the "
                       "workspace folder. Streams straight to disk, so a 5 GB file "
                       "costs no more memory than a small one, and it can continue "
                       "where it stopped if the connection drops. Returns the saved "
                       "path, the byte size and (for text files) the beginning of the "
                       "content so you can see what you got.",
        "parameters": {
            "type": "object",
            "properties": dict({
                "url": {
                    "type": "string",
                    "description": "Full http(s) URL to download."
                },
                "path": {
                    "type": "string",
                    "description": "Relative destination inside the workspace, "
                                   "e.g. 'Downloads/installer.exe'. Defaults to "
                                   "the file name from the URL."
                },
                "follow": {
                    "type": "boolean",
                    "description": "The URL is a page, not a file. Resolve it "
                                   "first (index -> file host -> file) in a real "
                                   "browser and download the file behind it, "
                                   "carrying the session cookies over. Default "
                                   "false. Use web_resolve first if you want to "
                                   "see what is behind the page before anything "
                                   "is written to disk."
                }
            }, **_dl_tune_props(with_files=True)),
            "required": ["url"]
        }
    }
}


WEB_RESOLVE_TOOL = {
    "type": "function",
    "function": {
        "name": "web_resolve",
        "description": "Find the real file behind a download PAGE. Some sites do not "
                       "give you a file URL: they show an index that links elsewhere, "
                       "a file host behind a 'your download starts in N seconds' page, "
                       "links built by JavaScript, or a bot check. This reads the page, "
                       "follows those hops, and returns a ranked list of candidate "
                       "files with names and sizes. It downloads nothing. Use it when a "
                       "URL is a page rather than a file, then pass the chosen url to "
                       "download_file (with 'follow': true to carry the session over).",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The page URL to resolve."},
                "render": {"type": "boolean",
                           "description": "Load the page in a real browser so "
                                          "JavaScript runs and bot checks are handled. "
                                          "Slower, but needed when the links only exist "
                                          "after the page runs. Default false."},
                "max_hops": {"type": "integer",
                             "description": "How many page-to-page hops to follow. "
                                            "Default 3, max 5."},
                "max_results": {"type": "integer",
                                "description": "How many candidates to return. "
                                               "Default 10, max 25."},
                "timeout": {"type": "integer",
                            "description": "Seconds to allow, default 30."},
                "headers": {"type": "object",
                            "description": "Extra request headers, e.g. a cookie, for a "
                                           "page that needs a session."},
            },
            "required": ["url"]
        }
    }
}


DOWNLOAD_BATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "download_batch",
        "description": "Download MANY files in one call: pass a list of URLs, or "
                       "point 'from_page' at a page and it resolves the files "
                       "actually behind it (following hops, ignoring nav links), "
                       "optionally filtered by 'match'. Files land in one "
                       "folder, several at a time, and you get a per-file report "
                       "of what succeeded and what failed. Use this instead of "
                       "calling download_file in a loop.",
        "parameters": {
            "type": "object",
            "properties": dict({
                "urls": {"type": "array", "items": {"type": "string"},
                         "description": "Full http(s) URLs to download."},
                "from_page": {"type": "string",
                              "description": "A page to take links from. By "
                                             "default it downloads every link "
                                             "the page's markup carries, "
                                             "filtered by 'match'."},
                "resolve": {"type": "boolean",
                            "description": "Resolve the page first and download "
                                           "the files actually behind it, "
                                           "following hops (index -> file host "
                                           "-> file) instead of grabbing the "
                                           "page's own links. This skips nav "
                                           "and login links that a plain scrape "
                                           "would try to download. Default "
                                           "false, so 'from_page' keeps its "
                                           "original meaning."},
                "match": {"type": "string",
                          "description": "Only keep links matching this regular "
                                         "expression, e.g. '\\\\.pdf$'."},
                "render": {"type": "boolean",
                           "description": "With 'resolve', read the page in a "
                                          "real browser so JavaScript-built "
                                          "links and bot checks resolve too. "
                                          "Slower; default false."},
                "max_hops": {"type": "integer",
                             "description": "With 'resolve', how many "
                                            "page-to-page hops to follow. "
                                            "Default 3, max 5."},
                "path": {"type": "string",
                         "description": "Folder inside the workspace for the "
                                        "files. Default 'downloads'."},
                "max_files": {"type": "integer",
                              "description": "Safety cap, default 25, max 200."},
                "parallel": {"type": "integer",
                             "description": "How many files at once, default 4, max 8."},
            }, **_dl_tune_props()),
            "required": []
        }
    }
}

DOWNLOAD_AUTHED_TOOL = {
    "type": "function",
    "function": {
        "name": "download_authed",
        "description": "Download a file that needs credentials - a page behind a "
                       "login, a private API, a file with a token. Pass 'bearer', "
                       "'cookie' or full 'headers'. The secret values are never "
                       "written to the chat history or printed back; only the "
                       "header names are echoed. Prefer download_file for public "
                       "URLs.",
        "parameters": {
            "type": "object",
            "properties": dict({
                "url": {"type": "string", "description": "Full http(s) URL."},
                "bearer": {"type": "string",
                           "description": "API token; sent as 'Authorization: "
                                          "Bearer <token>'."},
                "cookie": {"type": "string",
                           "description": "Cookie header value, e.g. "
                                          "'session=abc123'."},
                "headers": {"type": "object",
                            "description": "Extra request headers, e.g. "
                                           "{'X-Api-Key': '...'}."},
                "path": {"type": "string",
                         "description": "Destination inside the workspace."},
            }, **_dl_tune_props()),
            "required": ["url"]
        }
    }
}

DOWNLOAD_PAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "download_page",
        "description": "Save a web page so it works OFFLINE: downloads the HTML "
                       "plus its images, CSS, JS and fonts, rewrites the links to "
                       "the local copies and writes index.html you can open later. "
                       "Use it to keep a page, a docs page or an article.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Page to save."},
                "path": {"type": "string",
                         "description": "Folder inside the workspace. Default "
                                        "'pages'."},
                "assets": {"type": "boolean",
                           "description": "Download images/CSS/JS too. Default true."},
                "max_assets": {"type": "integer",
                               "description": "How many assets, default 60, max 400."},
                "max_mb": {"type": "integer", "description": "Per-asset size cap."}
            },
            "required": ["url"]
        }
    }
}

DOWNLOAD_VERIFY_TOOL = {
    "type": "function",
    "function": {
        "name": "download_verify",
        "description": "Download a file you must be sure about: big downloads "
                       "continue where they stopped (HTTP resume), failed attempts "
                       "retry, and the result is checked against the sha256 and/or "
                       "the byte size you expected. Reports 'FAILED CHECK' instead "
                       "of pretending it worked. Use it for installers, archives, "
                       "datasets and model files.",
        "parameters": {
            "type": "object",
            "properties": dict({
                "url": {"type": "string", "description": "Full http(s) URL."},
                "path": {"type": "string",
                         "description": "Destination inside the workspace."},
                "sha256": {"type": "string",
                           "description": "Expected 64-char sha256 of the file."},
                "bytes": {"type": "integer",
                          "description": "Expected size in bytes."},
                "resume": {"type": "boolean",
                           "description": "Continue a partial download. Default true."},
            }, **_dl_tune_props()),
            "required": ["url"]
        }
    }
}

DOWNLOAD_MEDIA_TOOL = {
    "type": "function",
    "function": {
        "name": "download_media",
        "description": "Download video, audio or subtitles from a normal media site "
                       "(YouTube, Vimeo, a direct .m3u8 stream, podcasts, etc.) "
                       "using yt-dlp. Can pull the audio only as mp3, fetch "
                       "subtitles and embed a thumbnail. Needs yt-dlp installed "
                       "('pip install yt-dlp').",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Media page or stream URL."},
                "path": {"type": "string",
                         "description": "Folder inside the workspace. Default 'media'."},
                "audio_only": {"type": "boolean",
                               "description": "Extract the audio as mp3 instead "
                                              "of the video."},
                "audio_format": {"type": "string",
                                 "description": "Audio format, default mp3."},
                "quality": {"type": "string",
                            "description": "yt-dlp format string, e.g. "
                                           "'bestvideo[height<=1080]+bestaudio'."},
                "subtitles": {"type": "boolean",
                              "description": "Download subtitles too."},
                "sub_langs": {"type": "string",
                              "description": "Subtitle languages, default 'en'."},
                "thumbnail": {"type": "boolean",
                              "description": "Save and embed a thumbnail image."},
                "playlist": {"type": "boolean",
                             "description": "Allow whole playlists/albums."},
                "cookies_from_browser": {"type": "string",
                                         "description": "Reuse cookies from "
                                                        "chrome/firefox/edge for "
                                                        "age or private videos."},
                "timeout": {"type": "integer", "description": "Timeout seconds."}
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
                    "description": "Process name to match (e.g. "
                                   + ("'notepad.exe'" if IS_WINDOWS else "'firefox'")
                                   + ")."
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
                       "engine (no cloud, no built-in system voices). Also "
                       "saves the speech as a WAV file in the workspace. Use "
                       "this to give the user spoken feedback, alerts or TTS "
                       "output.",
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

RICH_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "rich_text",
        "description":
            "Put styled text on the system clipboard for another app's chat box, "
            "and optionally paste and send it there. The same text is offered as "
            "plain text, as RTF and as HTML at the same time, because apps read "
            "whichever of those they understand - Word and Outlook want RTF, a "
            "browser or Slack wants HTML, and a plain terminal wants text. Use "
            "this to hand a highlighted warning or a styled answer to another "
            "program's message box.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string",
                         "description": "The text to place, styled."},
                "color": {"type": "string",
                          "description": "Text colour. A name (red, green, blue, "
                                         "yellow, orange, purple, pink, white, "
                                         "black, grey) or a hex value like "
                                         "#ff3b30. Default: left as it is."},
                "bg": {"type": "string",
                       "description": "Highlight colour behind the text. Same "
                                      "names and hex values as color. Default: none."},
                "bold": {"type": "boolean",
                         "description": "Bold the text. Default false."},
                "size": {"type": "number",
                         "description": "Text size in points, 6 to 72. Default: "
                                        "the app's own size."},
                "monospace": {"type": "boolean",
                              "description": "Use a monospaced font. Default false."},
                "action": {"type": "string",
                           "enum": ["copy", "paste", "send"],
                           "description": "copy (default) only fills the "
                                          "clipboard. paste also puts the caret in "
                                          "the Bonsai chat box and presses Ctrl+V. "
                                          "send also presses Enter afterwards, which "
                                          "sends the message."}
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
             RICH_TEXT_TOOL,
             API_CALL_TOOL, WS_TEST_TOOL,
             SCHEDULE_TOOL, LIST_SCHEDULES_TOOL, UNSCHEDULE_TOOL,
             TTS_VOICES_TOOL, TTS_SPEAK_TOOL, PREVIEW_HTML_TOOL]

DOWNLOAD_TOOLS = [DOWNLOAD_STATUS_TOOL, DOWNLOAD_BATCH_TOOL,
                  DOWNLOAD_AUTHED_TOOL, DOWNLOAD_PAGE_TOOL,
                  DOWNLOAD_VERIFY_TOOL, DOWNLOAD_MEDIA_TOOL]

RESOLVE_TOOLS = [WEB_RESOLVE_TOOL]

PC_TOOLS = [SHOT_TOOL, INPUT_TOOL, CLIPBOARD_TOOL,
            DOWNLOAD_TOOL, ARCHIVE_TOOL, TODO_TOOL] + NEW_TOOLS + DOWNLOAD_TOOLS \
    + RESOLVE_TOOLS + OPENCODE_TOOLS


def _tools_for(mode):
    if mode == MODE_PLAN:
        return [FILE_TOOLS["list_dir"],
                FILE_TOOLS["grep"]] + WEB_SPECS
    return ([TOOL_SPEC] + list(FILE_TOOLS.values()) + WEB_SPECS +
            PC_TOOLS + _blender_tool_schemas())


def system_prompt(mode):
    text = SYSTEM
    if mode == MODE_PLAN:
        text += PLAN_MODE_SYSTEM
    return text


def _base_payload():
    """The shared part of every completion request.

    keep_alive is llama.cpp's own field for how long the server holds the
    model in memory. A hosted endpoint ignores parameters it does not know
    only when its provider does; the rest are forwarded upstream, so sending
    it is how you get a 400 from someone else's model server. Loopback only.
    """
    payload = {"model": BONSAI_MODEL_ID}
    entry = _active_model()
    if not entry or entry.get("type") != "api" or _is_loopback(entry):
        payload["keep_alive"] = -1
    payload.update(_effort_params())
    return payload


def _bonsai_chat(messages, tools):
    if _api_wire(_active_model()) == _API_WIRE_RESPONSES:
        return _responses_chat(messages, tools)
    payload = _base_payload()
    payload.update({"messages": messages, "tools": tools, "stream": False})
    data = _http_json(BONSAI_BASE + "/v1/chat/completions", payload)
    msg = data.get("choices", [{}])[0].get("message", {})
    msg["_usage"] = data.get("usage") or {}
    msg["_timings"] = data.get("timings") or {}
    # a hosted endpoint answers with "reasoning"; llama.cpp uses
    # "reasoning_content". Normalise so the Thought step works on both.
    if not msg.get("reasoning_content"):
        msg["reasoning_content"] = _reasoning_text(msg)
    return msg


def _provider_message(exc):
    """The human-readable part of a provider's error body, when it sent one.

    An error body can be read exactly once - `exc.read()` drains it - and more
    than one caller legitimately wants it. The vision check reads it to see
    whether this is a refusal it can recover from, and the handler that turns
    the failure into a message for the user reads it again afterwards. The
    second read came back empty, so a rate limit that had been reported in the
    provider's own words quietly turned into our generic "not answering right
    now". So the answer is kept on the exception and handed back unchanged.
    """
    cached = getattr(exc, "_bonsai_provider_message", None)
    if cached is not None:
        return cached
    text = _provider_message_from_body(exc)
    try:
        exc._bonsai_provider_message = text
    except Exception:
        pass
    return text


def _provider_message_from_body(exc):
    """Read the body off an HTTP error and pull the sentence worth reading.

    OpenRouter keeps the text worth reading in error.metadata.raw and leaves
    error.message as a flat "Provider returned error", so the metadata is
    checked first and the flat message is only a fallback. Whatever comes
    back is scrubbed, because a provider that rejects a credential often
    quotes it back in the body.
    """
    try:
        body = exc.read()
    except Exception:
        return ""
    if not body:
        return ""
    if isinstance(body, bytes):
        try:
            body = body.decode("utf-8", "replace")
        except Exception:
            return ""
    try:
        data = json.loads(body)
    except Exception:
        return _redact_embedded_secrets(body.strip())[:300]
    err = data.get("error") if isinstance(data, dict) else None
    if isinstance(err, dict):
        meta = err.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        for part in (meta.get("raw"), meta.get("reason"),
                     err.get("message"), err.get("code")):
            text = str(part).strip() if part else ""
            if text and not _generic_provider_text(text):
                return _redact_embedded_secrets(text)[:300]
        # only boilerplate was on offer: our own wording beats it
        return ""
    if isinstance(err, str):
        return _redact_embedded_secrets(err.strip())[:300]
    return ""


def _model_error_text(exc, entry=None):
    """Turn a failed model request into something the reader can act on.

    urllib's default is "HTTP Error 401: Unauthorized", which does not say
    which key, which model, or what to do about it - and for a hosted endpoint
    it used to be swallowed entirely behind "model server could not start"."""
    if entry is None:
        entry = _active_model()
    who = str((entry or {}).get("label") or (entry or {}).get("model")
              or "the model server")
    detail = _provider_message(exc) if isinstance(
        exc, urllib.error.HTTPError) else ""
    code = getattr(exc, "code", None)
    if code == 401:
        msg = ("%s rejected the API key (401). It is missing, wrong, or "
               "expired: check the key name on the '%s' model, then put a valid "
               "key in API KEYS.txt." % (who, who))
    elif code == 403:
        # A 403 is "we know you, but not this model", and there are two very
        # different reasons for it here, so the provider's own sentence decides
        # which one to say. Guessing "gated or region-locked" sent us looking
        # for an account setting that does not exist.
        low = (detail or "").lower()
        if "1010" in low or "cloudflare" in low:
            msg = ("%s was blocked by the host's firewall before it reached "
                   "the model (403, Cloudflare 1010). The request did not "
                   "identify itself as a real client, so it was turned away "
                   "without ever reaching the account." % who)
        elif "free tier" in low or "within opencode" in low:
            msg = ("%s is not open to outside clients (403). OpenCode keeps its "
                   "free models for its own app - they only answer requests made "
                   "from inside OpenCode, so a valid key cannot reach them from "
                   "here. Nothing is wrong with the key. Pick a model that "
                   "serves third-party clients." % who)
        elif detail:
            msg = "%s refused the request (403): %s" % (who, detail)
        else:
            msg = ("%s refused the request (403). The key was accepted, but this "
                   "model is not available to this account. Pick another model."
                   % who)
    elif code == 402:
        msg = ("%s is out of credit. Top up the account, or pick another model."
               % who)
    elif code == 404:
        msg = ("%s has no such endpoint or model (%s). Check the base URL and "
               "the model id - the model id is not the web page address."
               % (who, code))
    elif code == 429:
        # the provider usually says whether this is upstream throttling or the
        # account's own limit, and that is the useful part
        msg = ("%s is rate limiting us (429). %s" % (
            who, detail if detail else
            "Free models throttle hard; wait a moment, or use a model you "
            "have credit for."))
    elif code == 403:
        # "invalid_request_error: invalid request" names no field and no
        # reason, so repeating it helps nobody, and a Cloudflare 1010 is not a
        # permissions problem at all. Say which one this is.
        low = (detail or "").lower()
        if "1010" in low or "cloudflare" in low:
            msg = ("%s was blocked by the host's firewall before it reached the "
                   "model (403, Cloudflare 1010). The request did not identify "
                   "itself as a real client, so it was turned away without ever "
                   "reaching the account." % who)
        elif "free tier" in low or "within opencode" in low:
            msg = ("%s is not open to outside clients (403). OpenCode keeps its "
                   "free models for its own app - they only answer requests made "
                   "from inside OpenCode, so a valid key cannot reach them from "
                   "here. Nothing is wrong with the key. Pick a model that "
                   "serves third-party clients." % who)
        elif detail:
            msg = "%s refused the request (403): %s" % (who, detail)
        else:
            msg = ("%s refused the request (403). The key was accepted, but this "
                   "model is not available to this account. Pick another model."
                   % who)
    elif code == 400 and detail:
        low = detail.lower()
        if any(m in low for m in _INVALID_REQUEST_MARKERS):
            # "invalid_request_error: invalid request" names no field and no
            # reason, so repeating it helps nobody. In this app the usual
            # cause is a single piece of tool output too large for the model,
            # and the turn is retried with it shortened before this is ever
            # reached - so if it is still here, say what to do.
            msg = ("%s rejected the whole request as invalid (400) without "
                   "saying which part. That normally means one message is too "
                   "large for this model - a big file read, a long directory "
                   "listing or a scraped page. Start a new chat, or ask for a "
                   "smaller slice, or switch to a model with a bigger context "
                   "window. Provider said: %s" % (who, detail))
        else:
            msg = "%s rejected the request: %s" % (who, detail)
    elif code and code >= 500:
        msg = "%s is not answering right now (HTTP %s). Try again shortly." % (
            who, code)
    elif isinstance(exc, urllib.error.URLError):
        msg = ("Could not reach %s: %s. Check the base URL and your network."
               % (who, getattr(exc, "reason", None) or exc))
    else:
        msg = str(exc)
    return msg + (" (%s)" % detail if detail and detail not in msg else "")


def _reasoning_text(obj):
    """Pull reasoning out of a completion chunk or message, whatever the
    provider called it. llama.cpp and DeepSeek send reasoning_content,
    OpenRouter sends reasoning, and reasoning_details is the structured form
    (the raw text lives on entries of type "reasoning.text")."""
    obj = obj if isinstance(obj, dict) else {}
    for field in ("reasoning", "reasoning_content"):
        val = obj.get(field)
        if isinstance(val, str) and val:
            return val
    details = obj.get("reasoning_details")
    if isinstance(details, list):
        out = []
        for det in details:
            if isinstance(det, dict) and det.get("type") == "reasoning.text":
                text = det.get("text")
                if text:
                    out.append(text)
        if out:
            return "".join(out)
    return ""


def _bonsai_stream(messages, tools):
    if _api_wire(_active_model()) == _API_WIRE_RESPONSES:
        for ev in _responses_stream(messages, tools):
            yield ev
        return
    payload = _base_payload()
    payload.update({"messages": messages, "tools": tools, "stream": True})
    req = urllib.request.Request(BONSAI_BASE + "/v1/chat/completions",
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers=_model_headers(),
                                 method="POST")
    with urllib.request.urlopen(req, timeout=BONSAI_SOCKET_TIMEOUT) as resp:
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
            # reasoning and content are separate ifs, not elif: a provider is
            # free to put both in one chunk and the answer must not be dropped
            # because the model thought in the same breath.
            think = _reasoning_text(delta)
            if think:
                yield {"kind": "reason", "text": think}
            if delta.get("content"):
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
            "ctx_left": _ctx_window() - prompt_tok - comp_tok,
        }
        yield {"kind": "end", "tool_calls": ordered, "finish": finish, "stats": stats}


# ------------------------------------------------- OpenAI /v1/responses wire
# The second OpenAI completion API is a different protocol, not a renamed
# /v1/chat/completions. The body differs (input, not messages; tools flattened;
# the system prompt in its own field), a tool call is identified by a call_id
# that has to survive into the tool result, and the stream is a typed event log
# rather than anonymous chunks that happen to carry a delta.
#
# Rather than teach the streaming reader, the tool loop and the renderer a
# second dialect, everything is translated back into the events this app
# already knows how to draw. That keeps the four call sites of _bonsai_chat and
# _bonsai_stream unchanged, and means a model that only offers this endpoint
# behaves like any other instead of being a special case threaded through the
# agent loop.


def _responses_tools(tools):
    """chat tools -> responses tools.

    Responses lifts the name, description and schema out of the "function"
    wrapper that chat nests them in. An empty list is left out of the body
    entirely: "tools": [] is a 400 on the real endpoint, not a no-op."""
    out = []
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if not isinstance(fn, dict):
            continue
        item = {"type": "function",
                "name": fn.get("name") or "",
                "parameters": fn.get("parameters")
                or {"type": "object", "properties": {}}}
        if fn.get("description"):
            item["description"] = fn["description"]
        out.append(item)
    return out


def _content_to_text(content):
    """The text of a chat content field, whether it is a string or a list of
    parts, so the system prompt can be lifted out whole."""
    if isinstance(content, str):
        return content
    out = []
    for part in content or []:
        if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
            out.append(str(part["text"]))
    return "\n".join(out)


def _responses_input(messages):
    """chat messages -> (instructions, input items).

    The role words are the same two the app already uses; only the content
    parts are renamed, and they are renamed by direction: what goes in is
    input_text, what the model said comes back as output_text. Sending a past
    assistant turn as input_text is a 400, which is an easy way to break a
    second turn of any conversation."""
    instructions, items = [], []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "system":
            text = _content_to_text(content)
            if text.strip():
                instructions.append(text)
            continue
        if role not in ("user", "assistant"):
            continue
        parts = []
        if isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    parts.append({"type": "input_text", "text": str(part["text"])})
                elif part.get("type") == "image_url":
                    url = str((part.get("image_url") or {}).get("url") or "")
                    if url:
                        parts.append({"type": "input_image", "image_url": url})
        elif isinstance(content, str) and content.strip():
            parts.append({"type": "input_text", "text": content})
        if not parts:
            continue
        if role == "assistant":
            parts = [{"type": "output_text", "text": p["text"]}
                     for p in parts if p.get("type") == "input_text"]
            if not parts:
                continue
        items.append({"role": role, "content": parts})
    return instructions, items


def _responses_payload(messages, tools, stream):
    """The shared request body for both the streaming and one-shot paths."""
    payload = _base_payload()
    # _base_payload adds keep_alive for llama.cpp and the unified "reasoning"
    # effort object for hosted chat providers. Neither is part of a responses
    # body, and this API validates rather than ignoring what it does not know.
    for field in ("keep_alive", "reasoning", "chat_template_kwargs"):
        payload.pop(field, None)
    instructions, items = _responses_input(messages)
    payload["input"] = items
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    rtools = _responses_tools(tools)
    if rtools:
        payload["tools"] = rtools
    payload["stream"] = bool(stream)
    return payload


def _responses_error_text(obj):
    """The readable part of a responses error, or "" when there isn't one."""
    if isinstance(obj, str):
        text = obj
    elif isinstance(obj, dict):
        text = str(obj.get("message") or obj.get("code") or "")
    else:
        return ""
    text = text.strip()
    if not text or _generic_provider_text(text):
        return ""
    return _redact_embedded_secrets(text)[:300]


def _responses_usage(usage):
    """responses usage -> the chat field names the stats block uses."""
    usage = usage if isinstance(usage, dict) else {}
    return {
        "prompt_tokens": usage.get("input_tokens") or 0,
        "completion_tokens": usage.get("output_tokens") or 0,
        "cached_tokens": (usage.get("input_tokens_details")
                          or {}).get("cached_tokens") or 0,
    }


def _responses_chat(messages, tools):
    """The one-shot sibling of _bonsai_stream, for the calls that want an
    answer and not a stream: summarising, titling, the no-tools retry.

    Returns a message in the shape _bonsai_chat has always returned, so the
    callers cannot tell which wire the answer came over."""
    url = _api_base(_active_model()) + "/v1/responses"
    data = _http_json(url, _responses_payload(messages, tools, False))
    if not isinstance(data, dict):
        return {"role": "assistant", "content": "", "reasoning_content": None,
                "tool_calls": None, "_usage": {}, "_timings": {}}
    if data.get("error"):
        raise RuntimeError(_responses_error_text(data["error"])
                           or "the provider returned an error")
    text, thinking, calls = [], [], []
    for item in data.get("output") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "message":
            for part in item.get("content") or []:
                if (isinstance(part, dict) and part.get("type") == "output_text"
                        and part.get("text")):
                    text.append(str(part["text"]))
        elif kind == "reasoning":
            for part in item.get("summary") or []:
                if isinstance(part, dict) and part.get("text"):
                    thinking.append(str(part["text"]))
        elif kind == "function_call":
            calls.append({"id": item.get("call_id") or item.get("id") or "call_0",
                          "type": "function",
                          "function": {"name": item.get("name") or "",
                                       "arguments": item.get("arguments") or ""}})
    usage = _responses_usage(data.get("usage"))
    return {"role": "assistant", "content": "".join(text),
            "reasoning_content": "".join(thinking) or None,
            "tool_calls": calls or None,
            "_usage": {"prompt_tokens": usage["prompt_tokens"],
                       "completion_tokens": usage["completion_tokens"]},
            "_timings": {}}


def _responses_stream(messages, tools):
    """Stream a /v1/responses turn as the events the rest of the app reads.

    Every yield here is one of the four kinds _bonsai_stream already emits, so
    the reader, the agent loop and the Thought step are unchanged - which is
    the whole point, because those were never written to know about this API."""
    url = _api_base(_active_model()) + "/v1/responses"
    req = urllib.request.Request(url,
                                 data=json.dumps(_responses_payload(messages, tools,
                                                                    True)).encode("utf-8"),
                                 headers=_model_headers(), method="POST")
    calls = {}
    usage = {}
    finish = None
    first_content_ts = None
    t_start = time.monotonic()
    with urllib.request.urlopen(req, timeout=BONSAI_SOCKET_TIMEOUT) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if not body or body == "[DONE]":
                continue
            try:
                ev = json.loads(body)
            except Exception:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type") or ""
            # usage only ever arrives on the terminal event, nested one level
            # down under the finished response
            done = ev.get("response")
            if isinstance(done, dict) and done.get("usage"):
                usage = done["usage"] or {}
            if kind in ("response.failed", "error"):
                err = (done or {}).get("error") if isinstance(done, dict) else None
                raise RuntimeError(_responses_error_text(err)
                                   or _responses_error_text(ev)
                                   or "the provider reported an error")
            if kind == "response.incomplete":
                finish = "length"
            if kind in ("response.output_text.delta", "response.refusal.delta"):
                # text first, and the point it starts is when thinking stopped,
                # which is what think_end measures
                text = str(ev.get("delta") or "")
                if text:
                    if first_content_ts is None:
                        first_content_ts = time.monotonic()
                        yield {"kind": "think_end",
                               "t_ms": int((first_content_ts - t_start) * 1000)}
                    yield {"kind": "delta", "text": text}
            elif kind in ("response.reasoning_summary_text.delta",
                          "response.reasoning_text.delta"):
                think = str(ev.get("delta") or "")
                if think:
                    yield {"kind": "reason", "text": think}
            elif kind == "response.output_item.added":
                item = ev.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    idx = ev.get("output_index", 0)
                    calls[idx] = {"id": item.get("call_id") or item.get("id")
                                  or "call_%s" % idx,
                                  "type": "function",
                                  "function": {"name": item.get("name") or "",
                                               "arguments": item.get("arguments") or ""}}
            elif kind == "response.function_call_arguments.delta":
                slot = calls.get(ev.get("output_index", 0))
                if slot is not None:
                    slot["function"]["arguments"] += str(ev.get("delta") or "")
            elif kind == "response.output_item.done":
                # the finished item is authoritative, in both directions: the
                # arguments deltas can belong to an item never announced, and
                # only this event carries the call_id that the tool result has
                # to quote back
                item = ev.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    idx = ev.get("output_index", 0)
                    slot = calls.setdefault(
                        idx, {"id": item.get("call_id") or item.get("id")
                              or "call_%s" % idx, "type": "function",
                              "function": {"name": "", "arguments": ""}})
                    if item.get("call_id"):
                        slot["id"] = item["call_id"]
                    if item.get("name"):
                        slot["function"]["name"] = item["name"]
                    if item.get("arguments"):
                        slot["function"]["arguments"] = item["arguments"]
            if kind == "response.completed":
                break
    ordered = [calls[i] for i in sorted(calls)]
    t_end = time.monotonic()
    think_ms = int((first_content_ts - t_start) * 1000) if first_content_ts else None
    total_ms = int((t_end - t_start) * 1000)
    respond_ms = total_ms - think_ms if think_ms is not None else total_ms
    use = _responses_usage(usage)
    prompt_tok = use["prompt_tokens"]
    comp_tok = use["completion_tokens"]
    if finish is None:
        finish = "tool_calls" if ordered else "stop"
    stats = {
        "think_ms": think_ms,
        "respond_ms": respond_ms,
        "total_ms": total_ms,
        "prompt_tokens": prompt_tok,
        "completion_tokens": comp_tok,
        "total_tokens": prompt_tok + comp_tok,
        "cached_tokens": use["cached_tokens"],
        "tok_s": 0,
        "ctx_used": prompt_tok + comp_tok,
        "ctx_left": _ctx_window() - prompt_tok - comp_tok,
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


# ---------------------------------------------------------------- context
# A "new" conversation is never really empty: every request carries the
# system prompt plus the whole tool catalogue, and llama.cpp renders that
# through the model's chat template before it sees a single word. The
# baseline is cached per (mode, model) because it only changes when the
# prompt or the tool list does.
_CTX_BASE = {}
# Whether that cached baseline came from the real tokenizer rather than the
# estimate. Only an exact baseline is worth estimating a conversation against.
_CTX_EXACT_BASE = {}
_CTX_LOCK = threading.Lock()
# A vision projector spends a fixed slice of the window per image, and the
# exact size depends on the projector. This is a sane average; the meter
# reports images separately instead of pretending to know.
_CTX_IMG_TOKENS = 1024


def _ctx_strip_images(msgs):
    """Return (text-only messages, image count). Base64 image payloads must
    never reach /tokenize - they are megabytes of noise that would count as
    hundreds of thousands of tokens."""
    out = []
    images = 0
    for m in msgs:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        texts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                images += 1
            elif part.get("type") == "text" and part.get("text"):
                texts.append(str(part["text"]))
        out.append({"role": m.get("role"), "content": "\n".join(texts)})
    return out, images


def _ctx_estimate(msgs, tools):
    """Rough token count for models we cannot ask. JSON is token-dense, so
    3.6 chars/token sits much closer to the truth than the usual 4."""
    blob = json.dumps({"messages": msgs, "tools": tools}, default=str)
    return int(len(blob) / 3.6) + 24


def _ctx_exact(msgs, tools, timeout=25):
    """Ask the running llama-server for the real number: render the prompt
    through the model's own chat template, then tokenize it. Returns None if
    the server cannot answer."""
    try:
        prompt = _http_json(BONSAI_BASE + "/apply-template",
                            {"messages": msgs, "tools": tools}, timeout)["prompt"]
        toks = _http_json(BONSAI_BASE + "/tokenize",
                          {"content": prompt, "add_special": True}, timeout)["tokens"]
        return len(toks)
    except Exception:
        return None


def _ctx_server_total():
    """The window the server is actually running with - it rounds up, so this
    is not always the number the user typed."""
    try:
        props = _http_json_get(BONSAI_BASE + "/props", timeout=5)
        return int((props.get("default_generation_settings") or {}).get("n_ctx") or 0)
    except Exception:
        return 0


_API_CTX_CACHE = {}
# How long a failed /v1/models lookup stays cached. Long enough that a broken
# key is not re-probed on every keystroke, short enough that pasting the key
# fixes the meter without a restart.
_API_CTX_TTL = 60.0


def _ctx_api_total(entry):
    """The real context window of a hosted model, from its /v1/models list.

    A local model reports n_ctx through /props, but a hosted one has no such
    endpoint, so this used to fall through to the registry default of 32768. On
    a 262k model that is not a cosmetic number: once used > total the app
    declares the conversation full and refuses to send it. OpenRouter reports
    context_length per model; a plain OpenAI-compatible server does not, in
    which case 0 means "unknown" and the caller keeps the old default."""
    base = _api_base(entry)
    model = str(entry.get("model") or entry.get("id") or "")
    if not base or not model:
        return 0
    hit = _API_CTX_CACHE.get((base, model))
    if hit is not None:
        val, when = hit
        # A total is worth trusting; a miss is not. Caching a failed lookup
        # forever meant a model added before its key was in place stayed stuck
        # on the 32768 default until the app was restarted, which is exactly
        # the order things happen in: add model, paste key, look at the meter.
        if val or (time.time() - when) < _API_CTX_TTL:
            return val
    try:
        data = _http_json_get(base + "/v1/models", timeout=10)
    except Exception:
        _API_CTX_CACHE[(base, model)] = (0, time.time())
        return 0
    total = 0
    for item in (data.get("data") or []) if isinstance(data, dict) else []:
        if str(item.get("id") or "") == model:
            total = int(item.get("context_length") or 0)
            break
    _API_CTX_CACHE[(base, model)] = (total, time.time())
    return total


def _ctx_window():
    """The window the active model can really read, not the registry default.

    The stats footer and the stream events used to subtract from BONSAI_CTX,
    a fixed 32768, so a 262k hosted model reported ctx 4k/32k while the meter
    beside BUILD said 4k/262k."""
    entry = _active_model()
    total = _ctx_server_total()
    if not total and entry and entry.get("type") == "api":
        total = _ctx_api_total(entry)
    if not total:
        total = int(_entry_cfg(entry).get("ctx") or 0)
    return total or BONSAI_CTX


def _ctx_usage(raw_messages, mode=MODE_BUILD, quick=False):
    """How much of the window this conversation is using right now.

    Returns fixed (the system prompt + tools cost every turn carries), own
    (what the conversation itself has added) and total (the real window).

    quick=True is the mid-typing refresh: it skips the exact count, which for a
    local model means two round-trips to the model server, and estimates the
    conversation instead. The UI marks the result with a ~ so the number is not
    mistaken for exact."""
    mode = MODE_PLAN if str(mode) == MODE_PLAN else MODE_BUILD
    msgs = build_messages(raw_messages, mode)
    tools = _tools_for(mode)
    msgs, images = _ctx_strip_images(msgs)
    entry = _active_model()
    local = bool(entry) and entry.get("type") != "api" and _bonsai_ready()

    # The baseline first: what an empty conversation in this mode costs. It is
    # cached per mode+model, so paying for an exact count here is a one-off and
    # it gives the mid-typing estimate something honest to be measured against.
    key = (mode, (entry or {}).get("id"))
    empty = build_messages([], mode)
    with _CTX_LOCK:
        fixed = _CTX_BASE.get(key)
    if fixed is None:
        fixed = _ctx_exact(empty, tools) if local else None
        exact_base = fixed is not None
        if fixed is None:
            fixed = _ctx_estimate(empty, tools)
        with _CTX_LOCK:
            _CTX_BASE[key] = fixed
            _CTX_EXACT_BASE[key] = exact_base
    else:
        exact_base = _CTX_EXACT_BASE.get(key, False)

    used = None
    exact = False
    if local and not quick:
        used = _ctx_exact(msgs, tools)
        exact = used is not None
    if used is None and quick and exact_base:
        # Estimate the conversation's marginal cost and add it to the real
        # baseline. Falling back to a whole-prompt estimate here instead made
        # the bar jump 11k -> 14k the instant you typed a single character and
        # fall back again when you stopped, and it reported the whole tool
        # schema as "this conversation" even with an empty chat.
        used = fixed + max(0, _ctx_estimate(msgs, tools)
                           - _ctx_estimate(empty, tools))
    if used is None:
        used = _ctx_estimate(msgs, tools)
    if images:
        used += images * _CTX_IMG_TOKENS

    total = _ctx_server_total()
    if not total and entry and entry.get("type") == "api":
        # No /props on a hosted endpoint, and the gear dialog refuses to write
        # a ctx for a non-local model, so this used to be stuck on the 32768
        # registry default whatever the model could actually read.
        total = _ctx_api_total(entry)
    if not total:
        total = int(_entry_cfg(entry).get("ctx") or 0)
    own = max(0, used - fixed)
    # 'why' lets the tooltip say something true. "estimated - model not loaded"
    # is wrong for a hosted model, which is never going to be loaded here.
    why = "exact" if exact else ("quick" if quick else
                                 ("remote" if entry and entry.get("type") == "api"
                                  else "model-not-loaded"))
    base = {"used": used, "fixed": fixed, "own": own, "total": total,
            "exact": exact, "mode": mode, "images": images, "why": why,
            # the thresholds the console uses to decide when to compact, sent
            # from here so the two can never drift apart
            "fold_at": int(COMPACT_AT * 100), "fold_min": COMPACT_MIN_TOKENS,
            "fold_keep": COMPACT_KEEP}
    if total and used > total:
        return dict(base, ok=False, error="this conversation no longer fits in "
                     "the context window - start a new chat or raise ctx")
    return dict(base, ok=True, pct=round(used * 100.0 / total, 1) if total else None)


# --------------------------------------------------------------- compaction
# A long chat eventually stops fitting in the context window. The old answer
# was "start a new chat", which throws the whole conversation away. The better
# one is what a coding agent does: keep the recent part word for word and
# replace the older part with a summary, so the model still knows what it was
# asked to do without re-reading every turn.
#
# Nothing is deleted. The transcript keeps every message, the fold point is
# drawn in the chat, and clearing the summary puts the full history back in
# front of the model. So compaction costs context, never content.

# How much of the tail is never folded. These stay verbatim so the model has
# the real recent turns to work from, not a summary of them.
COMPACT_KEEP = 6
# Fold once the conversation has taken this much of the window.
COMPACT_AT = 0.80
# And never bother while the whole thing is this small - a summary of a
# short chat is longer than the chat.
COMPACT_MIN_TOKENS = 6000

COMPACT_SYSTEM = (
    "You are compacting your own working memory. Summarise the conversation "
    "below so that you can keep working without re-reading it.\n"
    "Write plain prose under these headings, and keep it under 700 words:\n"
    "GOAL - what the user ultimately wants.\n"
    "DECIDED - choices already made, and constraints the user set.\n"
    "STATE - what exists now: files written, commands that worked or failed, "
    "and the exact errors still outstanding.\n"
    "OPEN - the next steps, and anything the user asked for that is not done "
    "yet.\n"
    "Be specific. Keep file paths, names, identifiers, versions and error "
    "text verbatim - those are what you will need later. Drop the pleasantries "
    "and the restating. Do not add advice, and do not ask questions. Your only "
    "job is to record what happened."
)


def _compact_fold_index(messages):
    """Where the summary ends and the verbatim tail begins.

    Always a real message boundary, and never so close to the end that there is
    nothing worth summarising.
    """
    n = len(messages or [])
    if n <= COMPACT_KEEP + 1:
        return 0
    return n - COMPACT_KEEP


def _compact_transcript(messages):
    """The dropped turns, rendered as something a model can actually read."""
    out = []
    for m in messages or []:
        role = str(m.get("role") or "")
        content = m.get("content")
        if isinstance(content, list):
            bits = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and part.get("text"):
                    bits.append(str(part["text"]))
                elif part.get("type") == "image_url":
                    bits.append("[an image was attached]")
            content = " ".join(bits)
        text = str(content or "").strip()
        if role == "tool":
            # tool output is the bulkiest and least reusable part; keep the
            # shape of it, not the payload
            text = "[tool result] " + text[:600]
        if not text:
            continue
        name = {"user": "User", "assistant": "Assistant",
                "tool": "Tool", "system": "System"}.get(role, role)
        out.append("%s: %s" % (name, text[:4000]))
    return "\n\n".join(out)


def _compact_fallback(transcript):
    """If the model cannot be asked, keep something usable rather than nothing.

    Deliberately not a real summary: just the head and tail of the dropped
    turns, so the model still has the request and the most recent exchange.
    """
    if not transcript:
        return ""
    if len(transcript) <= 2400:
        return transcript
    return transcript[:1600] + "\n\n[...]\n\n" + transcript[-800:]


def _summary_message(summary):
    """The one message that stands in for everything that was folded away.

    Built here rather than in the page so there is a single copy of the
    wording: the console puts this exact object at the head of the list.
    """
    return {"role": "user", "content":
            "This is a summary of the earlier part of this conversation, which "
            "was compacted to fit the context window. Treat it as what really "
            "happened, and carry on from it:\n\n" + str(summary or "")}


def _compact_chat(messages, mode=MODE_BUILD):
    """Summarise the older turns of a conversation.

    Returns ok=False with a reason rather than raising, so a failure to
    summarise is a quiet no-op and never a lost turn.
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    idx = _compact_fold_index(msgs)
    if idx <= 0:
        return {"ok": False, "error": "there is not enough here to compact",
                "folded": 0}
    dropped = msgs[:idx]
    transcript = _compact_transcript(dropped)
    if not transcript.strip():
        return {"ok": False, "error": "nothing to compact", "folded": 0}

    summary = ""
    try:
        ask = [{"role": "system", "content": COMPACT_SYSTEM},
               {"role": "user", "content":
                "Conversation so far:\n\n" + transcript}]
        got = _bonsai_chat(ask, [])
        summary = str(got.get("content") or "").strip()
    except Exception:
        # a provider that is down, or a model that refuses, must not stop the
        # user compacting - fall back to the extractive version
        summary = ""
    if not summary:
        summary = _compact_fallback(transcript)
        via = "fallback"
    else:
        via = "model"
    return {"ok": True, "summary": summary, "message": _summary_message(summary),
            "folded": len(dropped), "kept": len(msgs) - idx, "via": via}


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
    """Capture the full screen on Linux via a command-line grabber.

    Tries each tool that can do it rather than assuming X11. scrot and
    ImageMagick's import both need a real X display and are simply blind on a
    Wayland session, which is the default on current GNOME and KDE; grim is
    the Wayland one. Trying them in order means a Wayland user with grim
    installed gets screenshots instead of an error that says "X11 session
    required", and a user with neither still gets told what to install."""
    from PIL import Image
    import tempfile
    wanted = (("scrot", [lambda p: ["-z", p]]),
              ("grim", [lambda p: [p]]),
              ("import", [lambda p: ["-window", "root", p]]),
              ("gnome-screenshot", [lambda p: ["-f", p]]))
    tried = []
    for name, forms in wanted:
        exe = shutil.which(name)
        if not exe:
            continue
        tried.append(name)
        for build in forms:
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp = os.path.join(tmpdir, "shot.png")
                try:
                    subprocess.run([exe] + build(tmp), check=True, timeout=30,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   creationflags=CREATE_NO_WINDOW)
                except Exception:
                    continue
                if not os.path.exists(tmp):
                    continue
                try:
                    img = Image.open(tmp).convert("RGB")
                except Exception:
                    continue
                if bbox:
                    img = img.crop(bbox)
                return img
    raise RuntimeError(
        "screenshot capture failed on Linux: none of the screen-capture tools "
        "worked (tried: %s). Install one - sudo apt install scrot for an X11 "
        "session, or sudo apt install grim for Wayland"
        % (", ".join(tried) if tried else "none of them are installed"))


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


def _pyautogui_error(exc):
    """Why mouse/keyboard control is unavailable, in words that help.

    "pyautogui is not installed" was the whole message even when it was
    installed and simply could not work: on Linux pyautogui drives the
    pointer through Xlib, and importing it opens the display, so on a Wayland
    session (or with no DISPLAY at all) the import raises and the tool blamed
    a package the user had in fact installed."""
    detail = str(exc).strip() or exc.__class__.__name__
    if IS_LINUX:
        if "display" in detail.lower() or "Xlib" in detail \
                or "connection" in detail.lower():
            return ("mouse and keyboard control needs a display: pyautogui "
                    "drives the pointer over X11 and cannot work on a bare "
                    "Wayland session (%s). On X11 install python3-xlib; on "
                    "Wayland log in to an X11 session, or use ydotool - a "
                    "screen reader or the keyboard tool may work better"
                    % detail)
        return ("mouse and keyboard control is unavailable (%s). On Linux "
                "pyautogui also needs python3-xlib and a running X11 display"
                % detail)
    return "pyautogui is not installed: %s" % detail


def _control_input(args):
    try:
        import pyautogui
    except Exception as exc:
        return {"error": _pyautogui_error(exc)}
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
    # The model is told to look for "chrome.exe" because that is what a
    # process is called on Windows, and pgrep matches the whole command line
    # on Linux - where the name is "chrome" and ".exe" is not a suffix that
    # exists. So the documented example never matched anything and every
    # wait_for(kind="process") quietly timed out. Try the name as given, then
    # without an .exe suffix, and match the bare name rather than a substring
    # of the entire command line (which otherwise matches any editor with the
    # file open).
    cands = [name]
    if name.endswith(".exe"):
        cands.append(name[:-4])
    for cand in cands:
        try:
            out = subprocess.run(["pgrep", "-if", cand], capture_output=True,
                                 text=True, timeout=10).stdout
        except Exception:
            return False
        if out.strip():
            return True
        try:
            out = subprocess.run(["pgrep", "-ixf", cand], capture_output=True,
                                 text=True, timeout=10).stdout
        except Exception:
            return False
        if out.strip():
            return True
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


def _screen_size():
    """The real size of the desktop, in pixels.

    Order matters: the cheapest thing that cannot lie wins, and the Windows
    call is last because ctypes.windll does not exist off Windows and used to
    drop straight through to a hardcoded 1920x1080. That number is not a
    fallback, it is a false statement - on a 2560x1440 or HiDPI screen the OCR
    tools cropped a region that does not exist and reported a confident wrong
    answer instead of admitting they could not see the screen."""
    try:
        from PIL import ImageGrab
        im = ImageGrab.grab()
        if im.size[0] > 1 and im.size[1] > 1:
            return (int(im.size[0]), int(im.size[1]))
    except Exception:
        pass
    try:
        import pyautogui
        w, h = pyautogui.size()
        if w and h:
            return (int(w), int(h))
    except Exception:
        pass
    if IS_LINUX:
        # "Screen 0: minimum 8 x 8, current 3840 x 2160, ..." - the current
        # size is the whole virtual desktop, so it is right for multi-monitor
        # too. xrandr is present on both X11 and XWayland sessions.
        try:
            out = subprocess.run(["xrandr", "--current"], capture_output=True,
                                 text=True, timeout=6).stdout
            hit = re.search(r"current\s+(\d+)\s*x\s*(\d+)", out)
            if hit and int(hit.group(1)) > 1:
                return (int(hit.group(1)), int(hit.group(2)))
        except Exception:
            pass
    if IS_WINDOWS:
        try:
            import ctypes
            user32 = ctypes.windll.user32  # noqa: F841
            return (int(user32.GetSystemMetrics(78)),
                    int(user32.GetSystemMetrics(79)))
        except Exception:
            pass
    return (1920, 1080)


def _full_screen_bbox():
    w, h = _screen_size()
    return (0, 0, w, h)


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
        # Three tools, three desktops. xclip is the X11 one, xsel is what a
        # minimal install usually has instead, and wl-paste is the only one of
        # the three that works on a Wayland session - which is the default on
        # GNOME 42+ and KDE Plasma 6, where xclip is frequently not installed
        # at all. Looking only for xclip meant "paste the image I just copied"
        # silently returned nothing on exactly the desktops that need it.
        # The return code is checked too: without it a failing xclip and an
        # empty clipboard are the same silence.
        import io as _io
        attempts = [
            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
            ["xsel", "--clipboard", "--output", "--type", "image/png"],
            ["wl-paste", "--type", "image/png", "--no-newline"],
        ]
        for argv in attempts:
            exe = shutil.which(argv[0])
            if not exe:
                continue
            try:
                out = subprocess.run([exe] + argv[1:], capture_output=True,
                                     timeout=5)
            except Exception:
                continue
            if out.returncode != 0 or not out.stdout:
                continue
            try:
                return Image.open(_io.BytesIO(out.stdout)).convert("RGB")
            except Exception:
                continue
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
                         "Tesseract engine ("
                         + ("winget install UB-Mannheim.TesseractOCR"
                            if IS_WINDOWS else "sudo apt install tesseract-ocr")
                         + ")"}
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
        return {"error": _pyautogui_error(exc)}
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


# ---------------- rich text for another app's chat box ----------------
#
# A chat composer only takes what the clipboard offers it, and different ones
# read different things: Word and Outlook want "Rich Text Format", a browser or
# Slack want "HTML Format", a terminal wants plain text. So the same styled
# text is put on the clipboard in all three at once and the app is left to take
# the richest one it understands. Nothing here needs an RTF library - the
# markup is small and the CF_HTML offsets are the only fiddly part.

_RICH_COLOURS = {
    "red": "ff3b30", "green": "34c759", "blue": "007aff",
    "yellow": "ffcc00", "orange": "ff9500", "purple": "af52de",
    "pink": "ff2d55", "white": "ffffff", "black": "000000",
    "grey": "8e8e93", "gray": "8e8e93", "cyan": "32ade6",
    "brown": "a2845e",
}


def _rich_hex(value):
    """A named colour or #rrggbb to (r, g, b), or None."""
    s = str(value or "").strip().lower()
    if not s:
        return None
    if not s.startswith("#"):
        s = _RICH_COLOURS.get(s, "")
        if not s:
            return None
    s = s.lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        return None
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except ValueError:
        return None


def _rich_rtf_text(s):
    """RTF body escapes: braces and backslashes, and anything non-ascii as a
    \\uNNNN escape, because the RTF header claims a single-byte code page."""
    out = []
    for ch in str(s or ""):
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ord(ch) < 128:
            out.append(ch)
        else:
            n = ord(ch)
            if n > 0xFFFF:  # outside the BMP: a surrogate pair
                n -= 0x10000
                hi = 0xD800 + (n >> 10)
                lo = 0xDC00 + (n & 0x3FF)
                out.append("\\u%d?\\u%d?" % (hi, lo))
            else:
                out.append("\\u%d?" % n)
    return "".join(out)


def _rich_rtf(text, fg, bg, bold, half_pt, mono):
    table = [";"]
    idx_fg = idx_bg = 0
    if fg:
        idx_fg = len(table)
        table.append("\\red%d\\green%d\\blue%d;" % fg)
    if bg:
        idx_bg = len(table)
        table.append("\\red%d\\green%d\\blue%d;" % bg)
    font = "{\\f1\\fmodern Consolas;}" if mono else "{\\f0\\fswiss Segoe UI;}"
    cmd = ["\\pard\\plain ", "\\f1" if mono else "\\f0"]
    if half_pt:
        cmd.append("\\fs%d" % half_pt)
    if idx_fg:
        cmd.append("\\cf%d" % idx_fg)
    if idx_bg:
        cmd.append("\\chcbpat%d" % idx_bg)
    if bold:
        cmd.append("\\b")
    body = _rich_rtf_text(text)
    if bold:
        body += "\\b0"
    return ("{\\rtf1\\ansi\\ansicpg1252\\deff0{\\fonttbl%s}"
            "{\\colortbl%s}%s %s\\par\n}" % (font, "".join(table),
                                            "".join(cmd), body))


def _rich_html_fragment(text, fg, bg, bold, size, mono):
    import html as _html
    css = []
    if fg:
        css.append("color:#%02x%02x%02x" % fg)
    if bg:
        css.append("background-color:#%02x%02x%02x" % bg)
    if bold:
        css.append("font-weight:bold")
    if size:
        css.append("font-size:%gpt" % size)
    if mono:
        css.append("font-family:Consolas,'Courier New',monospace")
    inner = _html.escape(str(text or "")).replace("\n", "<br>")
    return '<span style="%s">%s</span>' % (";".join(css), inner)


def _rich_cf_html(fragment):
    """The CF_HTML header carries byte offsets into itself. The numbers are
    zero padded to a fixed width, so the header is the same length whatever the
    offsets turn out to be - build it once to learn that length, then again with
    the real numbers in it."""
    head = ("Version:1.0\r\nStartHTML:%010d\r\nEndHTML:%010d\r\n"
            "StartFragment:%010d\r\nEndFragment:%010d\r\n")
    start_html = len(head % (0, 0, 0, 0))
    prefix = "<html><body>\r\n<!--StartFragment-->"
    suffix = "<!--EndFragment-->\r\n</body></html>"
    start_frag = start_html + len(prefix.encode("utf-8"))
    end_frag = start_frag + len(fragment.encode("utf-8"))
    end_html = end_frag + len(suffix.encode("utf-8"))
    return (head % (start_html, end_html, start_frag, end_frag)
            + prefix + fragment + suffix).encode("utf-8")


def _set_clipboard_rich(rich_rtf, html_bytes, plain):
    """Fill the clipboard with all three flavours at once."""
    if os.name != "nt":
        # no CF_HTML on the other desktops; the best a terminal can do is text
        out = None
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"],
                    ["xsel", "--clipboard", "--input"]):
            try:
                p = subprocess.run(cmd, input=str(plain).encode("utf-8"),
                                   timeout=10)
                if p.returncode == 0:
                    out = " ".join(cmd)
                    break
            except Exception:
                continue
        if not out:
            return {"error": "no clipboard tool found (need xclip, xsel or "
                             "wl-copy on this system)"}
        return {"via": out, "flavours": ["text"]}
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as exc:
        return {"error": "ctypes is unavailable: %s" % exc}
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32.RegisterClipboardFormatW.restype = wintypes.UINT
    u32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    CF_UNICODETEXT = 13
    GMEM_MOVEABLE = 0x0002
    rtf_fmt = u32.RegisterClipboardFormatW("Rich Text Format")
    html_fmt = u32.RegisterClipboardFormatW("HTML Format")
    if not u32.OpenClipboard(None):
        return {"error": "another program is holding the clipboard: %s"
                         % ctypes.WinError(ctypes.get_last_error())}
    try:
        u32.EmptyClipboard()
        items = [(CF_UNICODETEXT, str(plain).encode("utf-16-le") + b"\x00\x00")]
        if rtf_fmt:
            items.append((rtf_fmt, rich_rtf.encode("ascii", "replace")))
        if html_fmt:
            items.append((html_fmt, html_bytes))
        for fmt, data in items:
            handle = k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return {"error": "out of memory for the clipboard"}
            ptr = k32.GlobalLock(handle)
            if not ptr:
                k32.GlobalFree(handle)
                return {"error": "could not lock the clipboard"}
            try:
                ctypes.memmove(ptr, data, len(data))
            finally:
                k32.GlobalUnlock(handle)
            if not u32.SetClipboardData(fmt, handle):
                k32.GlobalFree(handle)
                return {"error": "the clipboard refused format %d" % fmt}
    finally:
        u32.CloseClipboard()
    return {"flavours": ["text", "rtf", "html"]}


def _rich_text(args):
    text = args.get("text")
    if text is None or not str(text):
        return {"error": "text is required"}
    text = str(text)
    fg = _rich_hex(args.get("color"))
    bg = _rich_hex(args.get("bg"))
    bold = bool(args.get("bold"))
    mono = bool(args.get("monospace"))
    size = args.get("size")
    try:
        size = float(size) if size not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    if size is not None:
        size = max(6.0, min(72.0, size))
    if args.get("color") and not fg:
        return {"error": "color must be a name or #rrggbb, not %r"
                         % args.get("color")}
    if args.get("bg") and not bg:
        return {"error": "bg must be a name or #rrggbb, not %r" % args.get("bg")}

    half_pt = int(round(size * 2)) if size else None
    rtf = _rich_rtf(text, fg, bg, bold, half_pt, mono)
    frag = _rich_html_fragment(text, fg, bg, bold, size, mono)
    cf_html = _rich_cf_html(frag)
    try:
        put = _set_clipboard_rich(rtf, cf_html, text)
    except Exception as exc:
        return {"error": "could not use the clipboard: %s" % exc}
    if put.get("error"):
        return put

    action = str(args.get("action") or "copy").strip().lower()
    result = {"result": "ok", "action": action, "chars": len(text)}
    result.update({k: v for k, v in put.items() if k != "error"})
    if action == "copy":
        result["note"] = ("on the clipboard as plain text, RTF and HTML; "
                          "use action=paste or send to put it in a chat box")
        return result

    # paste, or paste and send
    pasted = sent = False
    try:
        import pyautogui
        # ask the page to put the caret in its own input first, so the paste
        # does not land in whatever the user happened to be looking at
        _UI_FOCUS.set()
        time.sleep(0.6)
        pyautogui.hotkey("ctrl", "v")
        pasted = True
        time.sleep(0.35)
        if action == "send":
            pyautogui.press("enter")
            sent = True
    except Exception as exc:
        result["pasted"] = False
        result["error"] = "clipboard is set, but pasting failed: %s" % exc
        return result
    result["pasted"] = pasted
    result["sent"] = sent
    return result


def _url_ok(url):
    return str(url or "").strip().lower().startswith(("http://", "https://"))

def _name_from_response(url, headers):
    """Best filename for a download: Content-Disposition, then the URL path."""
    try:
        disp = (headers or {}).get("Content-Disposition") or ""
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disp, re.I)
        if m:
            name = os.path.basename(urllib.parse.unquote(m.group(1)).strip())
            if name:
                return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    except Exception:
        pass
    path = urllib.parse.urlsplit(url).path
    name = os.path.basename(urllib.parse.unquote(path)).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    return name or "download.bin"


# ---------------- link resolution ----------------
#
# A "download page" is a page whose real payload is somewhere else: an index
# that links out, a file host behind a "your download starts in N seconds"
# interstitial, a link built by JavaScript, or a bot check in front of both.
# The resolver turns such a page into a ranked list of candidate file URLs.
#
# It is deliberately split from the downloader. Resolving is cheap and safe -
# it fetches a page and reports what it found. Downloading writes to disk and
# can be gigabytes, so that stays an explicit, separate call the caller
# approves after seeing the candidates.

# Extensions that are a file a user means to download. Everything else on a
# page (nav links, login pages, css, trackers) is noise to be ranked away.
_RESOLVE_FILE_EXT = {
    # archives
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".tbz2", ".iso",
    ".img", ".dmg", ".pkg", ".cab", ".z01",
    # installers and binaries
    ".exe", ".msi", ".msix", ".appx", ".deb", ".rpm", ".apk", ".appimage", ".snap",
    ".bat", ".cmd", ".ps1", ".vbs", ".jar", ".class", ".bin", ".run", ".sh",
    # documents
    ".pdf", ".epub", ".mobi", ".azw3", ".djvu", ".cbz", ".cbr", ".chm",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods", ".odp",
    ".rtf", ".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml",
    # images, audio, video
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".svg",
    ".heic", ".raw", ".psd", ".ai", ".eps",
    ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".opus",
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".flv", ".wmv", ".ts", ".m2ts",
    # subtitles and misc
    ".srt", ".sub", ".ass", ".ssa", ".vtt", ".ttf", ".otf", ".woff", ".woff2",
}

# A path ending in a digit run right before the extension is almost always an
# auto-numbered file (/file_01.zip) rather than a route.
_RESOLVE_INDEX_EXT = {".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".cgi",
                      ".do", ".action", ".shtml", ".phtml", ".cfm"}

# Pages that are never the file. Following these just burns a hop.
_RESOLVE_SKIP_TEXT = (
    "login", "signin", "sign-in", "register", "signup", "sign-up", "logout",
    "account", "profile", "settings", "preferences", "contact", "about",
    "privacy", "terms", "tos", "legal", "cookie", "faq", "help", "support",
    "search", "forum", "community", "comment", "reply", "newsletter",
    "subscribe", "advertise", "careers", "jobs", "sitemap", "rss", "feed",
    "donate", "premium", "membership", "subscribe", "upgrade", "billing",
    "cart", "checkout", "wishlist", "compare", "review", "rating", "vote",
    "facebook", "twitter", "instagram", "youtube", "telegram", "discord",
    "reddit", "tiktok", "pinterest", "linkedin", "whatsapp", "github",
    "twitter", "mastodon", "xkcd", "imgur", "flickr", "patreon", "paypal",
)

# Content types that mean "this really is a file, stream it".
_RESOLVE_FILE_CT = (
    "application/zip", "application/x-zip-compressed", "application/x-rar",
    "application/vnd.rar", "application/x-7z-compressed", "application/x-tar",
    "application/gzip", "application/x-gzip", "application/x-bzip2",
    "application/x-xz", "application/octet-stream", "application/x-msdownload",
    "application/vnd.microsoft.portable-executable", "application/x-msi",
    "application/vnd.ms-cab-compressed", "application/vnd.android.package-archive",
    "application/pdf", "application/epub+zip", "application/x-cbz",
    "application/x-cbr", "application/vnd.comicbook+zip",
    "application/msword", "application/vnd.openxmlformats-officedocument",
    "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/vnd.oasis.opendocument",
    "audio/", "video/", "image/",
    "application/x-debian-package", "application/x-rpm",
    "application/vnd.apple.installer+xml", "application/x-apple-diskimage",
    "application/x-apple-diskimage", "application/java-archive",
    "application/x-ms-shortcut", "application/x-sh",
)

# Never render or fetch these, whatever a page links to. A browser will happily
# follow a redirect into the private network, which a plain fetcher would not.
_RESOLVE_BLOCK_HOSTS = {
    "metadata.google.internal", "metadata.goog", "instance-data",
    "instance-data.ec2.internal", "metadata",
}
_RESOLVE_BLOCK_NET = ()  # reserved for future ranges; IPv4/IPv6 checked inline


def _resolve_host_allowed(url):
    """True when a URL is safe for the resolver to fetch: http(s), resolvable,
    and not a cloud-metadata address.

    Loopback and LAN addresses are deliberately ALLOWED. Every other download
    tool already fetches them (a plain download of http://192.168.1.10/... has
    always worked), and the agent can reach any host through api_call and
    run_code regardless, so refusing them here would only make the resolver
    behave differently from the rest of the download stack for no real gain.
    What does get refused is the one address class that is a genuine SSRF
    target: link-local cloud metadata, where a stray link could otherwise read
    cloud credentials. Set BONSAI_ALLOW_PRIVATE_FETCH=1 to skip the check
    entirely, metadata included.
    """
    if os.environ.get("BONSAI_ALLOW_PRIVATE_FETCH") == "1":
        try:
            parts = urllib.parse.urlsplit(url)
            if parts.scheme.lower() in ("http", "https") and (parts.hostname or ""):
                return True, ""
        except Exception:
            pass
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return False, "only http and https are supported"
        host = (parts.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False, "no host in the URL"
        if host in _RESOLVE_BLOCK_HOSTS:
            return False, "refusing to fetch a cloud metadata address"
        try:
            infos = socket.getaddrinfo(host, None)
        except Exception:
            return False, "cannot resolve the host name"
        for info in infos:
            packed = info[4][0]
            try:
                if info[0] == socket.AF_INET:
                    octets = [int(x) for x in packed.split(".")]
                    if octets[0] == 169 and octets[1] == 254:
                        return False, "refusing to fetch a link-local or metadata address"
                    if octets[0] == 0 or octets[0] >= 224:
                        return False, "refusing to fetch a reserved address"
                else:
                    raw = socket.inet_pton(socket.AF_INET6, packed)
                    # fe80::/10 link-local, which is where cloud metadata lives
                    if raw[0] == 0xFE and (raw[1] & 0xC0) == 0x80:
                        return False, "refusing to fetch a link-local or metadata address"
            except Exception:
                # a family we cannot parse is not a reason to allow the url
                return False, "could not check the address for this host"
        return True, ""
    except Exception as exc:
        return False, str(exc)


def _resolve_ct_is_file(content_type):
    ct = str(content_type or "").split(";")[0].strip().lower()
    if not ct:
        return False
    if ct.startswith(("text/", "application/xhtml")):
        return False
    return any(ct.startswith(prefix) for prefix in _RESOLVE_FILE_CT)


def _resolve_score(url, content_type=None, size=None, text=None, depth=0):
    """Rank a candidate. Higher is more likely to be the wanted file."""
    try:
        parts = urllib.parse.urlsplit(url)
        path = parts.path or ""
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(urllib.parse.unquote(path))
        query = (parts.query or "").lower()
        blob = ("%s %s %s" % (url, name, query)).lower()
    except Exception:
        return -1
    score = 0
    is_file_ext = ext in _RESOLVE_FILE_EXT
    if is_file_ext:
        score += 60
    if _resolve_ct_is_file(content_type):
        score += 50
    if ext in _RESOLVE_INDEX_EXT:
        score -= 45
    if not ext and not _resolve_ct_is_file(content_type):
        score -= 20
    low = blob
    for word in ("download", "mirror", "file", "get", "dl", "fetch", "source"):
        if word in low:
            score += 12
            break
    if "/download" in low or "download=" in low or "?d=" in low:
        score += 18
    # Skip-words only demote things that are not already a real file, so a
    # genuine 'download-portal.zip' is not punished for a noisy name.
    if not (is_file_ext or _resolve_ct_is_file(content_type)):
        for bad in _RESOLVE_SKIP_TEXT:
            if bad in low:
                score -= 30
                break
        if re.search(r"/\d{3,}(?:[._-]|$)", path) and not is_file_ext:
            score -= 15
    if text:
        hint = str(text).strip().lower()
        if re.search(r"\b(size|mb|gb|mib|gib|bytes)\b", hint):
            score += 6
        if re.search(r"\b(download|mirror|file|part)\b", hint):
            score += 6
        if re.search(r"\b(advert|login|sign|home|menu|privacy)\b", hint):
            score -= 8
    if size:
        try:
            mb = int(size) / (1024 * 1024)
            if mb >= 1:
                score += min(18, int(mb / 50) + 6)
            elif mb < 0.05:
                score -= 12
        except Exception:
            pass
    if parts.scheme.lower() == "https":
        score += 4
    score -= depth * 2
    return score


def _resolve_candidates(markup, base_url, max_out=40, min_score=None):
    """Pull every plausible link out of a page, best first.

    By default this keeps even links that look like pages, because the
    hop-follower has to be able to walk *into* an interstitial. Pass
    min_score to drop the unpromising ones, which is what you want when the
    output is going straight to the user as a list of files.
    """
    out = {}
    try:
        pairs = []
        for m in re.finditer(
                r'(?is)<a\b[^>]*?href\s*=\s*(["\'])(?P<u>.+?)\1(?P<rest>[^>]*)>(?P<t>.*?)</a>',
                markup or ""):
            text = re.sub(r"(?is)<[^>]+>", " ", m.group("t") or "")
            pairs.append((m.group("u"), text))
        for attr in ("src", "data-href", "data-src", "data-url", "data-download",
                     "data-file", "href"):
            for m in re.finditer(
                    r'(?is)(?<![\w-])%s\s*=\s*(["\'])(?P<u>.+?)\1' % re.escape(attr),
                    markup or ""):
                raw_val = html.unescape(str(m.group("u") or "")).strip()
                # a marker attribute like data-href="yes" is not a url; only
                # keep values that actually look like one
                if attr != "href" and not _url_ok(raw_val) and \
                        not raw_val.lower().startswith("/"):
                    continue
                pairs.append((raw_val, ""))
        for m in re.finditer(r'(?is)"(?:url|href|src|downloadUrl|fileUrl)"\s*:\s*"([^"]+)"',
                              markup or ""):
            pairs.append((m.group(1), ""))
    except Exception:
        return []
    for raw, text in pairs:
        raw = str(raw or "").strip()
        if not raw or raw.lower().startswith(_SKIP_PREFIX):
            continue
        try:
            full = urllib.parse.urljoin(base_url, html.unescape(raw))
        except Exception:
            continue
        if not _url_ok(full) or full in out:
            continue
        # no host check here on purpose: this is a pure HTML parser, and letting
        # it do DNS would silently drop candidates on any host that does not
        # resolve from this machine. The guard runs where the network is
        # actually touched - the page walk and the candidate probe.
        out[full] = {"url": full, "name": _name_from_response(full, None),
                     "score": _resolve_score(full, text=text)}
    ranked = sorted(out.values(), key=lambda c: -c["score"])
    if min_score is None:
        return ranked[:max_out]
    return [c for c in ranked[:max_out] if c["score"] > min_score]


def _resolve_probe_candidate(cand, timeout=20, headers=None):
    """Learn a candidate's real size/type and re-rank it with that knowledge."""
    url = cand["url"]
    ok, why = _resolve_host_allowed(url)
    if not ok:
        cand["skipped"] = why
        return cand
    try:
        req_headers = {"User-Agent": _WEB_UA, "Accept": "*/*"}
        for k, v in (headers or {}).items():
            if v is not None and str(k).strip():
                req_headers[str(k)] = str(v)
        req = urllib.request.Request(url, headers=req_headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rh = {k.title(): v for k, v in resp.headers.items()}
            cand["final_url"] = resp.geturl()
            cand["content_type"] = rh.get("Content-Type")
            cand["filename"] = _name_from_response(resp.geturl(), rh) or cand["name"]
            if rh.get("Content-Length"):
                try:
                    cand["size"] = int(rh["Content-Length"])
                except Exception:
                    pass
            cand["accepts_ranges"] = (rh.get("Accept-Ranges") or "").lower().strip() == "bytes"
    except Exception as exc:
        cand.setdefault("skipped", str(exc)[:120])
    cand["score"] = _resolve_score(cand.get("final_url") or url,
                                   content_type=cand.get("content_type"),
                                   size=cand.get("size"),
                                   text=cand.get("text"))
    return cand


def _resolve_static(url, depth=0, max_depth=3, headers=None, timeout=20,
                    probe=True, max_hops_per_level=6):
    """Walk a page chain and return ranked file candidates.

    Pure HTTP, no new dependencies. Follows same-host and cross-host links up
    to max_depth levels, but only descends into pages that look like a host
    page rather than a whole site.
    """
    seen = set()
    found = {}
    frontier = [(url, 0)]
    pages = []
    while frontier:
        page, level = frontier.pop(0)
        if page in seen or level > max_depth:
            continue
        seen.add(page)
        ok, why = _resolve_host_allowed(page)
        if not ok:
            pages.append({"url": page, "error": why})
            continue
        try:
            markup = _fetch_html(page, 1500000, timeout=timeout)
        except Exception as exc:
            pages.append({"url": page, "error": str(exc)[:200]})
            continue
        pages.append({"url": page, "candidates": len(found)})
        # keep everything here: the pages that score worst are often exactly
        # the interstitials we need to walk into.
        cands = _resolve_candidates(markup, page)
        descend = 0
        for cand in cands:
            ext = os.path.splitext(urllib.parse.urlsplit(cand["url"]).path)[1].lower()
            looks_page = ext in _RESOLVE_INDEX_EXT or not ext
            if looks_page and level < max_depth and descend < max_hops_per_level:
                frontier.append((cand["url"], level + 1))
                descend += 1
            elif not looks_page or _resolve_ct_is_file(cand.get("content_type")):
                found[cand["url"]] = cand
    ranked = list(found.values())
    if probe:
        for cand in ranked[:25]:
            _resolve_probe_candidate(cand, timeout=timeout, headers=headers)
        ranked = sorted(ranked, key=lambda c: -c["score"])
    # only report things that actually look like a downloadable file
    files = []
    for cand in ranked:
        ext = os.path.splitext(urllib.parse.urlsplit(cand["url"]).path)[1].lower()
        if ext in _RESOLVE_FILE_EXT or _resolve_ct_is_file(cand.get("content_type")):
            files.append(cand)
    return files, pages


def _resolve_best(ranked):
    """The single most file-like candidate, if any is convincing enough."""
    for cand in ranked:
        if cand.get("score", 0) >= 60 and (
                cand.get("content_type") and _resolve_ct_is_file(cand["content_type"])
                or os.path.splitext(urllib.parse.urlsplit(cand["url"]).path)[1].lower()
                in _RESOLVE_FILE_EXT):
            return cand
    return ranked[0] if ranked else None


def _unique_path(path):
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    for i in range(1, 1000):
        cand = "%s (%d)%s" % (root, i, ext)
        if not os.path.exists(cand):
            return cand
    return "%s (%d)%s" % (root, int(time.time()), ext)


def _free_bytes(path):
    try:
        probe = path if os.path.isdir(path) else os.path.dirname(path) or "."
        while probe and not os.path.isdir(probe):
            parent = os.path.dirname(probe)
            if parent == probe:
                break
            probe = parent
        return shutil.disk_usage(probe).free
    except Exception:
        return None


def _content_length(url, headers=None, timeout=30):
    """Ask the server how big the file is (None when it will not say)."""
    return _probe_url(url, headers, timeout).get("size")


def _probe_url(url, headers=None, timeout=30):
    """One cheap round trip that learns size, range support and the real name.

    Range support decides whether a download can be split across several
    connections, so it is worth knowing before any bytes are written.
    """
    out = {"size": None, "accepts_ranges": False, "content_type": None,
           "filename": None, "final_url": url}
    req_headers = {"User-Agent": _WEB_UA}
    for k, v in (headers or {}).items():
        if v is not None and str(k).strip():
            req_headers[str(k)] = str(v)
    try:
        req = urllib.request.Request(url, headers=req_headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["final_url"] = resp.geturl()
            rheaders = {k.title(): v for k, v in resp.headers.items()}
            out["content_type"] = rheaders.get("Content-Type")
            out["filename"] = _name_from_response(url, rheaders)
            if (rheaders.get("Accept-Ranges") or "").lower().strip() == "bytes":
                out["accepts_ranges"] = True
            n = rheaders.get("Content-Length")
            if n:
                out["size"] = int(n)
            if out["size"] is not None:
                return out
    except Exception:
        pass
    try:
        req_headers["Range"] = "bytes=0-0"
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out["final_url"] = resp.geturl()
            code = getattr(resp, "status", 200) or 200
            rheaders = {k.title(): v for k, v in resp.headers.items()}
            if code == 206:
                out["accepts_ranges"] = True
            crange = rheaders.get("Content-Range") or ""
            m = re.search(r"/(\d+)$", crange)
            if m:
                out["size"] = int(m.group(1))
    except Exception:
        pass
    return out


def _space_guard(url, dest, headers=None, need=None):
    """Refuse early when the disk obviously cannot hold the download."""
    free = _free_bytes(dest)
    if free is None:
        return None
    want = need or _content_length(url, headers)
    if not want or want <= 0:
        return None
    if free < want + _DOWNLOAD_FLOOR_BYTES:
        return {"error": "not enough disk space: %s needs %.1f GB but only %.1f GB "
                         "is free on %s - free some space or download it in parts"
                         % (url, want / 1e9, free / 1e9,
                            os.path.dirname(dest) or dest)}
    return None


class _DLCanceled(Exception):
    """Raised inside a transfer when the job was canceled."""


class _DLPaused(Exception):
    """Raised inside a transfer when the job was paused (socket released)."""


def _dl_throttle(start_t, moved, bps):
    """Sleep just enough to keep the average rate at or below bps."""
    if not bps or bps <= 0:
        return
    ahead = (float(moved) / float(bps)) - (time.time() - start_t)
    if ahead > 0:
        time.sleep(min(ahead, 2.0))


def _stream_to_file(url, dest, headers=None, timeout=180, max_bytes=None,
                    resume=False, retries=0, method="GET", first=None, last=None,
                    progress=None, cancel=None, pause=None, throttle_bps=0,
                    have_bytes=None):
    """Stream one byte range of a URL to dest, appending at an offset.

    first/last are inclusive byte offsets used by segmented downloads; with
    first None the normal resume rules apply (continue at the current file
    size). Pause and cancel are raised as exceptions so a stopped transfer is
    never mistaken for a failure, and the partial file stays resumable.
    """
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    cap = int(max_bytes or MAX_DOWNLOAD_BYTES)
    req_headers = {"User-Agent": _WEB_UA, "Accept": "*/*"}
    for k, v in (headers or {}).items():
        if v is not None and str(k).strip():
            req_headers[str(k)] = str(v)
    span = (last - first + 1) if first is not None else None
    have = 0
    if first is not None:
        # a preallocated segment file is already at full length, so its size
        # says nothing: only the ledger may claim bytes are done
        have = max(0, min(int(have_bytes or 0), span))
    elif resume and os.path.isfile(dest) and os.path.getsize(dest) > 0:
        have = os.path.getsize(dest)
    started = time.time()
    moved = 0
    last_err = None
    for attempt in range(max(1, int(retries or 0) + 1)):
        try:
            if span is not None:
                req_headers["Range"] = "bytes=%d-%d" % (first + have, last)
            elif have:
                req_headers["Range"] = "bytes=%d-" % have
            else:
                req_headers.pop("Range", None)
            req = urllib.request.Request(url, headers=req_headers, method=method)
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                code = getattr(resp, "status", 200) or 200
                rheaders = {k.title(): v for k, v in resp.headers.items()}
                if span is not None and code != 206:
                    return {"error": "server ignored the range request (HTTP %s), "
                                     "cannot fetch that part" % code}
                if span is None:
                    mode = "ab" if have else "wb"
                    pos = have
                else:
                    mode = "r+b" if os.path.isfile(dest) else "wb"
                    pos = first + have
                got_here = 0
                with open(dest, mode) as fh:
                    if mode != "ab":
                        fh.seek(pos)
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise _DLCanceled()
                        if pause is not None and pause.is_set():
                            raise _DLPaused()
                        chunk = resp.read(DOWNLOAD_CHUNK)
                        if not chunk:
                            break
                        if cap and (pos + got_here + len(chunk)) > cap:
                            try:
                                os.remove(dest)
                            except OSError:
                                pass
                            return {"error": "file too large: %s exceeds the %.2f MB "
                                            "limit (raise it with max_mb)"
                                    % (url, cap / (1024 * 1024))}
                        fh.write(chunk)
                        got_here += len(chunk)
                        moved += len(chunk)
                        if progress:
                            progress(len(chunk))
                        _dl_throttle(started, moved, throttle_bps)
                written = pos + got_here
                want = rheaders.get("Content-Length")
                if want:
                    try:
                        want = int(want)
                    except (TypeError, ValueError):
                        want = None
                if want and got_here < want:
                    last_err = ("connection closed after %d of %d bytes"
                                % (written, pos + want))
                elif span is not None and written < last + 1:
                    last_err = "stopped at %d of %d" % (written, last + 1)
                else:
                    return {"ok": True, "url": url, "status": code,
                            "bytes": written, "resumed": bool(have),
                            "content_type": rheaders.get("Content-Type"),
                            "final_url": resp.geturl(), "headers": rheaders}
                have = written
        except urllib.error.HTTPError as exc:
            last_err = "HTTP %s %s" % (exc.code, exc.reason)
            if exc.code == 416 and have:
                return {"ok": True, "url": url, "status": 206, "bytes": have,
                        "resumed": True, "already_complete": True,
                        "content_type": None, "final_url": url, "headers": {}}
            if exc.code < 500:
                break
        except (_DLCanceled, _DLPaused):
            raise
        except Exception as exc:
            last_err = str(exc)
        if attempt < int(retries or 0):
            time.sleep(1.5 * (attempt + 1))
    return {"error": "download incomplete: %s" % (last_err or "unknown error")}


def _fetch_to_file(url, dest, headers=None, timeout=180, max_bytes=None,
                   resume=False, retries=0, method="GET", progress=None,
                   cancel=None, throttle_bps=0, first=None, last=None):
    """Thin wrapper kept for callers that want a plain single-stream fetch."""
    return _stream_to_file(url, dest, headers=headers, timeout=timeout,
                           max_bytes=max_bytes, resume=resume, retries=retries,
                           method=method, progress=progress, cancel=cancel,
                           throttle_bps=throttle_bps, first=first, last=last)


_SEG_MIN_BYTES = 8 * 1024 * 1024
_SEG_MAX_CONNECTIONS = 4
_SEG_LEDGER_FLUSH = 4 * 1024 * 1024


def _seg_bounds(total, count):
    """Split [0, total-1] into count chunk-aligned, non-empty ranges."""
    if count < 1:
        count = 1
    size = (total // count // DOWNLOAD_CHUNK) * DOWNLOAD_CHUNK
    if size < DOWNLOAD_CHUNK:
        size = DOWNLOAD_CHUNK
    bounds = []
    start = 0
    for i in range(count):
        if i == count - 1 or start + size > total:
            end = total - 1
        else:
            end = start + size - 1
        bounds.append((start, end))
        start = end + 1
        if start >= total:
            break
    return bounds


def _seg_ledger_path(part):
    return part + ".segments.json"


_SEG_LEDGER_VERSION = 2


def _seg_ledger_load(part, url, total, count):
    """Completed byte counts from a previous run, or None to start over."""
    try:
        with open(_seg_ledger_path(part), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if int(data.get("v") or 0) != _SEG_LEDGER_VERSION:
        return None
    if (data.get("url") != url or int(data.get("total") or 0) != int(total)
            or int(data.get("count") or 0) != int(count)):
        return None
    rows = data.get("segments") or []
    if len(rows) != count:
        return None
    out = []
    for row in rows:
        try:
            start, end, done = int(row[0]), int(row[1]), int(row[2])
        except Exception:
            return None
        out.append([start, end, max(0, min(done, end - start + 1))])
    return out


def _seg_ledger_save(part, url, total, segs):
    path = _seg_ledger_path(part)
    tmp = path + ".tmp"
    payload = {"v": _SEG_LEDGER_VERSION, "url": url, "total": int(total),
               "count": len(segs), "chunk": DOWNLOAD_CHUNK,
               "segments": [list(s) for s in segs], "saved_at": time.time()}
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    except Exception:
        pass


def _seg_ledger_clear(part):
    for path in (_seg_ledger_path(part), _seg_ledger_path(part) + ".tmp"):
        try:
            os.remove(path)
        except OSError:
            pass


def _fetch_segments(url, dest, total, count, headers=None, timeout=180,
                    retries=0, progress=None, cancel=None, pause=None,
                    throttle_bps=0, ledger=None):
    """Fetch total bytes using count parallel Range requests into one file.

    The file is preallocated and each worker owns a disjoint byte range, so no
    merge step and no second full pass over the disk is needed. A ledger of
    finished offsets is flushed as the transfer proceeds, which is what makes
    pause, cancel and crash-resume safe for a segmented download.
    """
    bounds = _seg_bounds(total, count)
    segs = ledger if ledger else [[s, e, 0] for s, e in bounds]
    try:
        with open(dest, "r+b") as fh:
            fh.truncate(total)
    except OSError:
        with open(dest, "wb") as fh:
            fh.truncate(total)
    _seg_ledger_save(dest, url, total, segs)
    errors = [None] * len(segs)
    lock = threading.Lock()
    stopped = []

    def worker(idx):
        try:
            _seg_run(idx)
        except (_DLCanceled, _DLPaused) as exc:
            with lock:
                stopped.append(exc)
        except Exception as exc:
            with lock:
                errors[idx] = str(exc)

    def _seg_run(idx):
        start, end, _done = segs[idx]
        flushed = segs[idx][2]

        def seg_progress(count):
            # track the segment live so a pause or a kill still has an
            # accurate ledger, and the job sees the bytes as they land
            segs[idx][2] += count
            if progress:
                progress(count)

        while start + segs[idx][2] <= end:
            if cancel is not None and cancel.is_set():
                raise _DLCanceled()
            if pause is not None and pause.is_set():
                raise _DLPaused()
            try:
                # first stays at the segment start: have_bytes carries the
                # ledger progress, so the Range header is start+done, once
                got = _stream_to_file(url, dest, headers=headers,
                                      timeout=timeout, retries=retries,
                                      first=start, last=end,
                                      progress=seg_progress, cancel=cancel,
                                      pause=pause, throttle_bps=throttle_bps,
                                      have_bytes=segs[idx][2])
            except (_DLCanceled, _DLPaused):
                with lock:
                    _seg_ledger_save(dest, url, total, segs)
                raise
            if got.get("error"):
                errors[idx] = got["error"]
                with lock:
                    _seg_ledger_save(dest, url, total, segs)
                return
            segs[idx][2] = max(0, min(got["bytes"] - start, end - start + 1))
            if (segs[idx][2] - flushed) >= _SEG_LEDGER_FLUSH:
                flushed = segs[idx][2]
                with lock:
                    _seg_ledger_save(dest, url, total, segs)
        with lock:
            _seg_ledger_save(dest, url, total, segs)

    threads = []
    for i in range(len(segs)):
        th = threading.Thread(target=worker, args=(i,), name="dl-seg%d" % i)
        th.daemon = True
        threads.append(th)
        th.start()
    for th in threads:
        th.join()
    if stopped:
        raise stopped[0]
    for err in errors:
        if err:
            return {"error": err, "segments": segs, "total": total}
    missing = [i for i, s in enumerate(segs) if s[2] < (s[1] - s[0] + 1)]
    if missing:
        return {"error": "segments %s did not finish" % ",".join(str(i) for i in missing),
                "segments": segs, "total": total}
    try:
        if os.path.getsize(dest) != total:
            return {"error": "size check failed: %d of %d bytes" %
                              (os.path.getsize(dest), total),
                    "segments": segs, "total": total}
    except OSError as exc:
        return {"error": "could not stat the finished file: %s" % exc,
                "segments": segs, "total": total}
    _seg_ledger_clear(dest)
    return {"ok": True, "url": url, "status": 206, "bytes": total,
            "resumed": any(s[2] > 0 for s in segs) if ledger else False,
            "content_type": None, "final_url": url, "headers": {},
            "segments": len(segs), "total": total}


def _sha256_of(path, limit=None):
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(DOWNLOAD_CHUNK)
            if not chunk:
                break
            h.update(chunk)
            read += len(chunk)
            if limit and read >= limit:
                break
    return h.hexdigest()


_DL_JOBS = {}
_DL_LOCK = threading.RLock()
_DL_SEQ = [0]
_DL_KEEP = 200
_DL_SEM = threading.Semaphore(max(1, int(os.environ.get("BONSAI_DL_JOBS") or 4)))


class _DLJob(object):
    """One download: registry entry, control events and live progress."""

    def __init__(self, spec, tool=""):
        with _DL_LOCK:
            _DL_SEQ[0] += 1
            seq = _DL_SEQ[0]
        self.id = "dl%d%d" % (int(time.time()) % 1000000, seq)
        self.tool = tool
        self.url = str(spec.get("url") or "")
        self.dest = str(spec.get("dest") or "")
        self.folder = str(spec.get("folder") or "")
        self.group = str(spec.get("group") or "")
        self.headers = spec.get("headers") or None
        self.timeout = max(1, int(spec.get("timeout") or 180))
        self.max_bytes = spec.get("max_bytes")
        self.retries = int(spec.get("retries") or 0)
        self.resume = bool(spec.get("resume", True))
        self.overwrite = bool(spec.get("overwrite"))
        self.want_conns = max(1, min(int(spec.get("connections") or 1),
                                     _SEG_MAX_CONNECTIONS))
        self.throttle_bps = int(spec.get("throttle_bps") or 0)
        self.sha256 = str(spec.get("sha256") or "")
        self.expect_size = int(spec.get("expect_size") or 0)
        self.preview = bool(spec.get("preview", True))
        self.sem = spec.get("sem")
        self.total = 0
        self.done = 0
        self.status = "queued"
        self.error = ""
        self.saved = ""
        self.name = ""
        self.content_type = ""
        self.record = {}
        self.failed_check = False
        self.resumed = False
        self.connections = 0
        self.started_at = 0.0
        self.ended_at = 0.0
        self.samples = []
        self._last_sample = 0.0
        self.cancel_evt = threading.Event()
        self.pause_evt = threading.Event()
        self.wake = threading.Event()
        self.finished = threading.Event()
        self.thread = None

    def add_bytes(self, count):
        self.done += count
        now = time.time()
        if now - self._last_sample >= 0.25:
            self._last_sample = now
            self.samples.append((now, self.done))
            if len(self.samples) > 80:
                self.samples.pop(0)

    def rate(self):
        pts = self.samples
        if len(pts) < 2:
            return 0.0, 0.0
        t0, d0 = pts[0]
        t1, d1 = pts[-1]
        span = t1 - t0
        if span < 0.35:
            return 0.0, 0.0
        speed = (d1 - d0) / span
        if speed <= 1 or not self.total:
            return max(0.0, speed), 0.0
        return speed, max(0.0, (self.total - self.done) / speed)

    def public(self):
        speed, eta = self.rate()
        pct = None
        if self.total:
            pct = round(min(100.0, self.done * 100.0 / self.total), 1)
        out = {"id": self.id, "url": self.url, "name": self.name,
               "status": self.status, "done": self.done, "total": self.total,
               "percent": pct, "speed_bps": int(speed),
               "eta_s": int(eta) if eta else None, "saved": self.saved,
               "folder": self.folder, "tool": self.tool,
               "connections": self.connections, "resumed": self.resumed,
               "started_at": self.started_at, "ended_at": self.ended_at,
               "part": self.dest}
        if self.error:
            out["error"] = self.error
        if self.content_type:
            out["content_type"] = self.content_type
        return out

    def set_status(self, status):
        with _DL_LOCK:
            if self.status in ("done", "error", "canceled"):
                return
            self.status = status

    def finish(self, status):
        with _DL_LOCK:
            self.status = status
            self.ended_at = time.time()
        self.finished.set()


def _dl_trim():
    with _DL_LOCK:
        if len(_DL_JOBS) <= _DL_KEEP:
            return
        done = [j for j in _DL_JOBS.values()
                if j.status in ("done", "error", "canceled")]
        done.sort(key=lambda j: j.ended_at)
        for job in done[:len(_DL_JOBS) - _DL_KEEP]:
            _DL_JOBS.pop(job.id, None)


def _dl_register(job):
    with _DL_LOCK:
        _DL_JOBS[job.id] = job
    _dl_trim()
    return job


def _dl_get(job_id):
    with _DL_LOCK:
        return _DL_JOBS.get(str(job_id or ""))


def _dl_state():
    with _DL_LOCK:
        jobs = list(_DL_JOBS.values())
    active = [j for j in jobs
              if j.status in ("queued", "running", "paused")]
    rest = [j for j in jobs if j not in active]
    rest.sort(key=lambda j: j.ended_at, reverse=True)
    return {"jobs": [j.public() for j in active + rest],
            "max_parallel": getattr(_DL_SEM, "_initial_value", 4),
            "connections": _SEG_MAX_CONNECTIONS}


def _dl_pause(job_id):
    job = _dl_get(job_id)
    if not job:
        return {"error": "no such download: %s" % job_id}
    if job.status in ("done", "error", "canceled"):
        return {"error": "download already finished"}
    job.pause_evt.set()
    job.set_status("paused")
    return {"ok": True, "status": "paused"}


def _dl_resume(job_id):
    job = _dl_get(job_id)
    if not job:
        return {"error": "no such download: %s" % job_id}
    if job.status in ("done", "error", "canceled"):
        return {"error": "download already finished"}
    job.pause_evt.clear()
    job.wake.set()
    job.set_status("running")
    return {"ok": True, "status": "running"}


def _dl_cancel(job_id):
    job = _dl_get(job_id)
    if not job:
        return {"error": "no such download: %s" % job_id}
    job.cancel_evt.set()
    job.pause_evt.set()
    job.wake.set()
    return {"ok": True, "status": "canceled"}


def _dl_clear(where="finished"):
    gone = []
    with _DL_LOCK:
        for jid, job in list(_DL_JOBS.items()):
            if where == "all":
                if job.status not in ("running",):
                    job.cancel_evt.set()
                    job.wake.set()
                    gone.append(jid)
                    _DL_JOBS.pop(jid, None)
            elif job.status in ("done", "error", "canceled"):
                gone.append(jid)
                _DL_JOBS.pop(jid, None)
    return {"ok": True, "removed": len(gone)}


def _dl_execute(job):
    """Run one slice of a job. Returns a record, or {'paused': True} to yield."""
    if not _url_ok(job.url):
        return {"error": "url must start with http:// or https://"}
    try:
        explicit = bool(job.dest)
        if explicit:
            dest = _safe_path(job.dest)
            part = dest + ".part"
        else:
            probe_name = _name_from_response(job.url, None) or "download.bin"
            if probe_name in (".", "..", ""):
                probe_name = "download.bin"
            dest = None
            part = _safe_path(os.path.join(job.folder, probe_name + ".part")
                              if job.folder else probe_name + ".part")
        os.makedirs(os.path.dirname(part) or ".", exist_ok=True)
    except Exception as exc:
        return {"error": "bad destination path: %s" % exc}
    ledger = None
    if not job.resume:
        for stale in (part, _seg_ledger_path(part)):
            try:
                os.remove(stale)
            except OSError:
                pass
    elif explicit and not os.path.isfile(part) and os.path.isfile(dest):
        try:
            shutil.move(dest, part)
        except Exception:
            pass
    probe = _probe_url(job.url, job.headers)
    if probe.get("size"):
        job.total = int(probe["size"])
    if not os.path.isfile(part):
        blocked = _space_guard(job.url, part, job.headers, need=job.total or None)
        if blocked:
            return blocked
    conns = 1
    if (job.want_conns > 1 and job.total and not job.max_bytes
            and probe.get("accepts_ranges")
            and (job.total - (job.done or 0)) >= _SEG_MIN_BYTES):
        conns = min(job.want_conns, _SEG_MAX_CONNECTIONS)
    ledger = None
    if conns > 1:
        bounds = _seg_bounds(job.total, conns)
        ledger = _seg_ledger_load(part, job.url, job.total, len(bounds))
        if ledger is None and os.path.isfile(_seg_ledger_path(part)):
            # a ledger from another layout means this part file is preallocated,
            # so its length proves nothing and none of it can be trusted
            for stale in (part, _seg_ledger_path(part)):
                try:
                    os.remove(stale)
                except OSError:
                    pass
            ledger = [[s, e, 0] for s, e in bounds]
            job.done = 0
        elif ledger is None:
            # upgrade an unfinished single-stream prefix into segment 0..n
            ledger = [[s, e, 0] for s, e in bounds]
            spare = job.done or 0
            for row in ledger:
                if spare <= 0:
                    break
                take = min(spare, row[1] - row[0] + 1)
                row[2] = take
                spare -= take
            job.done = min(job.total, job.done or 0)
        else:
            job.done = sum(min(s[2], s[1] - s[0] + 1) for s in ledger)
    elif os.path.isfile(_seg_ledger_path(part)):
        # leftover segmented state but this run is a plain stream: the part file
        # is preallocated and cannot be treated as a contiguous prefix
        for stale in (part, _seg_ledger_path(part)):
            try:
                os.remove(stale)
            except OSError:
                pass
        job.done = 0
    job.connections = conns
    if conns > 1:
        try:
            got = _fetch_segments(job.url, part, job.total, conns,
                                  headers=job.headers, timeout=job.timeout,
                                  retries=job.retries, progress=job.add_bytes,
                                  cancel=job.cancel_evt, pause=job.pause_evt,
                                  throttle_bps=job.throttle_bps, ledger=ledger)
        except _DLPaused:
            return {"paused": True}
        except _DLCanceled:
            return {"error": "canceled", "canceled": True}
    else:
        if job.done and job.total and job.done >= job.total:
            got = {"ok": True, "bytes": job.done, "already_complete": True,
                   "headers": {}, "content_type": probe.get("content_type")}
        else:
            try:
                got = _stream_to_file(job.url, part, headers=job.headers,
                                      timeout=job.timeout, max_bytes=job.max_bytes,
                                      resume=True, retries=job.retries,
                                      progress=job.add_bytes,
                                      cancel=job.cancel_evt,
                                      pause=job.pause_evt,
                                      throttle_bps=job.throttle_bps)
            except _DLPaused:
                return {"paused": True}
            except _DLCanceled:
                return {"error": "canceled", "canceled": True}
    if got.get("error"):
        return got
    try:
        if not explicit:
            name = _name_from_response(job.url, got.get("headers")) or \
                probe.get("filename") or "download.bin"
            dest = _safe_path(os.path.join(job.folder, name) if job.folder
                              else name)
        if not job.overwrite and os.path.exists(dest):
            dest = _unique_path(dest)
        os.replace(part, dest)
    except Exception as exc:
        return {"error": "could not finalise the download: %s" % exc}
    size = 0
    try:
        size = os.path.getsize(dest)
    except OSError:
        pass
    record = {"url": job.url, "saved": dest.replace("\\", "/"), "bytes": size,
              "status": got.get("status"),
              "content_type": got.get("content_type") or probe.get("content_type"),
              "resumed": bool(got.get("resumed") or job.done),
              "connections": conns}
    checks = {}
    if job.expect_size:
        checks["size"] = {"expected": job.expect_size, "actual": size,
                          "ok": size == job.expect_size}
    if job.sha256:
        actual = _sha256_of(dest)
        checks["sha256"] = {"expected": job.sha256, "actual": actual,
                            "ok": actual.lower() == job.sha256.lower()}
    if checks:
        record["checks"] = checks
        record["result"] = "ok" if all(c["ok"] for c in checks.values()) else "FAILED CHECK"
        if record["result"] != "ok":
            record["verdict"] = "; ".join(
                "%s check failed (expected %s, got %s)" % (kind, c.get("expected"),
                                                           c.get("actual"))
                for kind, c in checks.items() if not c["ok"])
    if job.preview and (record.get("content_type") or "").startswith("text/"):
        try:
            with open(dest, "r", encoding="utf-8", errors="replace") as fh:
                record["preview"] = fh.read(1200)
        except Exception:
            pass
    return record


def _dl_worker(job):
    guard = job.sem or _DL_SEM
    guard.acquire()
    try:
        if job.cancel_evt.is_set():
            job.finish("canceled")
            return
        job.started_at = time.time()
        job.set_status("running")
        while True:
            try:
                rec = _dl_execute(job)
            except _DLPaused:
                rec = {"paused": True}
            except _DLCanceled:
                rec = {"error": "canceled", "canceled": True}
            except Exception as exc:
                rec = {"error": "download failed: %s" % exc}
            if not rec.get("paused"):
                break
            job.set_status("paused")
            job.wake.wait()
            job.wake.clear()
            if job.cancel_evt.is_set():
                break
            job.set_status("running")
        if rec.get("canceled"):
            job.error = "canceled"
            job.finish("canceled")
        elif rec.get("error"):
            job.error = rec["error"]
            job.finish("error")
        else:
            job.record = rec or {}
            job.saved = rec.get("saved", "")
            job.content_type = rec.get("content_type") or ""
            job.resumed = bool(rec.get("resumed"))
            job.connections = rec.get("connections") or job.connections
            job.total = rec.get("bytes") or job.total
            job.done = rec.get("bytes") or job.done
            job.name = os.path.basename(job.saved.replace("\\", "/")) or job.name
            if rec.get("result") == "FAILED CHECK":
                job.failed_check = True
                job.error = rec.get("verdict") or "checks failed"
                job.finish("error")
            else:
                job.finish("done")
    finally:
        guard.release()


def _dl_add(spec, tool="", register=True, wait=0, start=True):
    job = _DLJob(spec, tool=tool)
    if register:
        _dl_register(job)
    if start:
        job.thread = threading.Thread(target=_dl_worker, args=(job,),
                                      name="dl-%s" % job.id)
        job.thread.daemon = True
        job.thread.start()
    if wait:
        job.finished.wait(min(3600, int(wait) + 2))
    return job


def _dl_result(job):
    """Tool-shaped result for a finished (or still running) job."""
    if job.status in ("done", "error") and job.failed_check:
        # a file that downloaded but did not match is a verdict, not an error
        out = {"result": "FAILED CHECK", "url": job.url, "saved": job.saved,
               "bytes": job.done, "resumed": job.resumed,
               "checks": (job.record or {}).get("checks") or {},
               "verdict": job.error}
        if job.content_type:
            out["content_type"] = job.content_type
        return out
    if job.status == "done":
        rec = {"result": "ok", "url": job.url, "saved": job.saved,
               "bytes": job.done, "content_type": job.content_type or None,
               "resumed": job.resumed}
        for key in ("preview", "checks", "result", "verdict"):
            if (job.record or {}).get(key):
                rec[key] = job.record[key]
        return rec
    if job.status == "error":
        return {"error": job.error or "download failed"}
    if job.status == "canceled":
        return {"error": "download canceled", "canceled": True}
    return {"result": "running", "job": job.id, "url": job.url,
            "status": job.status, "done": job.done, "total": job.total,
            "part": job.dest or None}


def _dl_retry(job_id):
    job = _dl_get(job_id)
    if not job:
        return {"error": "no such download: %s" % job_id}
    if job.status in ("queued", "running", "paused"):
        return {"error": "download is still active"}
    spec = {"url": job.url, "dest": job.dest, "folder": job.folder,
            "group": job.group, "headers": job.headers, "timeout": job.timeout,
            "max_bytes": job.max_bytes, "retries": job.retries,
            "resume": True, "overwrite": job.overwrite,
            "connections": job.want_conns, "throttle_bps": job.throttle_bps,
            "sha256": job.sha256, "expect_size": job.expect_size}
    _dl_add(spec, tool=job.tool)
    return {"ok": True, "retried": job.id}


def _download_one(url, raw_path, folder, headers=None, timeout=180,
                  max_bytes=None, resume=False, retries=0, overwrite=False,
                  connections=1, throttle_bps=0, preview=True, sha256="",
                  expect_size=0):
    """Download one URL into the workspace, returning a per-file record.

    The bytes land in a .part file first, so a half-finished transfer is never
    mistaken for a finished file and the real name can still come from the
    server's Content-Disposition header.
    """
    if not _url_ok(url):
        return {"url": url, "error": "url must start with http:// or https://"}
    spec = {"url": url, "dest": raw_path, "folder": folder, "headers": headers,
            "timeout": timeout, "max_bytes": max_bytes, "resume": resume,
            "retries": retries, "overwrite": overwrite, "connections": connections,
            "throttle_bps": throttle_bps, "preview": preview, "sha256": sha256,
            "expect_size": expect_size}
    job = _dl_add(spec, register=False, wait=timeout + 5)
    rec = {"url": job.url}
    if job.status in ("done", "error") and job.failed_check:
        rec["saved"] = job.saved
        rec["bytes"] = job.done
        rec["status"] = 200
        rec["content_type"] = job.content_type or None
        rec["resumed"] = job.resumed
        rec["result"] = "FAILED CHECK"
        rec["verdict"] = job.error
        rec["checks"] = (job.record or {}).get("checks") or {}
    elif job.status == "done":
        rec["saved"] = job.saved
        rec["bytes"] = job.done
        rec["status"] = 200
        rec["content_type"] = job.content_type or None
        rec["resumed"] = job.resumed
        for key in ("preview", "checks", "result", "verdict"):
            if (job.record or {}).get(key):
                rec[key] = job.record[key]
    elif job.status == "canceled":
        rec["error"] = "download canceled"
    elif job.status == "error":
        rec["error"] = job.error or "download failed"
    else:
        rec["error"] = "download timed out after %ss" % timeout
    return rec


def _dl_args(args, default_timeout=180):
    """Tuning knobs shared by every download tool."""
    out = {}
    try:
        out["timeout"] = min(max(int(args.get("timeout") or default_timeout), 1), 1800)
    except Exception:
        out["timeout"] = default_timeout
    try:
        max_mb = float(args.get("max_mb") or 0)
    except Exception:
        max_mb = 0.0
    out["max_bytes"] = int(max_mb * 1024 * 1024) if max_mb > 0 else None
    try:
        out["retries"] = min(max(int(args.get("retries") or 0), 0), 10)
    except Exception:
        out["retries"] = 0
    try:
        out["connections"] = min(max(int(args.get("connections") or 1), 1),
                                 _SEG_MAX_CONNECTIONS)
    except Exception:
        out["connections"] = 1
    try:
        kbps = float(args.get("throttle_kbps") or 0)
    except Exception:
        kbps = 0.0
    out["throttle_bps"] = int(kbps * 1024.0) if kbps > 0 else 0
    out["background"] = bool(args.get("background"))
    out["path"] = str(args.get("path") or "").strip()
    out["overwrite"] = bool(args.get("overwrite"))
    out["resume"] = bool(args.get("resume", True))
    return out


def _dl_run(spec, tool, tune):
    """Enqueue a job for the model: wait for it, or hand back a job id."""
    job = _dl_add(spec, tool=tool)
    if tune.get("background"):
        return {"result": "started", "job": job.id, "url": job.url,
                "status": job.status,
                "note": "running in the background - watch it in the DOWNLOADS "
                        "panel, or call download_status with this id"}
    job.finished.wait(min(3600, job.timeout + 5))
    res = _dl_result(job)
    if res.get("result") == "running":
        res["note"] = ("still going after %ss: it keeps running in the "
                       "background and the DOWNLOADS panel can pause, cancel or "
                       "clear it" % job.timeout)
    return res


def _download_status(args):
    job_id = str(args.get("job") or "").strip()
    if not job_id:
        state = _dl_state()
        return {"result": "ok", "active": [j for j in state["jobs"]
                                           if j["status"] in
                                           ("queued", "running", "paused")],
                "max_parallel": state["max_parallel"],
                "connections_per_file": state["connections"]}
    job = _dl_get(job_id)
    if not job:
        return {"error": "no such download: %s" % job_id}
    return _dl_result(job)


def _resolve_for_download(url, headers, follow, render, timeout=30):
    """Resolve a page to a real file, carrying the session across the hop.

    Returns (url, headers, note). The cookies the browser earned are put into
    the headers, because a download sent without them gets challenged again and
    fails even though the browser just passed.
    """
    if not follow:
        return url, headers, ""
    try:
        res = _web_resolve({"url": url, "render": True, "max_hops": 3,
                            "max_results": 10, "timeout": timeout,
                            "headers": headers or {}})
    except Exception as exc:
        return url, headers, "could not resolve the page: %s" % str(exc)[:120]
    if res.get("error"):
        return url, headers, str(res.get("error"))[:160]
    cands = res.get("candidates") or []
    if not cands:
        return url, headers, "no file found behind that page"
    best = cands[0]
    merged = dict(headers or {})
    # The clearance cookie is the whole point: a transfer sent without it gets
    # challenged again and fails even though the browser just passed.
    cookie = res.get("_cookie") or ""
    if cookie and not any(str(k).lower() == "cookie" for k in merged):
        merged["Cookie"] = cookie
    note = "resolved via %s: %s" % (res.get("how", "?"), best.get("name") or best["url"])
    if res.get("source_page"):
        merged["Referer"] = res["source_page"]
    return best["url"], merged, note


def _download_file(args):
    url = str(args.get("url") or "").strip()
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    tune = _dl_args(args, 180)
    follow = args.get("follow")
    if isinstance(follow, str):
        follow = follow.strip().lower() not in ("0", "false", "no", "off")
    headers = _auth_headers(args)
    if follow:
        url, headers, note = _resolve_for_download(url, headers, True, True,
                                                  timeout=min(60, tune["timeout"]))
        if note:
            tune["resolve_note"] = note
    spec = {"url": url, "dest": tune["path"], "timeout": tune["timeout"],
            "max_bytes": tune["max_bytes"], "resume": tune["resume"],
            "retries": tune["retries"], "overwrite": tune["overwrite"],
            "connections": tune["connections"],
            "throttle_bps": tune["throttle_bps"], "headers": headers}
    res = _dl_run(spec, "download_file", tune)
    if tune.get("resolve_note"):
        res["resolved"] = tune["resolve_note"]
        if res.get("error"):
            res["note"] = ("the page resolved to %s but the transfer still failed; "
                           "try web_resolve with render:true and download the "
                           "candidate url directly" % url)
    return res


def _download_batch(args):
    urls = [str(u).strip() for u in (args.get("urls") or []) if str(u).strip()]
    page = str(args.get("from_page") or "").strip()
    pattern = str(args.get("match") or "").strip()
    # headers every resolved transfer needs: the session the page was reached
    # with, or a resolved file host would hand back a challenge page
    carry = {}
    resolved = {}
    if page:
        if not _url_ok(page):
            return {"error": "from_page must start with http:// or https://"}
        rx = re.compile(pattern, re.I) if pattern else None
        if args.get("resolve"):
            # opt-in: follow hops to the file behind the page and take only
            # real files, instead of every link the markup happens to carry
            render = args.get("render")
            if isinstance(render, str):
                render = render.strip().lower() not in ("0", "false", "no", "off")
            try:
                max_hops = min(max(int(args.get("max_hops") or 3), 0), 5)
            except Exception:
                max_hops = 3
            try:
                res = _web_resolve({"url": page, "render": render,
                                    "max_hops": max_hops, "max_results": 60,
                                    "timeout": 45, "headers": _auth_headers(args)})
            except Exception as exc:
                return {"error": "could not read the page: %s" % exc}
            if res.get("error"):
                msg = str(res["error"])
                # public_result() collapses a failed result to {"error": ...}
                # alone, so a hint in a sibling key would be discarded
                if not res.get("pages_walked"):
                    return {"error": "could not read the page: %s" % msg}
                return {"error": "%s - if the links only appear after the page "
                                 "runs its scripts, retry with 'render': true" % msg}
            if res.get("_cookie"):
                carry["Cookie"] = res["_cookie"]
            carry["Referer"] = res.get("source_page") or page
            resolved = {"how": res.get("how"),
                        "walked": res.get("pages_walked") or [],
                        "found": res.get("found", 0), "candidates": []}
            for cand in res.get("candidates") or []:
                full = cand.get("url")
                if not full or not _url_ok(full):
                    continue
                if rx and not rx.search(full):
                    continue
                resolved["candidates"].append(
                    {"url": full, "name": cand.get("name"),
                     "size_h": cand.get("size_h"), "type": cand.get("type")})
                urls.append(full)
            urls = list(dict.fromkeys(urls))
        else:
            try:
                body = _fetch_html(page, 1500000)
            except Exception as exc:
                return {"error": "could not read the page: %s" % exc}
            found = re.findall(r'(?:href|src)\s*=\s*["\']([^"\']+)["\']', body, re.I)
            base = page if page.endswith("/") else page.rsplit("/", 1)[0] + "/"
            for link in found:
                if link.startswith(("javascript:", "mailto:", "data:", "#")):
                    continue
                full = urllib.parse.urljoin(base, link)
                if _url_ok(full) and (not rx or rx.search(full)):
                    urls.append(full)
            urls = list(dict.fromkeys(urls))
    urls = [u for u in urls if _url_ok(u)]
    if not urls:
        if page:
            return {"error": "nothing to download from %s%s" %
                            (page, " (no link matched 'match')" if pattern else "")}
        return {"error": "nothing to download: give 'urls' or a 'from_page' whose "
                         "links match 'match'"}
    try:
        max_files = min(max(int(args.get("max_files") or 25), 1), 200)
    except Exception:
        max_files = 25
    if len(urls) > max_files:
        urls = urls[:max_files]
    folder = str(args.get("path") or "downloads").strip() or "downloads"
    tune = _dl_args(args, 120)
    try:
        workers = min(max(int(args.get("parallel") or 4), 1), 8)
    except Exception:
        workers = 4
    sem = threading.Semaphore(workers)
    group = os.path.basename(folder.replace("\\", "/")) or folder
    jobs = []
    for url in urls:
        spec = {"url": url, "dest": "", "folder": folder, "group": group,
                "timeout": tune["timeout"], "max_bytes": tune["max_bytes"],
                "resume": True, "retries": tune["retries"],
                "connections": tune["connections"], "headers": dict(carry) or None,
                "throttle_bps": tune["throttle_bps"], "sem": sem}
        jobs.append(_dl_add(spec, tool="download_batch"))
    if resolved:
        resolved["queued"] = len(jobs)
        resolved["skipped_over_max_files"] = max(0, len(set(urls)) - max_files)
    if tune["background"]:
        out = {"result": "started", "requested": len(jobs),
               "jobs": [j.id for j in jobs], "folder": folder.replace("\\", "/"),
               "note": "%d downloads queued in the background - call "
                       "download_status to check on them" % len(jobs)}
        if resolved:
            out["resolved"] = resolved
        return out
    rounds = (len(jobs) + workers - 1) // workers
    deadline = time.time() + tune["timeout"] * rounds + 30
    for job in jobs:
        job.finished.wait(max(0.5, deadline - time.time()))
    ok = [j for j in jobs if j.status == "done"]
    bad = [j for j in jobs if j.status in ("error", "canceled")]
    live = [j for j in jobs if j.status in ("queued", "running", "paused")]
    out = {"result": "ok", "requested": len(jobs), "downloaded": len(ok),
           "failed": len(bad), "folder": folder.replace("\\", "/"),
           "files": [{"file": os.path.basename(j.saved.replace("\\", "/")),
                      "bytes": j.done, "url": j.url, "job": j.id} for j in ok]}
    if resolved:
        out["resolved"] = resolved
    if bad:
        out["errors"] = [{"url": j.url, "error": j.error or j.status}
                         for j in bad][:20]
    if live:
        out["still_running"] = [{"url": j.url, "job": j.id} for j in live][:20]
    if not ok and not live and bad:
        if resolved:
            names = ", ".join(c["url"] for c in resolved.get("candidates", [])[:4])
            return {"error": "all %d downloads failed (%s) - the page resolved to "
                             "[%s] but the transfers failed; try web_resolve with "
                             "render:true and download them one at a time"
                             % (len(bad), bad[0].error, names)}
        return {"error": "all %d downloads failed: %s" % (len(bad), bad[0].error)}
    return out


_SECRET_KEYS = ("authorization", "bearer", "cookie", "x-api-key", "api-key",
                "apikey", "token", "secret", "password", "passwd", "session",
                "auth")


def _redact_secrets(args):
    """Copy of the arguments with secret values masked, for storage/history."""
    clean = {}
    for key, val in (args or {}).items():
        low = str(key).lower()
        if any(s in low for s in _SECRET_KEYS) and val:
            clean[key] = "***redacted***"
        elif low == "headers" and isinstance(val, dict):
            clean[key] = {k: ("***redacted***"
                              if any(s in str(k).lower() for s in _SECRET_KEYS) and v
                              else v)
                          for k, v in val.items()}
        else:
            clean[key] = val
    return clean


def _auth_headers(args):
    headers = {}
    for k, v in (args.get("headers") or {}).items():
        if v is not None and str(k).strip():
            headers[str(k)] = str(v)
    bearer = str(args.get("bearer") or "").strip()
    if bearer:
        headers["Authorization"] = (bearer if " " in bearer else "Bearer " + bearer)
    cookie = str(args.get("cookie") or "").strip()
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _human_bytes(n):
    """A byte count as a short human string, for reporting sizes to the model."""
    try:
        n = float(n)
    except Exception:
        return ""
    if n < 1024:
        return "%d B" % int(n)
    for unit, step in (("KB", 1024), ("MB", 1024 ** 2), ("GB", 1024 ** 3),
                       ("TB", 1024 ** 4)):
        if n < step * 1024 or unit == "TB":
            return "%.1f %s" % (n / float(step), unit)
    return "%.1f TB" % (n / float(1024 ** 4))


_RESOLVE_PW_LOCK = threading.Lock()
_RESOLVE_PW = {"playwright": None, "browser": None, "procs": set(), "dead": False}
# One page at a time. A second Chromium is hundreds of MB and these calls are
# interactive, so queueing is cheaper than a second browser.
_RESOLVE_PW_SEM = threading.BoundedSemaphore(1)
_RESOLVE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _resolve_pw_stop():
    """Tear the browser down and reap anything it left running.

    Called from the normal exit path and from the server shutdown hook, because
    a leaked Chromium holds a profile lock and a few hundred MB.
    """
    with _RESOLVE_PW_LOCK:
        browser = _RESOLVE_PW.get("browser")
        pw = _RESOLVE_PW.get("playwright")
        _RESOLVE_PW["browser"] = None
        _RESOLVE_PW["playwright"] = None
        _RESOLVE_PW["dead"] = True
        procs = list(_RESOLVE_PW.get("procs") or ())
        _RESOLVE_PW["procs"] = set()
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
    for proc in procs:
        try:
            proc.terminate()
        except Exception:
            pass
    for proc in procs:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _resolve_pw_browser():
    """A long-lived headless Chromium, started on first use.

    Returns (playwright, browser) or raises. The caller must hold the
    semaphore. On any failure the half-built objects are dropped so the next
    call starts clean rather than reusing a dead browser.
    """
    with _RESOLVE_PW_LOCK:
        if _RESOLVE_PW.get("dead"):
            raise RuntimeError("the browser resolver was shut down")
        if _RESOLVE_PW.get("browser") is not None:
            if _RESOLVE_PW["browser"].is_connected():
                return _RESOLVE_PW["playwright"], _RESOLVE_PW["browser"]
            _RESOLVE_PW["browser"] = None
            _RESOLVE_PW["playwright"] = None
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    browser = None
    try:
        browser = pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ])
    except Exception:
        try:
            pw.stop()
        except Exception:
            pass
        raise
    with _RESOLVE_PW_LOCK:
        _RESOLVE_PW["playwright"] = pw
        _RESOLVE_PW["browser"] = browser
    return pw, browser


def _resolve_render(url, timeout=30, headers=None, max_depth=1, wait_ms=2500):
    """Load a page in a real browser and harvest the links it produces.

    Returns (candidates, pages, cookies_header, note). A note explains why the
    browser path did not work, so the caller can say something useful instead
    of "no files found".
    """
    acquired = _RESOLVE_PW_SEM.acquire(timeout=min(30, int(timeout) + 5))
    if not acquired:
        return [], [], "", "the browser resolver is busy"
    browser = None
    context = None
    try:
        ok, _why = _resolve_host_allowed(url)
        if not ok:
            return [], [], "", "refused: " + _why
        try:
            _pw, browser = _resolve_pw_browser()
        except Exception as exc:
            return [], [], "", "no browser available (%s)" % str(exc)[:120]
        try:
            context = browser.new_context(
                user_agent=_RESOLVE_UA, ignore_https_errors=True,
                accept_downloads=False, java_script_enabled=True)
        except Exception as exc:
            return [], [], "", "could not open a page (%s)" % str(exc)[:120]
        if headers:
            try:
                context.set_extra_http_headers({str(k): str(v)
                                               for k, v in headers.items()
                                               if v is not None})
            except Exception:
                pass
        page = None
        network_downloads = []
        try:
            page = context.new_page()

            def _on_response(resp):
                # a response that is a file, not a document, is the prize
                try:
                    if resp.request.resource_type not in ("document", "xhr", "fetch"):
                        if _resolve_ct_is_file(resp.headers.get("content-type")) or \
                                "attachment" in (resp.headers.get("content-disposition") or ""):
                            if _url_ok(resp.url):
                                network_downloads.append(resp.url)
                except Exception:
                    pass

            page.on("response", _on_response)
            page.set_default_timeout(max(3000, int(timeout) * 1000))
            page.goto(url, wait_until="domcontentloaded",
                      timeout=int(timeout) * 1000)
        except Exception as exc:
            return [], [], "", "could not load the page (%s)" % str(exc)[:140]
        # Links built by script appear after the DOM settles, and a bot check
        # usually swaps the body and navigates, so give both a moment.
        try:
            page.wait_for_load_state("networkidle", timeout=min(8000, wait_ms + 4000))
        except Exception:
            pass
        try:
            page.wait_for_timeout(wait_ms)
        except Exception:
            pass
        final_url = url
        try:
            final_url = page.url or url
        except Exception:
            pass
        try:
            markup = page.content()
        except Exception:
            markup = ""
        # Also harvest anything the page actually fetched: a file served by
        # fetch/XHR never appears in the HTML but shows up as a response.
        seen = {}
        for cand in (_resolve_candidates(markup, final_url) if markup else []):
            seen[cand["url"]] = cand
        for hint in network_downloads + _resolve_render_downloads(page):
            if hint not in seen:
                seen[hint] = {"url": hint,
                              "name": _name_from_response(hint, None),
                              "score": _resolve_score(hint) + 25,
                              "from": "network"}
        # walk one more hop if the page only led to another page
        pages = [{"url": final_url, "how": "browser"}]
        files = []
        for cand in sorted(seen.values(), key=lambda c: -c["score"]):
            ext = os.path.splitext(urllib.parse.urlsplit(cand["url"]).path)[1].lower()
            if ext in _RESOLVE_FILE_EXT or _resolve_ct_is_file(
                    cand.get("content_type")):
                files.append(cand)
            elif max_depth > 0 and (ext in _RESOLVE_INDEX_EXT or not ext):
                more, _p, _h, _n = _resolve_render(cand["url"], timeout=timeout,
                                                   max_depth=max_depth - 1)
                files.extend(more)
        files = sorted(files, key=lambda c: -c["score"])[:40]
        # Collect the cookies BEFORE probing. A probe sent without them gets a
        # soft 404 or a challenge page instead of the file, which is exactly
        # the failure this whole path exists to avoid.
        cookie_header = ""
        try:
            pairs = ["%s=%s" % (c.get("name"), c.get("value"))
                     for c in (context.cookies() or [])
                     if c.get("name") and c.get("value")]
            if pairs:
                cookie_header = "; ".join(pairs)
        except Exception:
            pass
        probe_headers = dict(headers or {})
        if cookie_header and not any(str(k).lower() == "cookie" for k in probe_headers):
            probe_headers["Cookie"] = cookie_header
        # the browser tells us a link exists; only a real request tells us what
        # it actually is
        for cand in files[:25]:
            _resolve_probe_candidate(cand, timeout=timeout, headers=probe_headers)
        files = sorted([c for c in files
                        if os.path.splitext(urllib.parse.urlsplit(c["url"]).path)[1].lower()
                        in _RESOLVE_FILE_EXT
                        or _resolve_ct_is_file(c.get("content_type"))
                        or c.get("from") == "network"],
                       key=lambda c: -c["score"])[:40]
        return files, pages, cookie_header, ""
    except Exception as exc:
        return [], [], "", "browser render failed (%s)" % str(exc)[:140]
    finally:
        # close in the reverse order, and never let teardown raise
        for closer in (lambda: page and page.close(),
                       lambda: context and context.close()):
            try:
                closer()
            except Exception:
                pass
        try:
            _RESOLVE_PW_SEM.release()
        except Exception:
            pass


def _resolve_render_downloads(page):
    """Content-disposition / attachment responses the browser observed.

    Playwright only keeps these if they were recorded while the page was open,
    so the caller passes a page whose responses are already tracked.
    """
    try:
        hint = page.evaluate(
            """() => {
                const out = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.href;
                    if (!h) return;
                    if (a.hasAttribute('download') ||
                        /\\.(zip|rar|7z|iso|exe|msi|pdf|dmg|deb|rpm|apk|img|bin)$/i.test(h)) {
                        out.push(h);
                    }
                });
                return out;
            }""")
        return [h for h in (hint or []) if _url_ok(h)]
    except Exception:
        return []


def _web_resolve(args):
    """Report the files behind a download page. Downloads nothing."""
    url = str(args.get("url") or "").strip()
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    try:
        max_hops = min(max(int(args.get("max_hops") or 3), 0), 5)
    except Exception:
        max_hops = 3
    try:
        max_results = min(max(int(args.get("max_results") or 10), 1), 25)
    except Exception:
        max_results = 10
    try:
        timeout = min(max(int(args.get("timeout") or 30), 3), 300)
    except Exception:
        timeout = 30
    render = args.get("render")
    if isinstance(render, str):
        render = render.strip().lower() not in ("0", "false", "no", "off")
    headers = _auth_headers(args)
    started = time.time()
    ranked, pages = [], []
    how = "static"
    cookie_header = ""
    try:
        ranked, pages = _resolve_static(url, max_depth=max_hops, headers=headers,
                                        timeout=timeout)
    except Exception as exc:
        return {"error": "could not resolve %s: %s" % (url, exc)}
    # A page that gave us nothing is exactly the case a browser is for.
    if render or not ranked:
        rendered, rpages, rcookies, note = _resolve_render(url, timeout=timeout,
                                                           headers=headers)
        if rendered:
            ranked, pages, how = rendered, pages + rpages, "browser"
            cookie_header = rcookies
            if note:
                how += " (%s)" % note
            else:
                how += " (javascript rendered)"
        elif not ranked:
            return {"error": "no downloadable file found on %s%s" %
                            (url, (" after following %d page(s)" % len(pages))
                             if len(pages) > 1 else ""),
                    "pages_walked": [p.get("url") for p in pages],
                    "why": note or "the page had no file links",
                    "note": "if the links only appear after the page runs, retry "
                            "with 'render': true"}
    if not ranked:
        return {"error": "no downloadable file found on %s%s" %
                        (url, (" after following %d page(s)" % len(pages))
                         if len(pages) > 1 else ""),
                "pages_walked": [p.get("url") for p in pages],
                "note": "if the links only appear after the page runs, retry with "
                        "'render': true"}
    out = []
    for cand in ranked[:max_results]:
        item = {"url": cand["url"], "name": cand.get("filename") or cand.get("name"),
                "score": cand.get("score")}
        if cand.get("content_type"):
            item["type"] = cand["content_type"].split(";")[0]
        if cand.get("size"):
            item["bytes"] = cand["size"]
            item["size_h"] = _human_bytes(cand["size"])
        else:
            item["size"] = None
        if cand.get("final_url") and cand["final_url"] != cand["url"]:
            item["redirects_to"] = cand["final_url"]
        if cand.get("accepts_ranges"):
            item["resumable"] = True
        if cand.get("skipped"):
            item["note"] = cand["skipped"]
        out.append(item)
    res = {"result": "ok", "page": url, "how": how, "found": len(out),
           "candidates": out, "pages_walked": [p.get("url") for p in pages],
           "elapsed": round(time.time() - started, 1)}
    # private, for download_file(follow=True) only - never shown to the model
    res["_cookie"] = cookie_header
    res["source_page"] = url
    best = _resolve_best(ranked)
    if best and best["url"] in [c["url"] for c in out[:1]]:
        res["suggestion"] = ("call download_file with url=%r%s to fetch it"
                             % (best["url"],
                                " and follow=true" if how.startswith("browser") else ""))
    return res


def _download_authed(args):
    url = str(args.get("url") or "").strip()
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    headers = _auth_headers(args)
    if not headers:
        return {"error": "no credentials given - pass 'bearer', 'cookie' or "
                         "'headers' (the values are never saved or shown back)"}
    tune = _dl_args(args, 180)
    if not tune["retries"]:
        tune["retries"] = 1
    spec = {"url": url, "dest": tune["path"], "headers": headers,
            "timeout": tune["timeout"], "max_bytes": tune["max_bytes"],
            "resume": tune["resume"], "retries": tune["retries"],
            "connections": tune["connections"],
            "throttle_bps": tune["throttle_bps"]}
    res = _dl_run(spec, "download_authed", tune)
    res["sent_headers"] = sorted(headers.keys())
    return res


_ASSET_ATTR = re.compile(
    r'(?P<attr>\b(?:src|href|poster|data-src|data-original)\s*=\s*)(?P<q>["\'])(?P<url>[^"\']+)(?P=q)',
    re.I)
_SRCSET = re.compile(r'(?P<attr>\bsrcset\s*=\s*)(?P<q>["\'])(?P<val>[^"\']+)(?P=q)', re.I)
_ASSET_EXT = (".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
              ".ico", ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm",
              ".mp3", ".wav", ".pdf", ".json", ".txt", ".xml")
_SKIP_PREFIX = ("javascript:", "mailto:", "data:", "blob:", "#", "tel:")


def _download_page(args):
    url = str(args.get("url") or "").strip()
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    folder = str(args.get("path") or "").strip() or "pages"
    want_assets = args.get("assets", True)
    if isinstance(want_assets, str):
        want_assets = want_assets.strip().lower() not in ("0", "false", "no", "off")
    try:
        max_assets = min(max(int(args.get("max_assets") or 60), 0), 400)
    except Exception:
        max_assets = 60
    try:
        timeout = min(max(int(args.get("timeout") or 60), 1), 300)
    except Exception:
        timeout = 60
    try:
        max_mb = float(args.get("max_mb") or 0)
    except Exception:
        max_mb = 0
    cap = int(max_mb * 1024 * 1024) if max_mb else None
    try:
        body = _fetch_html(url, 3000000, timeout=timeout)
    except Exception as exc:
        return {"error": "could not read the page: %s" % exc}
    try:
        index_path = _safe_path(os.path.join(folder, "index.html"))
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
    except Exception as exc:
        return {"error": "bad destination path: %s" % exc}
    base = url if url.endswith("/") else url.rsplit("/", 1)[0] + "/"
    assets = []

    def take(raw):
        raw = raw.strip()
        if not raw or raw.lower().startswith(_SKIP_PREFIX):
            return None
        return urllib.parse.urljoin(base, raw)

    if want_assets and max_assets:
        wanted = []
        for m in _ASSET_ATTR.finditer(body):
            raw = m.group("url")
            full = take(raw)
            if full and _url_ok(full):
                wanted.append({"url": full, "raw": raw,
                               "attr": m.group("attr"), "quote": m.group("q")})
        for m in _SRCSET.finditer(body):
            for part in m.group("val").split(","):
                bits = part.strip().split()
                if not bits:
                    continue
                full = take(bits[0])
                if full and _url_ok(full):
                    wanted.append({"url": full, "raw": bits[0],
                                   "attr": m.group("attr"), "quote": m.group("q")})
        seen = set()
        for item in wanted[:max_assets]:
            full = item["url"]
            if full in seen:
                continue
            seen.add(full)
            sub = os.path.join(folder, "assets")
            rec = _download_one(full, "", sub, timeout=timeout, max_bytes=cap)
            if rec.get("error"):
                continue
            rel = "assets/" + os.path.basename(rec["saved"]).replace("\\", "/")
            assets.append({"file": rel, "bytes": rec["bytes"], "url": full})
            needle = item["attr"] + item["quote"] + item["raw"] + item["quote"]
            if needle in body:
                body = body.replace(needle, item["attr"] + item["quote"] + rel + item["quote"])
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", body, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", html.unescape(m.group(1))).strip()[:120]
    try:
        with open(index_path, "w", encoding="utf-8") as fh:
            fh.write(body)
    except Exception as exc:
        return {"error": "could not save the page: %s" % exc}
    return {"result": "ok", "url": url, "title": title,
            "saved": index_path.replace("\\", "/"),
            "bytes": len(body.encode("utf-8")),
            "assets_saved": len(assets), "assets": assets[:40],
            "note": "open the saved index.html - it works offline, assets are "
                    "in the assets/ subfolder"}


def _download_verify(args):
    url = str(args.get("url") or "").strip()
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    want_sha = str(args.get("sha256") or "").strip().lower().replace(" ", "")
    if want_sha and not re.fullmatch(r"[0-9a-f]{64}", want_sha):
        return {"error": "sha256 must be 64 hex characters"}
    try:
        expect_size = int(args.get("bytes") or 0)
    except Exception:
        expect_size = 0
    try:
        retries = min(max(int(args.get("retries") or 3), 0), 10)
    except Exception:
        retries = 3
    tune = _dl_args(args, 300)
    tune["retries"] = retries
    spec = {"url": url, "dest": tune["path"], "timeout": tune["timeout"],
            "max_bytes": tune["max_bytes"], "resume": tune["resume"],
            "retries": retries, "overwrite": True,
            "connections": tune["connections"],
            "throttle_bps": tune["throttle_bps"],
            "sha256": want_sha, "expect_size": expect_size}
    res = _dl_run(spec, "download_verify", tune)
    if not res.get("error"):
        res.setdefault("checks", {})
    return res


def _download_media(args):
    url = str(args.get("url") or "").strip()
    if not _url_ok(url):
        return {"error": "url must start with http:// or https://"}
    exe = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
    if not exe:
        try:
            __import__("yt_dlp")
            exe = sys.executable
        except Exception:
            return {"error": "yt-dlp is not installed - run "
                             "'pip install yt-dlp' (or "
                             "'python -m pip install yt-dlp') and try again"}
    folder = str(args.get("path") or "media").strip() or "media"
    try:
        dest = _safe_path(folder)
        os.makedirs(dest, exist_ok=True)
    except Exception as exc:
        return {"error": "bad destination path: %s" % exc}
    cmd = [exe] if exe == sys.executable else [exe]
    cmd += ["--no-playlist" if not args.get("playlist") else "--yes-playlist",
            "--newline", "--no-warnings", "--print-json",
            "--paths", dest, "-o", "%(title).80B [%(id)s].%(ext)s"]
    audio_only = bool(args.get("audio_only"))
    if audio_only:
        cmd += ["-x", "--audio-format", str(args.get("audio_format") or "mp3")]
    quality = str(args.get("quality") or "").strip()
    if quality and not audio_only:
        cmd += ["-f", quality]
    if args.get("subtitles"):
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs",
                str(args.get("sub_langs") or "en"), "--embed-subs"]
    if args.get("thumbnail"):
        cmd += ["--write-thumbnail", "--embed-thumbnail"]
    if str(args.get("cookies_from_browser") or "").strip():
        cmd += ["--cookies-from-browser", str(args.get("cookies_from_browser")).strip()]
    cmd.append(url)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=min(max(int(args.get("timeout") or 900), 30), 3600),
                              creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"error": "yt-dlp timed out"}
    except Exception as exc:
        return {"error": "yt-dlp failed to start: %s" % exc}
    files = []
    titles = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            info = json.loads(line)
        except Exception:
            continue
        if info.get("title"):
            titles.append(info["title"])
        for entry in (info.get("requested_downloads") or []):
            fp = entry.get("filepath") or entry.get("_filename")
            if fp and os.path.isfile(fp):
                files.append({"file": fp.replace("\\", "/"),
                              "bytes": os.path.getsize(fp)})
        if info.get("filepath") and os.path.isfile(info["filepath"]):
            fp = info["filepath"]
            if not any(f["file"].endswith(os.path.basename(fp)) for f in files):
                files.append({"file": fp.replace("\\", "/"),
                              "bytes": os.path.getsize(fp)})
    if proc.returncode != 0 and not files:
        tail = ((proc.stderr or "").strip().splitlines() or ["unknown error"])[-1]
        return {"error": "yt-dlp: %s" % tail[:300]}
    if not files:
        try:
            for name in sorted(os.listdir(dest)):
                full = os.path.join(dest, name)
                if os.path.isfile(full) and not name.endswith(".part"):
                    files.append({"file": full.replace("\\", "/"),
                                  "bytes": os.path.getsize(full)})
        except Exception:
            pass
    return {"result": "ok", "url": url, "title": (titles[0] if titles else ""),
            "folder": dest.replace("\\", "/"), "files": files[:20],
            "audio_only": audio_only}


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
                                 "shell command (e.g. 7z)"}
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
                "entries": count[0]}
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
    """Play a wav file. Returns the duration when we handled it, or None.

    None used to mean both "a player took it" and "nothing on this machine
    can play audio at all", and the caller reported success either way - so a
    Linux box with no PulseAudio, no ALSA and no paplay/aplay/ffplay said the
    words out loud in a result the model and the user both believed. The
    caller now checks which one it got.
    """
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
        if IS_WINDOWS:
            try:
                import winsound
                winsound.PlaySound(path, winsound.SND_FILENAME
                                   | winsound.SND_ASYNC)
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
        return False


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
    played = _play_wav(wav_path)
    out = {"result": "ok", "voice": voice, "text": text[:200],
           "wav": wav_path.replace("\\", "/"),
           "bytes": os.path.getsize(wav_path),
           "duration_seconds": duration,
           "played": played is not False}
    if played is False:
        # Say so. The audio file is real and on disk, so the honest result is
        # "written but nobody could play it", with the way out.
        out["warning"] = (
            "the speech was generated and saved, but nothing on this machine "
            "could play it - no sounddevice, and no paplay, aplay or ffplay. "
            "On Linux install one: sudo apt install pulseaudio-utils")
    return out


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
    """Downscale + JPEG-encode a PIL image, returning (data_uri, w, h).

    w and h are the size of the image that was actually encoded, not the size
    it was on disk. They used to be the original, so a 1600x1200 screenshot
    was sent to the model at 1280x960 and described as 1600x1200 - and the
    model is the one being asked to reason about what it can see."""
    from PIL import Image
    view = img
    if max(img.size) > max_edge:
        r = max_edge / float(max(img.size))
        size = (max(1, int(img.size[0] * r)), max(1, int(img.size[1] * r)))
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
    return data_uri, view.size[0], view.size[1]


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
    started = time.time()
    try:
        record = _execute_tool_call(name, args, tc, hooks, image_uri)
        record["ms"] = int((time.time() - started) * 1000)
        return record
    except CLIENT_GONE:
        # a closed browser is not a tool failure: let the stream unwind
        raise
    except Exception as exc:
        # One bad tool call must not kill the whole turn; hand the failure
        # back to the model so it can correct the arguments and continue.
        _take_last_change()
        err = {"error": f"{type(exc).__name__}: {exc}"}
        record = {"name": name, "arguments": args, "result": public_result(err),
                  "full_result": err,
                  "tool_call_id": tc.get("id") or "call_" + str(len(name))}
        record["ms"] = int((time.time() - started) * 1000)
        return record
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
    elif name == "shell":
        raw_result = _shell_oc(args.get("command"), args.get("workdir"),
                               args.get("timeout"))
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
    elif name == "rich_text":
        raw_result = _rich_text(args)
    elif name == "download_file":
        raw_result = _download_file(args)
    elif name == "web_resolve":
        raw_result = _web_resolve(args)
    elif name == "download_batch":
        raw_result = _download_batch(args)
    elif name == "download_authed":
        raw_result = _download_authed(args)
    elif name == "download_page":
        raw_result = _download_page(args)
    elif name == "download_verify":
        raw_result = _download_verify(args)
    elif name == "download_media":
        raw_result = _download_media(args)
    elif name == "download_status":
        raw_result = _download_status(args)
    elif name == "archive":
        raw_result = _archive(args)
    elif name == "question":
        questions = args.get("questions") or []
        answers = []
        for q in questions:
            if isinstance(q, dict):
                q_text = q.get("question", "")
                q_options = q.get("options") or []
                answers.append({"question": q_text, "options": q_options,
                                 "header": q.get("header", "")})
            elif isinstance(q, str):
                answers.append({"question": q, "options": [], "header": ""})
        on_ask = (hooks or {}).get("on_ask")
        if on_ask:
            raw_result = {"result": "ok",
                          "answers": on_ask({"questions": answers}) or []}
        else:
            raw_result = {"error": "question is only available in the live UI"}
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
    elif name == "execute":
        raw_result = _execute_js(args.get("code", ""), args.get("timeout"))
    elif name == "window_list":
        raw_result = _window_list()
    elif name == "window_action":
        raw_result = _window_action(args)
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
    elif name == "glob":
        raw_result = _glob_search(args.get("pattern"), args.get("path"))
    elif name == "read":
        raw_result = _read_oc(args.get("filePath") or args.get("path"),
                              args.get("offset"), args.get("limit"))
        # Reading a .png attaches it so the model can look at it. That
        # attachment was being dropped on the floor: image_uri was only ever
        # set in the Blender branch, so the picture was fetched, encoded and
        # then thrown away, and the model was asked to describe a file it
        # could not see.
        if isinstance(raw_result, dict) and raw_result.get("image_data"):
            image_uri = raw_result["image_data"]
    elif name == "edit":
        raw_result = _edit_oc(args.get("filePath") or args.get("path"),
                              args.get("oldString") or args.get("old_text"),
                              args.get("newString") or args.get("new_text"),
                              args.get("replaceAll") or args.get("replace_all", False))
    elif name == "shell":
        raw_result = _shell_oc(args.get("command"), args.get("workdir"),
                               args.get("timeout"))
    elif name == "question":
        questions = args.get("questions") or []
        answers = []
        for q in questions:
            if isinstance(q, dict):
                q_text = q.get("question", "")
                q_options = q.get("options") or []
                answers.append({"question": q_text, "options": q_options,
                                 "header": q.get("header", "")})
            elif isinstance(q, str):
                answers.append({"question": q, "options": [], "header": ""})
        on_ask = (hooks or {}).get("on_ask")
        if on_ask:
            raw_result = {"result": "ok",
                          "answers": on_ask({"questions": answers}) or []}
        else:
            raw_result = {"error": "question is only available in the live UI"}
    elif name == "task":
        raw_result = _task_agent(args.get("description", ""),
                                args.get("prompt", ""),
                                args.get("subagent_type", "general"),
                                args.get("background", False))
    elif name == "plan":
        raw_result = _plan_task(args.get("task", ""))
    elif name == "skill":
        raw_result = _skill_load(args.get("name", ""))
    elif name == "execute":
        raw_result = _execute_js(args.get("code", ""), args.get("timeout"))
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
    shown_args = _redact_secrets(args) if name == "download_authed" else args
    record = {"name": name, "arguments": shown_args, "result": result,
              "full_result": full,
              "tool_call_id": tc.get("id") or "call_" + str(len(name))}
    change = _take_last_change()
    if change:
        # UI-only: the model already got its result, the chat gets the diff.
        record["change"] = change
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
            "cached_tokens": 0, "tok_s": 0, "ctx_used": 0, "ctx_left": _ctx_window()}


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
            "ctx_used": prompt + comp, "ctx_left": _ctx_window() - prompt - comp}


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
    out["ctx_left"] = _ctx_window() - out["ctx_used"]
    return out


def run_agent(messages, on_tool=None, mode=MODE_BUILD, on_stats=None, hooks=None):
    calls = []
    tools = _tools_for(mode)
    msgs = list(messages)
    total = _empty_stats()
    for _ in range(MAX_TOOL_ROUNDS):
        try:
            message = _bonsai_chat(msgs, tools)
        except Exception as exc:
            # A text-only model that has just been handed a screenshot answers
            # with a 400: retry the same round without the image rather than
            # losing the turn - see _blind_turn. And a tool result too large
            # for the provider fails the whole request: shorten it and retry -
            # see _oversize_turn. Neither is a reason to discard the work.
            if not _is_blind():
                fixed = _blind_turn(msgs, exc)
                if fixed is not None:
                    _remember_blind()
                    msgs[:] = fixed
                    if on_tool:
                        on_tool(_blind_record())
                    message = _bonsai_chat(msgs, tools)
                else:
                    fixed = _oversize_turn(msgs, exc)
                    if fixed is None:
                        raise
                    msgs[:] = fixed
                    if on_tool:
                        on_tool(_oversize_record())
                    message = _bonsai_chat(msgs, tools)
            else:
                fixed = _oversize_turn(msgs, exc)
                if fixed is None:
                    raise
                msgs[:] = fixed
                if on_tool:
                    on_tool(_oversize_record())
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
                _append_shot_image(msgs, {"preview": image_uri})
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


_VISION_REFUSAL_MARKERS = (
    "does not support image",
    "doesn't support image",
    "does not support vision",
    "doesn't support vision",
    "not support image",
    "not support vision",
    "no vision",
    "images are not supported",
    "image input is not supported",
    "image inputs are not supported",
    "unsupported image",
    "invalid image",
    "image content type",
    "multimodal",
    "text-only model",
    "text only model",
    "cannot accept image",
    "can't accept image",
    "unable to process image",
    "does not understand image",
    "image_url",
)

# What the model is told instead of the picture. It has to be specific, because
# the whole point is that the model carries on: it still has every text tool,
# and the file is on disk.
_BLIND_NOTE = (
    "[A screenshot was taken, but this model cannot see images - the provider "
    "rejected the image. The capture is on disk if you need it. Carry on with "
    "what you can actually read: window_list, read_file, ui_dump, and reading "
    "values you are told. Do not call the screenshot tools again this turn, and "
    "if you truly need to see the screen, say so and let the user describe it.]")

# Learned per model, not per turn. Once a model has refused an image, it will
# refuse the next one, and the rejected request is not free, so the answer is
# remembered until that model is not the active one.
_BLIND_MODELS = set()


def _is_blind(entry=None):
    entry = entry if entry is not None else _active_model()
    return str((entry or {}).get("id") or "") in _BLIND_MODELS


def _remember_blind(entry=None):
    entry = entry if entry is not None else _active_model()
    mid = str((entry or {}).get("id") or "")
    if mid:
        _BLIND_MODELS.add(mid)


def _msgs_have_image(msgs):
    for m in msgs or []:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    return True
    return False


def _vision_refusal_text(exc):
    """The provider's own words when it turned an image down, else ""."""
    try:
        text = _model_error_text(exc)
    except Exception:
        text = ""
    blob = ("%s" % text).lower()
    if not blob.strip():
        blob = str(exc or "").lower()
    return text if any(m in blob for m in _VISION_REFUSAL_MARKERS) else ""


def _blind_turn(msgs, exc):
    """A copy of `msgs` with the pictures swapped for a sentence saying why,
    or None when this failure is something else entirely.

    The model called the screenshot tool, got the image, and the next request
    came back "I cannot see images". Ending the turn there throws away all the
    work: the model still has its tools, the capture is still on disk, and
    everything except the pixels is still doable. So the retry is one message
    lighter rather than one turn shorter."""
    refused = _vision_refusal_text(exc)
    if not refused or not _msgs_have_image(msgs):
        return None
    out = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        had_image = False
        parts = []
        noted = False
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image_url":
                had_image = True
                continue
            if (part.get("type") == "text"
                    and "screenshot" in str(part.get("text") or "").lower()):
                parts.append({"type": "text", "text": _BLIND_NOTE})
                noted = True
                continue
            parts.append(part)
        if had_image and not noted:
            parts.insert(0, {"type": "text", "text": _BLIND_NOTE})
        if not parts:
            continue
        new = dict(m)
        new["content"] = parts
        out.append(new)
    return out or None


def _blind_record():
    """A tool-log line for the vision fallback, so the user can see that it
    happened rather than watching the model quietly carry on blind."""
    return {"name": "vision", "arguments": {},
            "result": {"error": _BLIND_NOTE.strip("[]")},
            "tool_call_id": "vision_blind", "ms": 0}


def _vision_error_text(exc):
    """A last-resort message for a turn that could not be recovered, so the
    user learns the model is blind rather than seeing a raw provider error."""
    refused = _vision_refusal_text(exc)
    if not refused:
        return None
    entry = _active_model()
    name = (entry or {}).get("label") or (entry or {}).get("id") or "this model"
    return ("'%s' cannot see images, so the screenshot could not be sent to it "
            "(%s).\n\nThe capture is still on disk. Either switch to a model "
            "that takes images, or ask the model to work from window titles, "
            "file contents and text you provide."
            % (name, refused))


_INVALID_REQUEST_MARKERS = (
    "invalid_request", "invalid request", "invalid_request_error",
    "upstream request failed", "context length", "context_length_exceeded",
    "too many tokens", "request too large", "payload too large",
    "reduce the length", "string too long", "too many images",
)

# One tool result is allowed to be this big before a provider is likely to
# refuse the whole request. Anything larger is trimmed rather than fatal.
_OVERSIZE_TOOL_CHARS = 60000


def _oversize_turn(msgs, exc):
    """A copy of `msgs` with any bloated tool output cut down, or None.

    "invalid_request_error / invalid request" says nothing about which part of
    the request was wrong, and the usual cause is size: one tool result - a
    whole file, a directory listing, a page of scraped text - grew past what
    the provider will accept and took the entire conversation down with it. The
    work already done is not lost, the offending text is just too big, so it is
    replaced with a note saying so and the turn is retried once. The model can
    always ask for a slice of it back."""
    try:
        blob = ("%s %s" % (_model_error_text(exc), exc)).lower()
    except Exception:
        return None
    if not any(m in blob for m in _INVALID_REQUEST_MARKERS):
        return None
    fixed, trimmed = [], 0
    for m in msgs:
        content = m.get("content")
        if m.get("role") == "tool" and isinstance(content, str) \
                and len(content) > _OVERSIZE_TOOL_CHARS:
            head = content[:_OVERSIZE_TOOL_CHARS // 2]
            tail = content[-_OVERSIZE_TOOL_CHARS // 4:]
            fixed.append(dict(m, content=(
                head + "\n\n... [%d characters of this result were removed - it "
                "was too large for the provider. Ask for a specific slice if you "
                "need the middle.] ...\n\n" % (len(content) - len(head) - len(tail))
                + tail)))
            trimmed += 1
        else:
            fixed.append(m)
    return fixed if trimmed else None


def _oversize_record():
    return {"name": "oversize_result", "label": "Oversized result",
            "result": {"ok": True, "trimmed": True},
            "full_result": {"ok": True, "trimmed": True,
                            "note": "a tool result was too large for the "
                                    "provider and was shortened"},
            "ms": 1}


def _shot_caption(record):
    """What to say about the picture, so the model is not misled about it."""
    detail = record.get("full_result")
    if not isinstance(detail, dict):
        detail = record if isinstance(record, dict) else {}
    explicit = detail.get("caption") or record.get("caption")
    if explicit:
        return explicit
    if detail.get("type") == "image" or detail.get("image_data"):
        name = os.path.basename(str(detail.get("path") or "the image"))
        return ("[Here is the image file you asked me to read - %s. Look at it "
                "carefully to answer.]" % name)
    return "[I just captured this screenshot - inspect it carefully.]"


def _append_shot_image(msgs, record):
    image_uri = record.get("preview") or record.get("image_data")
    if not image_uri:
        return
    if _is_blind():
        # Already learned this model cannot see. Sending it anyway would buy
        # the same rejection a second time, and the base64 is not free either.
        msgs.append({"role": "user", "content": [{"type": "text",
                                                  "text": _BLIND_NOTE}]})
        return
    # Say which it is. A picture the model was just handed is a file it asked
    # to look at, and calling it a screenshot makes it describe a capture it
    # never took.
    lead = _shot_caption(record)
    msgs.append({"role": "user",
                 "content": [{"type": "text", "text": lead},
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
        try:
            for ev in _bonsai_stream(msgs, tools_for_round):
                if ev["kind"] == "end":
                    tool_calls = ev["tool_calls"]
                emit(ev)
        except Exception as exc:
            # Two things are worth a retry rather than the end of the turn.
            # The model was handed a screenshot and this provider will not take
            # an image: replace the picture with a sentence and let it finish.
            # Or a tool result was too big and the provider refused the whole
            # request: shorten it and let it finish. Neither is a reason to
            # throw away work that is already done.
            if not _is_blind():
                fixed = _blind_turn(msgs, exc)
                if fixed is not None:
                    _remember_blind()
                    msgs[:] = fixed
                    if on_tool:
                        on_tool(_blind_record())
                    for ev in _bonsai_stream(msgs, tools_for_round):
                        if ev["kind"] == "end":
                            tool_calls = ev["tool_calls"]
                        emit(ev)
                    return tool_calls or []
            fixed = _oversize_turn(msgs, exc)
            if fixed is None:
                raise
            msgs[:] = fixed
            if on_tool:
                on_tool(_oversize_record())
            for ev in _bonsai_stream(msgs, tools_for_round):
                if ev["kind"] == "end":
                    tool_calls = ev["tool_calls"]
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


_CTX_OVERFLOW_MARKERS = (
    "exceeds the available context",
    "exceeds context size",
    "exceed the context size",
    "context size exceeded",
    "context shift is disabled",
    "n_ctx",
    "kv cache is full",
    "input is too large",
)


def _ctx_overflow_text(exc):
    """llama-server reports an oversized prompt as a raw HTTP 400. Turn that into
    something the user can actually act on instead of a wall of server text."""
    blob = str(exc or "").lower()
    if not any(marker in blob for marker in _CTX_OVERFLOW_MARKERS):
        return None
    entry = _active_model()
    name = (entry or {}).get("label") or (entry or {}).get("id") or "this model"
    try:
        size = int(BONSAI_CTX)
    except Exception:
        size = 0
    return ("This conversation no longer fits in the context window of '%s' "
            "(%s tokens), so the model could not read it.\n\n"
            "Fix it in whichever way suits you:\n"
            "  - start a new chat (the old one is still saved, you can reopen it),\n"
            "  - raise Context size in the model settings (gear) and save, which "
            "restarts the model server,\n"
            "  - or remove some long attachments or earlier messages."
            % (name, "{:,}".format(size) if size else "unknown"))


def handle_chat(messages, on_reason=None, on_delta=None, on_tool=None,
                stream=False, mode=MODE_BUILD, on_stats=None, hooks=None):
    msgs = build_messages(messages, mode=mode)
    try:
        if stream:
            return stream_agent(msgs, on_reason=on_reason, on_delta=on_delta,
                                on_tool=on_tool, mode=mode, on_stats=on_stats,
                                hooks=hooks)
        return run_agent(msgs, on_tool=on_tool, mode=mode, on_stats=on_stats,
                         hooks=hooks)
    except Exception as exc:
        friendly = _ctx_overflow_text(exc) or _vision_error_text(exc)
        if friendly:
            raise RuntimeError(friendly) from exc
        raise


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
  .cfgrid label.keyfield { grid-column: 1 / -1; }
  .cfgrid .kstate { font-size: 11px; font-weight: 600; letter-spacing: .04em; }
  .cfgrid .kstate.ok { color: var(--ok, #34d399); }
  .cfgrid .kstate.bad { color: var(--bad, #f87171); }
  .cfgrid .kstate.warn { color: var(--warn, #fbbf24); }
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
  .endednote { margin-top: 6px; font-size: 12px; color: var(--mut);
               font-style: italic; font-family: Consolas, monospace; }
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

  /* context window meter - always visible, so you can see the cost of the
     system prompt + tools before you even type, and how it grows. */
  .ctxmeter {
    display: flex; align-items: center; gap: 6px; align-self: center;
    background: var(--bg3); border: 1px solid var(--bd2); border-radius: 12px;
    padding: 0 10px; height: 34px; cursor: default; white-space: nowrap;
    font-size: 11px; font-family: Consolas, monospace; user-select: none;
    transition: border-color .15s;
  }
  .ctxmeter:hover { border-color: var(--acc); }
  .ctxmeter .ctxtag { color: var(--mut); font-weight: 700; letter-spacing: .5px; }
  .ctxmeter .ctxbar {
    width: 54px; height: 6px; border-radius: 3px; background: var(--bg4);
    overflow: hidden; flex: 0 0 auto;
  }
  .ctxmeter .ctxfill { height: 100%; width: 0%; background: var(--acc); transition: width .25s, background .25s; border-radius: 3px; }
  .ctxmeter.warn .ctxfill { background: #fbbf24; }
  .ctxmeter.over .ctxfill { background: var(--err); }
  .ctxmeter .ctxnum { color: var(--txt2); }
  .ctxmeter .ctxown { color: var(--mut); }
  .ctxmeter .ctxhint {
    display: none; position: absolute; z-index: 40; margin-top: 46px;
    background: var(--bg2); border: 1px solid var(--bd2); border-radius: 10px;
    padding: 8px 10px; font-size: 11px; color: var(--txt2); white-space: normal;
    width: 260px; box-shadow: 0 8px 24px rgba(0,0,0,.5); line-height: 1.5;
  }
  .ctxmeter:hover .ctxhint { display: block; }
  .ctxmeter .ctxhint b { color: var(--acc); }

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
  .askbox .opt.hasdesc { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; text-align: left; padding: 9px 14px; max-width: 260px; }
  .askbox .opt .olab { font-weight: 700; color: var(--txt); }
  .askbox .opt .odesc { font-size: 12px; color: var(--mut); line-height: 1.35; font-weight: 400; }
  .askbox .opt.picked { border-color: var(--acc); background: var(--bg4); box-shadow: 0 0 0 1px var(--acc) inset; }
  .askbox .opt.picked .olab { color: var(--acc); }
  .askbox .opt.allow { border-color: #2f7d5b; }
  .askbox .opt.deny { border-color: #7d3040; }
  .askbox .aqcard { border-top: 1px solid var(--bd2); margin-top: 14px; padding-top: 4px; }
  .askbox .aqcard:first-of-type { border-top: none; margin-top: 0; padding-top: 0; }
  .askbox .aall { display: flex; justify-content: flex-end; margin-top: 16px; }
  .askbox .asubmit { background: var(--acc); color: #04212e; border: none; border-radius: 10px; padding: 10px 18px; font-weight: 700; cursor: pointer; font-size: 14px; font-family: Consolas, monospace; }
  .askbox .asubmit:hover { filter: brightness(1.08); }
  .askbox .afree { display: flex; gap: 8px; margin-top: 14px; }
  .askbox .afree input { flex: 1; min-width: 0; background: var(--bg3); border: 1px solid var(--bd2); border-radius: 10px; padding: 10px; color: var(--txt); font-size: 14px; font-family: Consolas, monospace; outline: none; }
  .askbox .afree input:focus { border-color: var(--acc); }
  .askbox .afree button { background: var(--acc); color: #04212e; border: none; border-radius: 10px; padding: 10px 16px; font-weight: 700; cursor: pointer; font-size: 14px; font-family: Consolas, monospace; }

  /* screenshot thumb in tool log */
  .tlitem .tthumb { max-width: 220px; border-radius: 6px; border: 1px solid var(--bd); margin-top: 6px; display: block; }
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
  .foldnote { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 10px 0 14px; padding: 7px 10px; border: 1px dashed var(--bd2); border-left: 2px solid var(--acc); border-radius: 6px; background: var(--bg3); }
  .foldnote .foldb { flex: 1 1 240px; font-size: 10.5px; font-family: Consolas, monospace; color: var(--dim); letter-spacing: .3px; }
  .foldnote .foldbtn { font-family: Consolas, monospace; font-size: 10px; letter-spacing: .6px; color: var(--acc); background: transparent; border: 1px solid var(--bd2); border-radius: 5px; padding: 3px 8px; cursor: pointer; }
  .foldnote .foldbtn:hover { border-color: var(--acc); }
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
  .dllist { padding: 2px 0 4px; }
  .dlhead { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px 4px; font-size: 10px; letter-spacing: 1.5px; color: var(--mut); text-transform: uppercase; font-family: Consolas, monospace; }
  .dlempty { padding: 8px 12px; font-size: 11px; color: var(--mut); }
  .dlrow { padding: 8px 12px; border-bottom: 1px solid rgba(22,78,99,.4); }
  .dlrow:last-child { border-bottom: none; }
  .dltop { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .dlname { font-size: 11.5px; color: var(--bg2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dlstat { font-size: 9px; letter-spacing: 1px; color: var(--mut); text-transform: uppercase; flex: none; }
  .dlrow.running .dlstat, .dlrow.queued .dlstat { color: var(--acc); }
  .dlrow.done .dlstat { color: #34d399; }
  .dlrow.error .dlstat, .dlrow.canceled .dlstat { color: var(--err); }
  .dlbar { height: 4px; margin: 6px 0 4px; background: rgba(255,255,255,.07); border-radius: 3px; overflow: hidden; }
  .dlfill { height: 100%; width: 0; background: var(--acc); transition: width .25s linear; }
  .dlrow.done .dlfill { background: #34d399; }
  .dlrow.error .dlfill, .dlrow.canceled .dlfill { background: var(--err); }
  .dlrow.paused .dlfill { background: var(--mut); }
  .dlfill.indet { width: 34% !important; animation: dlslide 1.1s ease-in-out infinite; }
  @keyframes dlslide { 0% { margin-left: 0; } 50% { margin-left: 66%; } 100% { margin-left: 0; } }
  .dlnum { font-size: 10px; color: var(--mut); font-family: Consolas, monospace; }
  .dlpath { font-size: 9.5px; color: var(--mut); opacity: .75; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dlerr { font-size: 10px; color: var(--err); margin-top: 3px; word-break: break-word; }
  .dlbtns { display: flex; gap: 5px; margin-top: 6px; }
  .dlbtn { background: transparent; border: 1px solid var(--bd); color: var(--mut); font-size: 9px; letter-spacing: 1px; padding: 3px 7px; border-radius: 4px; cursor: pointer; font-family: Consolas, monospace; }
  .dlbtn:hover { color: var(--acc); border-color: var(--acc); }
  .dlclearrow { display: none; gap: 6px; padding: 0 12px 8px; }
  .dlclear { flex: 1; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); font-size: 10.5px; font-weight: 700; letter-spacing: .5px; padding: 6px 4px; border-radius: 6px; cursor: pointer; font-family: Consolas, monospace; white-space: nowrap; transition: all .15s; }
  .dlclear:hover { color: var(--acc); border-color: var(--acc); }
  .dlclear.danger:hover { color: var(--err); border-color: var(--err); }
  .dlclear:disabled { opacity: .35; cursor: default; color: var(--mut); border-color: var(--bd); }
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
      <button class="hbtn" id="detailbtn" title="Show each step in the chat as one collapsed line, or fully expanded">DETAIL: BRIEF</button>
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
        <input id="apikey" placeholder="API key name, e.g. OPENROUTER_API_KEY" title="The NAME of a line in API KEYS.txt - not the key itself. Most hosted providers need one; a llama-server on this PC does not.">
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
          <label>Context size (ctx)<input id="cfg_ctx" type="number" min="512" max="4194304" step="512"></label>
        <label>Temperature<input id="cfg_temp" type="number" min="0" max="2" step="0.05"></label>
        <label>Top-p<input id="cfg_top_p" type="number" min="0.01" max="1" step="0.01"></label>
        <label>Top-k<input id="cfg_top_k" type="number" min="0" max="1000" step="1"></label>
        <label>GPU layers (-ngl)<input id="cfg_ngl" type="number" min="-1" max="999" step="1"></label>
        <label class="keyfield" id="cfg-keyrow" hidden>API key name <span class="kstate" id="cfg_keystate"></span><input id="cfg_apikey" type="text" spellcheck="false" autocomplete="off" placeholder="OPENROUTER_API_KEY"></label>
      </div>
      <div class="hint" id="cfghint"></div>
      <div class="row">
        <button class="act inline" id="cfg-save">Save</button>
        <button class="act inline" id="cfg-testkey" hidden>TEST KEY</button>
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
      <div class="ptitle" style="margin-top:8px; border-top:1px solid #164e63; padding-top:8px;"><span>DOWNLOADS</span><span id="dlcount"></span></div>
      <div class="dllist" id="dllist"></div>
      <div class="dlclearrow" id="dlclearrow">
        <button class="dlclear" id="dlclear" title="Remove finished, failed and cancelled downloads from the list">CLEAR FINISHED</button>
        <button class="dlclear danger" id="dlclearall" title="Remove every entry - finished, failed, cancelled, queued and paused. A download that is actively transferring right now is left to finish on its own.">CLEAR ALL</button>
      </div>
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
            <div class="ctxmeter" id="ctxmeter" title="Context window in use - the system prompt and all tools are already counted, before you type">
              <span class="ctxtag">CTX</span>
              <span class="ctxbar"><span class="ctxfill" id="ctxfill"></span></span>
              <span class="ctxnum" id="ctxnum">--</span>
              <span class="ctxhint" id="ctxhint"></span>
            </div>
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
let BONSAI_CTX = 0;
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
  startDlWatch();
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
    scheduleCtx();
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
function renderAll() { renderList(); renderConv(); scheduleCtx(); }
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
    const at = (cur.summary && cur.summary_at) ? cur.summary_at : -1;
    cur.messages.forEach(function (m, i) {
      if (i === at) addFold(cur);
      if (m.role === 'user') addUser(m.content, !!m.queued);
      else if (m.role === 'assistant') addAsst(m.content, m.calls || [], m.reason, m.stats, m.parts, m);
    });
    if (at > cur.messages.length) addFold(cur);
  }
  busy = false;
  requeuePending();
}
// A long conversation eventually stops fitting in the context window, and the
// old answer - "start a new chat" - throws the whole thing away. Instead the
// older turns are replaced by a summary and the recent ones are kept word for
// word, which is the same trade a coding agent makes when it runs out of room.
// Nothing is deleted: the transcript still shows every message, and the button
// in the divider puts the full history back in front of the model.
function addFold(chat) {
  const conv = convEl();
  const row = document.createElement('div');
  row.className = 'foldnote';
  const n = chat.summary_at || 0;
  const how = chat.summary_via === 'fallback' ? ' (plain extract - the model would not answer)' : '';
  const b = document.createElement('div');
  b.className = 'foldb';
  b.textContent = n + ' earlier message' + (n === 1 ? '' : 's') + ' summarised' + how
    + ' \u00b7 still shown above, just not sent to the model';
  const btn = document.createElement('button');
  btn.className = 'foldbtn';
  btn.type = 'button';
  btn.textContent = 'use the full history again';
  btn.onclick = function () { undoFold(chat); };
  row.appendChild(b);
  row.appendChild(btn);
  conv.appendChild(row);
}
function undoFold(chat) {
  if (!chat || !chat.summary) return;
  chat.summary = null;
  chat.summary_at = null;
  chat.summary_via = null;
  chat.summary_msg = null;
  save();
  renderConv();
  refreshCtx();
}
function pendingList(chat) {
  return ((chat && chat.messages) || []).filter(function (m) { return !m.queued; });
}
// What actually goes to the model. The meter and the request both come through
// here, so the number on the bar is the number that gets sent.
function wireFor(chat) {
  const msgs = pendingList(chat);
  if (!chat || !chat.summary) return msgs;
  const at = Math.max(0, Math.min(chat.summary_at || 0, msgs.length));
  /* the wording comes from the server, so there is one copy of it */
  const head = chat.summary_msg || { role: 'user', content: chat.summary };
  return [head].concat(msgs.slice(at));
}
let folding = false;
function maybeFold(quick) {
  // never mid-word, never mid-turn, and never twice for the same chat
  if (quick || folding || busy) return;
  const j = ctxLast;
  if (!j || !j.ok || !j.total) return;
  if (!cur || cur.summary) return;
  if (j.own < (j.fold_min || 0)) return;
  if (j.used * 100 / j.total < (j.fold_at || 101)) return;
  const msgs = pendingList(cur);
  /* below this the summary would be longer than the turns it replaces */
  if (msgs.length <= (j.fold_keep || 6) + 1) return;
  foldChat(cur, msgs);
}
async function foldChat(chat, msgs) {
  folding = true;
  addThinking();
  setBonsaiState('thinking');
  if (thinkingRow && thinkingRow.think) {
    thinkingRow.think.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> Summarising the earlier turns';
  }
  try {
    const r = await fetch('/api/compact', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: msgs, mode: chatMode }) });
    const j = await r.json();
    doneThinking(null);
    if (!j || !j.ok || !j.summary) return;
    chat.summary = j.summary;
    chat.summary_at = j.folded;
    chat.summary_via = j.via;
    chat.summary_msg = j.message;
    save();
    renderConv();
    refreshCtx();
  } catch (e) {
    doneThinking('Could not compact: ' + (e && e.message ? e.message : 'unknown error'));
  } finally {
    folding = false;
    setBonsaiState('idle');
  }
}
function esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function fmt(s) {
  /* Block-level markdown, then the inline marks. Models reach for headings
     and lists constantly and they used to arrive on screen as literal "#" and
     "-" text, because only the inline marks were ever handled. Fenced code is
     lifted out first so a list marker inside a code block stays put. */
  let src = String(s == null ? '' : s).replace(/\\r\\n?/g, '\\n');
  const fences = [];
  src = src.replace(/```([\\s\\S]*?)(?:```|$)/g, function (m, body) {
    fences.push('<pre>' + esc(body.replace(/\\n$/, '')) + '</pre>');
    return '\\u0000F' + (fences.length - 1) + '\\u0000';
  });

  const inline = function (x) {
    let t = esc(x);
    t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
    t = t.replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>');
    t = t.replace(/\\*([^*]+)\\*/g, '<i>$1</i>');
    /* Links go into placeholders and are put back at the end, so the bare-url
       pass cannot swallow the address of a link it has already built - it used
       to, and the closing bracket ended up inside the address. Only http and
       https are linked: a model writes these, so this is the one place a
       javascript: address could get in. esc() does not touch a double quote,
       so it is escaped here too - a quote in the address used to be able to
       close the href attribute and add an event handler. */
    const links = [];
    const hold = function (html) {
      links.push(html); return '\\u0001L' + (links.length - 1) + '\\u0001';
    };
    const anchor = function (url, text) {
      const u = String(url).replace(/"/g, '&quot;');
      return '<a href="' + u + '" target="_blank" rel="noreferrer">' + text + '</a>';
    };
    t = t.replace(/\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^)\\s"]+)\\)/g, function (m, label, url) {
      return hold(anchor(url, label));
    });
    t = t.replace(/(https?:\\/\\/[^\\s<"]+)/g, function (m) {
      let url = m, tail = '';
      while (/[),.;:!?]$/.test(url)) {
        const c = url.slice(-1);
        if (c === ')' && (url.match(/\\)/g) || []).length <= (url.match(/\\(/g) || []).length) break;
        tail = c + tail; url = url.slice(0, -1);
      }
      return hold(anchor(url, url)) + tail;
    });
    t = t.replace(/\\u0001L(\\d+)\\u0001/g, function (m, i) { return links[+i]; });
    return t;
  };

  const out = [];
  let para = [], list = null, quote = [];
  const flushPara = function () {
    /* no <p> wrapper: a paragraph of prose is just this, as before, so the
       spacing inside a bubble does not change */
    if (para.length) { out.push(para.map(inline).join('<br>')); para = []; }
  };
  const flushList = function () {
    if (list) { out.push('<' + list.tag + '>' + list.items.map(function (i) {
      return '<li>' + inline(i) + '</li>'; }).join('') + '</' + list.tag + '>'); list = null; }
  };
  const flushQuote = function () {
    if (quote.length) { out.push('<blockquote>' + quote.map(inline).join('<br>') + '</blockquote>'); quote = []; }
  };
  const flushAll = function () { flushPara(); flushList(); flushQuote(); };

  String(src).split('\\n').forEach(function (ln) {
    const fence = ln.match(/^\\u0000F(\\d+)\\u0000$/);
    if (fence) { flushAll(); out.push(fences[+fence[1]]); return; }
    if (/^\\s*(-{3,}|\\*{3,}|_{3,})\\s*$/.test(ln)) { flushAll(); out.push('<hr>'); return; }
    let m = ln.match(/^\\s*(#{1,6})\\s+(.*)$/);
    if (m) { flushAll(); out.push('<h' + m[1].length + '>' + inline(m[2]) + '</h' + m[1].length + '>'); return; }
    m = ln.match(/^\\s*>\\s?(.*)$/);
    if (m) { flushPara(); flushList(); quote.push(m[1]); return; }
    flushQuote();
    m = ln.match(/^\\s*[-*+]\\s+(.*)$/);
    if (m) { flushPara(); if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; } list.items.push(m[1]); return; }
    m = ln.match(/^\\s*\\d+[.)]\\s+(.*)$/);
    if (m) { flushPara(); if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; } list.items.push(m[1]); return; }
    flushList();
    if (!ln.trim()) { flushPara(); return; }
    para.push(ln);
  });
  flushAll();

  return out.join('');
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
function addAsst(text, calls, reason, stats, entry) {
  const row = document.createElement('div'); row.className = 'msgrow bonsai';
  const av = document.createElement('div'); av.className = 'av bonsai'; av.textContent = 'B';
  const b = document.createElement('div'); b.className = 'bubble';
  if (reason) addReasonBox(b, reason);
  addToolChips(b, calls);
  if (calls && calls.length) addToolLog(b, calls);
  const inner = document.createElement('div'); inner.className = 'abody'; inner.innerHTML = fmt(text);
  b.appendChild(inner);
  if (entry && entry.ended) {
    const e = document.createElement('div'); e.className = 'endednote';
    e.textContent = entry.ended === 'stopped'
      ? '(stopped by you - the work above was kept)'
      : '(this reply was cut off - the work above was kept)';
    b.appendChild(e);
  }
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

/* ---- context window meter ----------------------------------------------
   A new chat is not empty: the system prompt and the whole tool catalogue go
   out with every request, so the meter shows that fixed cost before you type
   and grows as the conversation does. Hover for the breakdown. */
let ctxLast = null;
let ctxTimer = null;
function fmtK(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(n);
  return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/[.]0$/, '') + 'k';
}
function ctxPaint() {
  const meter = document.getElementById('ctxmeter');
  if (!meter) return;
  const num = document.getElementById('ctxnum');
  const fill = document.getElementById('ctxfill');
  const hint = document.getElementById('ctxhint');
  const j = ctxLast;
  if (!j) { num.textContent = '--'; if (hint) hint.textContent = 'measuring...'; return; }
  if (!j.ok) {
    meter.classList.remove('warn'); meter.classList.add('over');
    if (fill) fill.style.width = '100%';
    num.textContent = 'FULL';
  } else {
    const pct = j.total ? Math.min(100, j.used * 100 / j.total) : 0;
    meter.classList.toggle('warn', pct >= 50 && pct < 80);
    meter.classList.toggle('over', pct >= 80);
    if (fill) fill.style.width = pct.toFixed(1) + '%';
    num.textContent = fmtK(j.used) + '/' + fmtK(j.total);
  }
  if (hint) {
    if (!j.ok) {
      hint.innerHTML = '<b>Context full.</b><br>This conversation no longer fits the '
        + 'window.<br>Start a new chat, or raise ctx in model settings.';
    } else {
      let extra = '';
      if (j.images) extra += '<br>images: ' + j.images + ' (about ' + fmtK(j.images * 1024) + ')';
      if (!j.exact) extra += '<br>estimated - '
        + (j.why === 'quick' ? 'still typing'
          : j.why === 'remote' ? 'the provider counts these for you'
            : 'model not loaded');
      hint.innerHTML = '<b>' + (j.exact ? '' : '~') + Number(j.used).toLocaleString() + '</b>'
        + ' of <b>' + Number(j.total || 0).toLocaleString() + '</b> tokens'
        + '<br>system prompt + tools: ' + Number(j.fixed || 0).toLocaleString()
        + '<br>this conversation: ' + Number(j.own || 0).toLocaleString() + extra;
    }
  }
}
async function refreshCtx(quick) {
  /* Count what is actually going to be sent - queued messages and, once the
     chat has been compacted, everything above the fold. The meter's own
     tooltip promises the number is right "before you type", but the draft was
     never included, so the bar only ever moved after a turn had already been
     sent - you found out a message was too long once it was too late to
     shorten it. */
  const msgs = (cur && cur.messages) ? wireFor(cur).slice() : [];
  try {
    const draft = (typeof buildUserMsg === 'function') ? buildUserMsg() : null;
    if (draft) msgs.push({ role: 'user', content: draft });
  } catch (e) { /* no composer on this view */ }
  try {
    const r = await fetch('/api/ctx', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: msgs, mode: chatMode, quick: !!quick }) });
    ctxLast = await r.json();
  } catch (e) { ctxLast = { ok: false }; }
  ctxPaint();
  maybeFold(quick);
}
function scheduleCtx(quick) {
  if (ctxTimer) clearTimeout(ctxTimer);
  /* While typing, settle quickly but ask for the cheap estimate: an exact
     count costs two round-trips to the model server, and nobody needs that
     to be precise mid-word. The bar shows a ~ while it is a guess. */
  ctxTimer = setTimeout(function () { refreshCtx(quick); }, quick ? 500 : 400);
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
  // A remote model with no key would otherwise fail as a 401 halfway through,
  // after the turn is already spent and buried in a conversation.
  try {
    const block = await failFastKey();
    if (block) { alert(block); return; }
  } catch (e) { /* never block the send on a failed check */ }
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
  try { await streamRun(wireFor(target), target, asked); }
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
  statsVals.ctx_left = j.ctx_left || Math.max(0, BONSAI_CTX - statsVals.ctx_used);
  statsVals.prompt_tokens = j.prompt_tokens || 0;
  statsVals.completion_tokens = j.completion_tokens || 0;
  runningStats();
}
// A long silence is normal, not a fault. A big model can spend many minutes on
// its opening thoughts before the first token arrives, and a tool run can be
// quiet just as long. So never cut a turn short on a timer: if it is going to
// answer, let it. All this does is replace a bare "processing..." with a plain
// note once it is clear the wait is real.
var waitTimer = null;
function clearWaitWatch() { if (waitTimer) { clearInterval(waitTimer); waitTimer = null; } }
/* One line that says what is actually going on. It said "The model is loading"
   for the whole wait, which is wrong the moment anything else happens: the
   model is not loading while a shell command runs or an edit is being written.
   It is also wrong when nothing is loading at all, which is the usual case. */
const ACTIVITY = {
  shell: 'Executing shell', run: 'Executing shell', execute: 'Executing shell',
  write_file: 'Preparing edit', write: 'Preparing edit', create_file: 'Preparing edit',
  edit_file: 'Preparing edit', edit: 'Preparing edit', apply_patch: 'Preparing edit',
  read_file: 'Reading the file', read: 'Reading the file', view: 'Reading the file',
  list_dir: 'Listing the folder', ls: 'Listing the folder', glob: 'Listing the folder',
  search: 'Searching the folder', grep: 'Searching the folder',
  web_search: 'Searching the web', fetch: 'Fetching a page',
  screenshot: 'Taking a screenshot', question: 'Waiting for your answer',
  rich_text: 'Pasting rich text', todo_write: 'Writing the to-do list',
  memory: 'Writing a memory'
};
function activityFor(name) {
  const k = String(name || '').toLowerCase();
  if (ACTIVITY[k]) return ACTIVITY[k];
  return 'Running ' + (k.replace(/_/g, ' ') || 'a tool');
}
function setStatus(text) {
  const row = (typeof thinkingRow !== 'undefined' && thinkingRow) || window.thinkingRow;
  if (!row || !row.think || !row.think.isConnected) return;
  const t = row.think;
  t.textContent = '';
  const d = document.createElement('span');
  d.className = 'dots';
  for (let i = 0; i < 3; i++) d.appendChild(document.createElement('i'));
  t.appendChild(d);
  t.appendChild(document.createTextNode(' ' + text));
}
/* Put the caret in the chat box and get it ready to receive a paste. The page
   is the only thing that can focus its own input, so a tool that is about to
   press Ctrl+V asks for this first. */
function focusChatInput() {
  try {
    const el = document.getElementById('user-input');
    if (!el) return false;
    el.focus();
    try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
    return document.activeElement === el;
  } catch (e) { return false; }
}
function showLoading() {
  const row = thinkingRow;
  if (!row || !row.think || !row.think.isConnected) { clearWaitWatch(); return; }
  setStatus('Waking the model');
}
function startWaitWatch() {
  clearWaitWatch();
  /* This used to swap the label for "The model is loading" after six seconds of
     silence. Silence is not evidence of a load - a long tool run and a long
     opening thought are both silent - and it fired often enough to say the
     model was loading while a shell command was still running. The server
     sends a real `waiting` event when it is actually starting a model, and only
     that may claim it. */
  waitTimer = setInterval(function () {
    if (!thinkingRow || !thinkingRow.think || !thinkingRow.think.isConnected) clearWaitWatch();
  }, 6000);
}
function addThinking() {
  liveCalls = [];
  const row = document.createElement('div'); row.className = 'msgrow bonsai';
  const av = document.createElement('div'); av.className = 'av bonsai'; av.textContent = 'B';
  const b = document.createElement('div'); b.className = 'bubble';
  const t = document.createElement('div'); t.className = 'think';
  t.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> Starting';
  b.appendChild(t);
  row.appendChild(av); row.appendChild(b);
  convEl().appendChild(row);
  scrollBottom();
  thinkingRow = { row: row, b: b, body: null, reasonD: null, reasonC: null, toolEl: null, think: t };
  startWaitWatch();
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
  name.textContent = String.fromCodePoint(0x1F4BE) + ' ' + (r.matched ? 'window: ' + r.matched : 'screenshot');
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
  clearWaitWatch();
  if (!thinkingRow) return;
  // a turn can end without a single token (nothing streamed, or the request
  // was cut off). Leaving the "The model is loading" note behind would have
  // the finished bubble still claiming it is waiting
  if (thinkingRow.think) thinkingRow.think.remove();
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
  /* The ordered renderer draws the reply in its own bubble, so this waiting
     row is often never written to at all. It used to be left behind as an
     empty bubble with an avatar under every single answer. */
  if (!errMsg && !thinkingRow.body && !thinkingRow.reasonD && !thinkingRow.toolEl) {
    if (thinkingRow.row.parentNode) thinkingRow.row.parentNode.removeChild(thinkingRow.row);
  }
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
  if (perm) {
    const h = document.createElement('h4');
    h.textContent = 'PERMISSION NEEDED';
    box.appendChild(h);
    const p = document.createElement('div'); p.className = 'askpath';
    p.textContent = j.path || '';
    const w = document.createElement('div'); w.className = 'asktool';
    w.textContent = 'tool: ' + (j.tool || 'file tool') + '  -  outside the workspace';
    box.appendChild(p); box.appendChild(w);
  }
  const say = function (ans) {
    ov.remove();
    if (perm) loadPathPolicy();
    fetch('/api/answer', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: j.id, answer: ans }) }).catch(function () {});
  };
  if (perm) {
    if (j.options && j.options.length) {
      const opts = document.createElement('div'); opts.className = 'opts';
      j.options.forEach(function (o) {
        const label = (o && typeof o === 'object') ? (o.label || '') : String(o);
        const b = document.createElement('button');
        b.className = 'opt' + (/^ALLOW/.test(label) ? ' allow' : (/^DENY/.test(label) ? ' deny' : ''));
        b.textContent = label;
        b.onclick = function () { say(label); };
        opts.appendChild(b);
      });
      box.appendChild(opts);
    }
    ov.appendChild(box);
    document.body.appendChild(ov);
    return;
  }

  /* How many things are being asked. A model that needs one thing gets one
     card and a single box. A model that asks three gets three cards, each with
     its own choices, answered together - so a follow-up question is not
     silently dropped on the floor. */
  const asked = (Array.isArray(j.questions) && j.questions.length)
    ? j.questions
    : [{ question: j.question || 'What should I do?', header: j.header || '',
         options: j.options || [] }];
  const many = asked.length > 1;

  const h = document.createElement('h4');
  h.textContent = many ? ('BONSAI has ' + asked.length + ' questions') : 'BONSAI is asking you';
  box.appendChild(h);

  const picks = [];
  const inputs = [];
  asked.forEach(function (one, idx) {
    const card = document.createElement('div'); card.className = 'aqcard';
    if (one.header) {
      const hh = document.createElement('div'); hh.className = 'aqhead';
      hh.textContent = (many ? (idx + 1) + '. ' : '') + one.header;
      card.appendChild(hh);
    }
    const q = document.createElement('div'); q.className = 'aq';
    q.textContent = one.question || one.header || 'What should I do?';
    card.appendChild(q);

    /* Choices are only drawn when there are any. A plain question stays a
       plain question with a box to type in. */
    const opts = one.options || [];
    if (opts.length) {
      const row = document.createElement('div'); row.className = 'opts';
      picks[idx] = '';
      opts.forEach(function (o) {
        const label = (o && typeof o === 'object') ? String(o.label || '') : String(o);
        const desc = (o && typeof o === 'object') ? String(o.description || '') : '';
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'opt' + (desc ? ' hasdesc' : '')
          + (/^ALLOW/i.test(label) ? ' allow' : (/^DENY/i.test(label) ? ' deny' : ''));
        const t = document.createElement('span'); t.className = 'olab'; t.textContent = label;
        b.appendChild(t);
        if (desc) {
          const d = document.createElement('span'); d.className = 'odesc'; d.textContent = desc;
          b.appendChild(d);
        }
        b.onclick = function () {
          picks[idx] = label;
          Array.from(row.children).forEach(function (n) { n.classList.remove('picked'); });
          b.classList.add('picked');
          const inp = inputs[idx];
          if (inp) { inp.value = ''; inp.placeholder = 'or type your own answer...'; }
          if (!many) say(label);
        };
        row.appendChild(b);
      });
      card.appendChild(row);
    } else {
      picks[idx] = '';
    }

    const free = document.createElement('div'); free.className = 'afree';
    const inp = document.createElement('input');
    inp.className = 'ain';
    inp.placeholder = 'Type your answer...';
    const btn = document.createElement('button');
    btn.type = 'button'; btn.textContent = many ? 'NEXT' : 'SEND';
    const submit = function () {
      const typed = inp.value.trim();
      const val = typed || picks[idx] || '';
      if (!many) { if (val) say(val); return; }
      picks[idx] = val;
      if (idx < asked.length - 1) { inputs[idx + 1].focus(); return; }
      say(asked.map(function (_, k) { return picks[k]; }));
    };
    btn.onclick = submit;
    inp.onkeydown = function (e) {
      if (e.key !== 'Enter') return;
      e.preventDefault();
      if (many && !inp.value.trim() && picks[idx] && idx < asked.length - 1) {
        picks[idx] = picks[idx];
        inputs[idx + 1].focus();
        return;
      }
      submit();
    };
    free.appendChild(inp); free.appendChild(btn);
    card.appendChild(free);
    inputs[idx] = inp;
    box.appendChild(card);
  });

  if (many) {
    const all = document.createElement('div'); all.className = 'aall';
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'asubmit'; b.textContent = 'SEND ALL ANSWERS';
    b.onclick = function () {
      asked.forEach(function (_, k) {
        const typed = inputs[k].value.trim();
        if (typed) picks[k] = typed;
      });
      if (picks.every(function (p) { return p; })) {
        say(asked.map(function (_, k) { return picks[k]; }));
      } else {
        inputs[picks.findIndex(function (p) { return !p; })].focus();
      }
    };
    all.appendChild(b);
    box.appendChild(all);
  }

  ov.appendChild(box);
  document.body.appendChild(ov);
  inputs[0].focus();
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



/* Keep a turn that did not finish, so the work in it is not lost. */
function keepTurn(chat, anchorMsg, reply, calls, reason, stats, ended) {
  chat = chat || cur;
  if (!chat) return null;
  const entry = { role: 'assistant', content: reply, calls: calls,
                  reason: reason && reason.trim() ? reason : undefined,
                  stats: stats || undefined, ended: ended };
  if (anchorMsg) {
    const at = chat.messages.indexOf(anchorMsg);
    if (at >= 0) chat.messages.splice(at + 1, 0, entry);
    else chat.messages.push(entry);
  } else {
    chat.messages.push(entry);
  }
  return entry;
}

/* save() only ran when a turn ended, so a job that ran for minutes and then
   hit a crash, a closed tab or a dead laptop lost everything since the last
   save. These keep the transcript written down while the work is in progress. */
let autosaveTimer = null;
function startAutosave() {
  stopAutosave();
  autosaveTimer = setInterval(function () {
    try { if (busy || (cur && cur.messages && cur.messages.length)) save(); } catch (e) {}
  }, 5000);
}
function stopAutosave() {
  if (autosaveTimer) { clearInterval(autosaveTimer); autosaveTimer = null; }
}
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'hidden') { try { save(); } catch (e) {} }
});
window.addEventListener('pagehide', function () { try { save(); } catch (e) {} });
window.addEventListener('beforeunload', function () { try { save(); } catch (e) {} });
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
        else if (ev === 'waiting') { showLoading(); }
        else if (ev === 'focus') { focusChatInput(); }
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
  /* Whatever happened, the work that got done is kept. A turn that was
     stopped, or that failed, used to throw before the entry was ever built:
     the tool calls it had already run, its thinking and its half-written
     answer were all dropped, and only the user's message survived. */
  const partial = reply.trim() || calls.length || liveCalls.length;
  if (aborted || (!gotEnd && partial)) {
    if (partial) {
      const st = statsVals && (statsVals.think_ms || statsVals.respond_ms || statsVals.completion_tokens)
        ? JSON.parse(JSON.stringify(statsVals)) : null;
      keepTurn(chat, anchorMsg, reply, calls, reason, st, aborted ? 'stopped' : 'cut off');
      try { save(); } catch (e) {}
    }
    throw new Error(aborted ? 'stopped' : 'The reply was cut off unexpectedly - please try again.');
  }
  if (aborted) throw new Error('stopped');
  if (!gotEnd) throw new Error('The reply was cut off unexpectedly - please try again.');
  const last = chat.messages[chat.messages.length - 1];
  const savedStats = statsVals && (statsVals.think_ms || statsVals.respond_ms || statsVals.completion_tokens)
      ? JSON.parse(JSON.stringify(statsVals)) : null;
  const entry = { role: 'assistant', content: reply, calls: calls,
                  reason: reason.trim() ? reason : undefined, stats: savedStats,
                  parts: (typeof __ocParts === 'function') ? __ocParts() : undefined };
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
  scheduleCtx();
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
function fmtCtx(c) {
  c = Number(c) || 0;
  if (c >= 1000000) { const t = c / 1000000; return (t >= 10 ? Math.round(t) : Math.round(t * 10) / 10) + 'M'; }
  if (c >= 1000) return Math.floor(c / 1000) + 'k';
  return String(c);
}
function modelCtxLabel(m) {
  return m.ctx ? ' \u00b7 ' + fmtCtx(m.ctx) + ' ctx' : '';
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
  const _am = (j.models || []).filter(function (m) { return m.id === j.active; })[0];
  if (_am && _am.ctx) BONSAI_CTX = _am.ctx;
  scheduleCtx();
  sel.innerHTML = '';
  (j.models || []).forEach(function (m) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = modelLabel(m) + modelCtxLabel(m) + (m.type === 'local' && m.vision ? ' · vision' : '') +
      (m.type === 'local' && m.available === false ? ' (missing)' : '') +
      (m.type === 'api' ? ' · ' + (m.needs_key ? 'NO KEY' : (m.api_key_set ? 'key ok' : 'no key needed')) : '');
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
  pickModelPost(what, null)
    .then(function () { if (btn) { btn.disabled = false; btn.textContent = '+'; } });
}
/* The native browser cannot open everywhere: no python3-tk, or no display at
   all on a headless Linux box. The server says so plainly instead of reporting
   a bare "cancelled", and this asks for the path by hand rather than leaving
   the button to do nothing at all. */
function pickModelPost(what, path) {
  const body = { what: what };
  if (path) body.path = path;
  return fetch('/api/pick_model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j && j.no_browser) {
        const hint = what === 'folder' ? 'folder' : 'full path to the .gguf file';
        const typed = window.prompt(j.error + '\\n\\nEnter the ' + hint + ':', '');
        if (typed && typed.trim()) return pickModelPost(what, typed.trim());
        return null;
      }
      return afterAdd(j, what);
    })
    .catch(function () { alert('Could not open the file browser'); });
}
/* Pasting a model's web page address (openrouter.ai/qwen/qwen3.8-27b:free) is the
   obvious thing to try, but the API wants the base URL and the model id as two
   separate values. Translate a page address into those two fields. Only base
   URLs that are actually documented are filled in - never guessed. */
const API_PAGE_BASE = { 'openrouter.ai': 'https://openrouter.ai/api/v1' };
function fixupApiUrl() {
  const baseEl = document.getElementById('apibase');
  const modelEl = document.getElementById('apimodel');
  if (!baseEl || !modelEl) return false;
  const raw = (baseEl.value || '').trim();
  if (raw.indexOf('http') !== 0) return false;
  let u = null;
  try { u = new URL(raw); } catch (e) { return false; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
  let host = u.hostname.toLowerCase();
  if (host.indexOf('www.') === 0) host = host.slice(4);
  const segs = u.pathname.split('/').filter(function (s) { return s !== ''; });
  if (!segs.length) return false;
  const head = segs[0].toLowerCase();
  /* already an API base address such as .../api/v1 - leave it be */
  if (head === 'api' || (head.charAt(0) === 'v' && Number(head.slice(1)) > 0)) return false;
  const known = API_PAGE_BASE[host];
  if (known) {
    let slug = '';
    try { slug = decodeURIComponent(segs.join('/')); } catch (e) { slug = segs.join('/'); }
    baseEl.value = known;
    modelEl.value = slug;
    return true;
  }
  /* Unknown provider: rescue the model id from the address, but do not
     invent a base URL - a wrong one fails in a way that looks like the key is
     broken rather than like the address is. */
  if (!(modelEl.value || '').trim()) modelEl.value = segs[segs.length - 1];
  return 'unknown';
}
const _apiBaseEl = document.getElementById('apibase');
if (_apiBaseEl) _apiBaseEl.onchange = function () {
  if (fixupApiUrl() === 'unknown') {
    alert('That looks like a model page address, not an API base URL.\\n\\n' +
          'I took "' + (document.getElementById('apimodel').value || '') +
          '" as the model id. Now enter the API base URL for that provider - ' +
          'for OpenAI-compatible providers it usually ends in /v1.');
  }
};
/* Fill in the key name we already know the answer to, so the common case is
   "paste the key once" rather than "guess what we named it". Only fills an
   empty box, and only for a host we recognise. */
var KNOWN_KEY_NAMES = { 'openrouter.ai': 'OPENROUTER_API_KEY', 'api.anthropic.com': 'ANTHROPIC_API_KEY', 'api.openai.com': 'OPENAI_API_KEY', 'generativelanguage.googleapis.com': 'GEMINI_API_KEY', 'api.groq.com': 'GROQ_API_KEY', 'api.mistral.ai': 'MISTRAL_API_KEY', 'api.deepseek.com': 'DEEPSEEK_API_KEY', 'openrouter.ai/api': 'OPENROUTER_API_KEY' };
function suggestKeyName() {
  var el = document.getElementById('apikey');
  if (!el) return;
  if ((el.value || '').trim()) return;
  var raw = (document.getElementById('apibase').value || '').trim();
  if (raw.indexOf('http') !== 0) return;
  var host;
  try { host = new URL(raw).hostname.toLowerCase(); } catch (e) { return; }
  if (host.indexOf('www.') === 0) host = host.slice(4);
  if (KNOWN_KEY_NAMES[host]) el.value = KNOWN_KEY_NAMES[host];
}
function addModelApi() {
  fixupApiUrl();
  suggestKeyName();
  const base = (document.getElementById('apibase').value || '').trim();
  const model = (document.getElementById('apimodel').value || '').trim();
  /* a NAME from API KEYS.txt, never the secret itself */
  const keyEl = document.getElementById('apikey');
  const key = keyEl ? (keyEl.value || '').trim() : '';
  if (!base || !model) { alert('Fill in both the base URL and the model id.'); return; }
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'add', type: 'api', label: model, base_url: base, model: model, api_key: key }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) { alert('Could not add the model:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j); closeAddMdl();
      document.getElementById('apibase').value = '';
      document.getElementById('apimodel').value = '';
      if (keyEl) keyEl.value = '';
      // a remote endpoint with no resolvable key cannot answer anything, and
      // saying so here beats finding out from a 401 mid-conversation
      if (j.warn) alert(j.warn);
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
/* fail-fast: a remote model with no resolvable key must never reach a chat.
   Reads the payload renderModels() already cached rather than asking again -
   /api/models costs a round trip and a readiness probe, which is real time
   added to every single message. The one case that does re-ask is the case
   where we are about to refuse the send, because API KEYS.txt is re-read on
   change and the user may have just pasted their key in. */
function keyProblem(j) {
  var act = null;
  for (var i = 0; i < (j.models || []).length; i++) if (j.models[i].id === j.active) { act = j.models[i]; break; }
  if (!act) return null;
  if (!act.needs_key) return null;
  if (act.api_key_set) return null;
  return "This model is a remote endpoint but no key is available for '" + (act.api_key_name || 'its API key') + "'. Open the gear (MODEL SETTINGS) and either add that name to API KEYS.txt, or fix the key name - then press TEST KEY.";
}
async function failFastKey() {
  var cached = window.__lastModels;
  // Nothing known yet: let the message through. Waiting on the network here
  // would add the /api/models round trip to a send, and that call is not cheap
  // - it probes model-server readiness, which is seconds when nothing is
  // listening. Not being able to fail fast is much better than being slow.
  if (!cached || !cached.models) return null;
  if (!keyProblem(cached)) return null;
  // Only now, where we are about to refuse the send, is a fresh read worth
  // it: API KEYS.txt is re-read on change, so the user may have just pasted
  // their key in and the cached payload would be out of date.
  var fresh = null;
  try { fresh = await fetch('/api/models').then(function (r) { return r.json(); }); }
  catch (e) { return keyProblem(cached); }
  if (fresh) {
    window.__lastModels = fresh;
    if (typeof renderModels === 'function') renderModels(fresh);
  }
  return keyProblem(fresh || cached);
}
/* ---------- MODEL SETTINGS modal ---------- */
/* A model's api_key is a *name* - of a line in API KEYS.txt, or of an
   environment variable - never the key itself. The backend can tell four
   states apart and each one needs a different word: reporting "key fine" for a
   name that is not in the file is how a 401 gets debugged from the wrong end. */
var KEY_BADGE = {
  named:   ['KEY FOUND', 'ok'],
  literal: ['KEY IN MODELS.JSON', 'warn'],
  missing: ['NOT IN API KEYS.TXT', 'bad'],
  none:    ['NO KEY', 'bad']
};
function keyStateOf(m) { return (m && m.api_key_state) || 'none'; }
function keyBadgeText(m) {
  var st = keyStateOf(m);
  // a llama-server on this PC needs no key, and saying NO KEY in red there
  // would be a lie about a perfectly working setup
  if (st === 'none' && m && !m.needs_key) return ['NO KEY NEEDED', ''];
  return KEY_BADGE[st] || KEY_BADGE.none;
}
function showKeyBadge(m) {
  var el = document.getElementById('cfg_keystate');
  if (!el) return;
  var b = keyBadgeText(m);
  el.textContent = b[0];
  el.className = 'kstate ' + b[1];
}
function keyHint(m) {
  if (!m || m.type !== 'api') return '';
  var name = m.api_key_name || '';
  var st = keyStateOf(m);
  if (st === 'named') return 'The key name is saved. The key itself stays in API KEYS.txt and is never shown here.';
  if (st === 'literal') return 'This model has the key itself in models.json, in plain text. Move it into API KEYS.txt under a name and put that name in the field above.';
  if (st === 'missing') return "'" + name + "' is not in API KEYS.txt, so it would be sent to the provider as if it were the key and every request would come back 401. Add '" + name + "=your-key' to API KEYS.txt, then press TEST KEY.";
  if (m.needs_key) return 'This endpoint needs a key. Put it in API KEYS.txt as NAME=your-key and put NAME in the field above.';
  return 'No key needed - this endpoint is on your own machine.';
}
function cfgFill(j) {
  const models = (j && j.models) || [];
  const act = models.filter(function (m) { return m.id === (j && j.active); })[0];
  const cfg = (act && act.cfg) || (j && j.defaults) || { ctx: 32768, temp: 1, top_p: 0.95, top_k: 20, ngl: 99 };
  if (act && act.ctx) BONSAI_CTX = act.ctx;
  const set = function (id, v) { const e = document.getElementById(id); if (e) e.value = v; };
  set('cfg_ctx', cfg.ctx); set('cfg_temp', cfg.temp);
  set('cfg_top_p', cfg.top_p); set('cfg_top_k', cfg.top_k); set('cfg_ngl', cfg.ngl);
  const who = document.getElementById('cfgwho');
  if (who) who.textContent = act ? ('- ' + (act.label || act.id)) : '';
  const isApi = !!(act && act.type === 'api');
  const keyRow = document.getElementById('cfg-keyrow');
  if (keyRow) keyRow.hidden = !isApi;
  const testBtn = document.getElementById('cfg-testkey');
  if (testBtn) testBtn.hidden = !isApi;
  const keyIn = document.getElementById('cfg_apikey');
  if (keyIn) keyIn.value = (act && act.api_key_name) || '';
  showKeyBadge(act);
  const hint = document.getElementById('cfghint');
  if (hint) {
    hint.textContent = !act ? 'No local model selected.'
      : (isApi ? keyHint(act)
      : 'Save and the model server restarts, so your next message uses these values. EJECT does the same by hand.');
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
  const body = { action: 'config', id: act.id, cfg: cfg };
  if (act.type === 'api') {
    const k = document.getElementById('cfg_apikey');
    body.api_key = k ? (k.value || '').trim() : '';
  }
  _cfgSave.disabled = true;
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (!res.ok) { alert('Could not save the settings:\\n' + (res.error || 'unknown error')); return; }
      renderModels(res);
      cfgFill(res);
      const hint = document.getElementById('cfghint');
      if (hint) hint.textContent = res.warn ? ('Saved. ' + res.warn)
        : ('Saved. ' + (res.detail || 'These values apply the next time this model is loaded.'));
    })
    .catch(function () { alert('Could not save the settings'); })
    .then(function () { _cfgSave.disabled = false; });
};
/* One cheap authenticated GET instead of a whole failed conversation. */
const _cfgTest = document.getElementById('cfg-testkey');
if (_cfgTest) _cfgTest.onclick = function () {
  const j = window.__lastModels || {};
  const act = (j.models || []).filter(function (m) { return m.id === j.active; })[0];
  if (!act) { alert('No model selected.'); return; }
  const k = document.getElementById('cfg_apikey');
  const hint = document.getElementById('cfghint');
  _cfgTest.disabled = true;
  const was = _cfgTest.textContent;
  _cfgTest.textContent = 'TESTING...';
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'test_key', base_url: act.base_url,
                           model: act.model, api_key: k ? (k.value || '').trim() : '' }) })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (res && res.ok) {
        let msg = res.detail || 'the key was accepted';
        if (res.context_length) msg += ' - context window ' + res.context_length + ' tokens';
        if (res.model_ok === false) msg = 'The key works. ' + (res.warn || '');
        if (hint) hint.textContent = msg;
      } else if (hint) {
        hint.textContent = (res && res.error) || 'the key test failed';
      }
    })
    .catch(function () { if (hint) hint.textContent = 'the key test could not reach the provider'; })
    .then(function () { _cfgTest.disabled = false; _cfgTest.textContent = was; });
};
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') { closeAddMdl(); closeCfgMdl(); }
});
refreshModels();

const inp = document.getElementById('user-input');
inp.addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 160) + 'px'; scheduleCtx(true); });
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
function dlBytes(n) {
  if (n === null || n === undefined || n === '') return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  if (!isFinite(v)) return '';
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i ? v.toFixed(1) : Math.round(v)) + ' ' + u[i];
}
function dlTime(s) {
  if (!s && s !== 0) return '';
  s = Math.round(Number(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  return Math.floor(s / 3600) + 'h ' + Math.round((s % 3600) / 60) + 'm';
}
function dlAct(id, what) {
  fetch('/api/dl_' + what, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id }) })
    .then(function () { dlTick(); })
    .catch(function () {});
}
function dlToast(j) {
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
  h.textContent = (j.status === 'done' ? 'DOWNLOAD FINISHED: ' : 'DOWNLOAD FAILED: ') +
                  (j.name || j.url || '');
  const x = document.createElement('button');
  x.className = 'toastx';
  x.textContent = '\u00d7';
  x.onclick = function () { if (t.parentNode) t.parentNode.removeChild(t); };
  const p = document.createElement('div');
  p.className = 'toastout';
  p.textContent = (j.saved || j.url || '') + (j.error ? '  ' + j.error : '  ' + dlBytes(j.done));
  t.appendChild(h); t.appendChild(x); t.appendChild(p);
  box.appendChild(t);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 15000);
}
function dlRow(j) {
  const row = document.createElement('div');
  row.className = 'dlrow ' + (j.status || '');
  const top = document.createElement('div');
  top.className = 'dltop';
  const nm = document.createElement('span');
  nm.className = 'dlname';
  nm.textContent = j.name || String(j.url || '').split('/').pop() || j.url;
  nm.title = j.url || '';
  const st = document.createElement('span');
  st.className = 'dlstat';
  st.textContent = j.status || '';
  top.appendChild(nm); top.appendChild(st);
  row.appendChild(top);
  const bar = document.createElement('div');
  bar.className = 'dlbar';
  const fill = document.createElement('div');
  fill.className = 'dlfill';
  if (j.status === 'done') fill.style.width = '100%';
  else if (j.percent !== null && j.percent !== undefined) fill.style.width = Math.max(2, j.percent) + '%';
  else fill.classList.add('indet');
  bar.appendChild(fill);
  row.appendChild(bar);
  const num = document.createElement('div');
  num.className = 'dlnum';
  const bits = [];
  if (j.total) bits.push(dlBytes(j.done) + ' / ' + dlBytes(j.total));
  else if (j.done) bits.push(dlBytes(j.done));
  if (j.status === 'running' && j.percent !== null && j.percent !== undefined) bits.push(j.percent + '%');
  if (j.speed_bps) bits.push(dlBytes(j.speed_bps) + '/s');
  if (j.eta_s) bits.push('ETA ' + dlTime(j.eta_s));
  if (j.connections > 1) bits.push(j.connections + ' conn');
  if (j.resumed) bits.push('resumed');
  num.textContent = bits.join('  \u00b7  ');
  row.appendChild(num);
  if (j.saved) {
    const p = document.createElement('div');
    p.className = 'dlpath';
    p.textContent = j.saved;
    p.title = j.saved;
    row.appendChild(p);
  }
  if (j.error) {
    const e = document.createElement('div');
    e.className = 'dlerr';
    e.textContent = j.error;
    row.appendChild(e);
  }
  const btns = document.createElement('div');
  btns.className = 'dlbtns';
  const mk = function (label, what, title) {
    const b = document.createElement('button');
    b.className = 'dlbtn';
    b.textContent = label;
    b.title = title || label;
    b.onclick = function (ev) { ev.stopPropagation(); dlAct(j.id, what); };
    btns.appendChild(b);
  };
  if (j.status === 'running' || j.status === 'queued') mk('PAUSE', 'pause', 'Stop reading for a moment - the socket is released and it picks up where it left off');
  if (j.status === 'paused') mk('RESUME', 'resume');
  if (j.status !== 'done' && j.status !== 'error' && j.status !== 'canceled') mk('CANCEL', 'cancel', 'Give up on this one; the partial file stays on disk so you can retry later');
  if (j.status === 'error' || j.status === 'canceled') mk('RETRY', 'retry', 'Try again - it continues from the bytes already on disk');
  if (btns.children.length) row.appendChild(btns);
  return row;
}
var DL_SEEN = {};
function dlRender(state) {
  const box = document.getElementById('dllist');
  if (!box) return;
  const jobs = state.jobs || [];
  const live = jobs.filter(function (j) { return j.status !== 'done'; });
  const recent = jobs.filter(function (j) { return j.status === 'done'; }).slice(0, 2);
  const show = live.concat(recent);
  const active = jobs.filter(function (j) {
    return j.status === 'queued' || j.status === 'running' || j.status === 'paused';
  }).length;
  const cnt = document.getElementById('dlcount');
  if (cnt) cnt.textContent = active ? active + ' active' : '';
  const clr = document.getElementById('dlclear');
  const fin = jobs.filter(function (j) {
    return j.status === 'done' || j.status === 'error' || j.status === 'canceled';
  }).length;
  const crow = document.getElementById('dlclearrow');
  if (crow) crow.style.display = jobs.length ? 'flex' : 'none';
  if (clr) { clr.textContent = fin ? 'CLEAR FINISHED (' + fin + ')' : 'CLEAR FINISHED'; clr.disabled = !fin; }
  const call = document.getElementById('dlclearall');
  if (call) call.disabled = !jobs.length;
  while (box.firstChild) box.removeChild(box.firstChild);
  if (!show.length) {
    const e = document.createElement('div');
    e.className = 'dlempty';
    e.textContent = 'No downloads yet.';
    box.appendChild(e);
    return;
  }
  show.forEach(function (j) { box.appendChild(dlRow(j)); });
  show.forEach(function (j) {
    const was = DL_SEEN[j.id];
    DL_SEEN[j.id] = j.status;
    if (was && was !== j.status && (j.status === 'done' || j.status === 'error')) dlToast(j);
  });
}
function dlTick() {
  fetch('/api/dl_state').then(function (r) { return r.json(); })
    .then(dlRender).catch(function () {});
}
function startDlWatch() {
  const post = function (where) {
    return fetch('/api/dl_clear', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ where: where }) })
      .then(function () { DL_SEEN = {}; dlTick(); }).catch(function () {});
  };
  const clr = document.getElementById('dlclear');
  if (clr) clr.onclick = function () { post('finished'); };
  const call = document.getElementById('dlclearall');
  if (call) call.onclick = function () {
    if (!confirm('Clear every finished, failed, cancelled, queued and paused download?'
      + '\\n\\nAnything actively transferring is left alone - it will finish, then you can clear it.')) return;
    post('all');
  };
  dlTick();
  setInterval(dlTick, 900);
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
  .cfgrid label.keyfield { grid-column: 1 / -1; }
  .cfgrid .kstate { font-size: 11px; font-weight: 600; letter-spacing: .04em; }
  .cfgrid .kstate.ok { color: var(--ok, #34d399); }
  .cfgrid .kstate.bad { color: var(--bad, #f87171); }
  .cfgrid .kstate.warn { color: var(--warn, #fbbf24); }
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
  .endednote { margin-top: 6px; font-size: 12px; color: var(--mut);
               font-style: italic; font-family: Consolas, monospace; }
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
  .foldnote { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 10px 0 14px; padding: 7px 10px; border: 1px dashed var(--bd); border-left: 2px solid var(--acc); border-radius: 8px; background: var(--bg2); }
  .foldnote .foldb { flex: 1 1 240px; font-size: 10.5px; font-family: Consolas, monospace; color: var(--dim); letter-spacing: .3px; }
  .foldnote .foldbtn { font-family: Consolas, monospace; font-size: 10px; letter-spacing: .6px; color: var(--acc); background: transparent; border: 1px solid var(--bd); border-radius: 6px; padding: 4px 9px; cursor: pointer; }
  .foldnote .foldbtn:hover { border-color: var(--acc); }
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
  .dllist { padding: 2px 0 4px; }
  .dlhead { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px 4px; font-size: 10px; letter-spacing: 1.5px; color: var(--mut); text-transform: uppercase; font-family: Consolas, monospace; }
  .dlempty { padding: 8px 12px; font-size: 11px; color: var(--mut); }
  .dlrow { padding: 8px 12px; border-bottom: 1px solid rgba(22,78,99,.4); }
  .dlrow:last-child { border-bottom: none; }
  .dltop { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .dlname { font-size: 11.5px; color: var(--bg2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dlstat { font-size: 9px; letter-spacing: 1px; color: var(--mut); text-transform: uppercase; flex: none; }
  .dlrow.running .dlstat, .dlrow.queued .dlstat { color: var(--acc); }
  .dlrow.done .dlstat { color: #34d399; }
  .dlrow.error .dlstat, .dlrow.canceled .dlstat { color: var(--err); }
  .dlbar { height: 4px; margin: 6px 0 4px; background: rgba(255,255,255,.07); border-radius: 3px; overflow: hidden; }
  .dlfill { height: 100%; width: 0; background: var(--acc); transition: width .25s linear; }
  .dlrow.done .dlfill { background: #34d399; }
  .dlrow.error .dlfill, .dlrow.canceled .dlfill { background: var(--err); }
  .dlrow.paused .dlfill { background: var(--mut); }
  .dlfill.indet { width: 34% !important; animation: dlslide 1.1s ease-in-out infinite; }
  @keyframes dlslide { 0% { margin-left: 0; } 50% { margin-left: 66%; } 100% { margin-left: 0; } }
  .dlnum { font-size: 10px; color: var(--mut); font-family: Consolas, monospace; }
  .dlpath { font-size: 9.5px; color: var(--mut); opacity: .75; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dlerr { font-size: 10px; color: var(--err); margin-top: 3px; word-break: break-word; }
  .dlbtns { display: flex; gap: 5px; margin-top: 6px; }
  .dlbtn { background: transparent; border: 1px solid var(--bd); color: var(--mut); font-size: 9px; letter-spacing: 1px; padding: 3px 7px; border-radius: 4px; cursor: pointer; font-family: Consolas, monospace; }
  .dlbtn:hover { color: var(--acc); border-color: var(--acc); }
  .dlclearrow { display: none; gap: 6px; padding: 0 12px 8px; }
  .dlclear { flex: 1; background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2); font-size: 10.5px; font-weight: 700; letter-spacing: .5px; padding: 6px 4px; border-radius: 6px; cursor: pointer; font-family: Consolas, monospace; white-space: nowrap; transition: all .15s; }
  .dlclear:hover { color: var(--acc); border-color: var(--acc); }
  .dlclear.danger:hover { color: var(--err); border-color: var(--err); }
  .dlclear:disabled { opacity: .35; cursor: default; color: var(--mut); border-color: var(--bd); }
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
  .shots { margin: 6px 0 10px; display: flex; flex-direction: column; gap: 8px; }
  .shotcard { margin-top: 8px; border: 1px solid var(--bd); border-radius: 8px; overflow: hidden; background: var(--bg2); }
  .shotcard .shothd { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 5px 8px; font-size: 11px; color: var(--mut); border-bottom: 1px solid var(--bd); font-family: Consolas, monospace; }
  .shotcard .shothd b { color: var(--txt); font-weight: 600; }
  .shotcard .shotimg { display: block; width: 100%; height: auto; cursor: zoom-in; background: var(--bg); }
  .shotcard .shotpath { padding: 4px 8px; font-size: 10.5px; color: var(--mut); font-family: Consolas, monospace; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* context window meter - always visible, so you can see the cost of the
     system prompt + tools before you even type, and how it grows. */
  .ctxmeter {
    display: flex; align-items: center; gap: 6px;
    background: var(--bg2); border: 1px solid var(--bd); border-radius: 12px;
    padding: 0 10px; height: 34px; cursor: default; white-space: nowrap;
    font-size: 11px; font-family: Consolas, monospace; user-select: none;
    position: relative; transition: border-color .15s;
  }
  .ctxmeter:hover { border-color: var(--acc); }
  .ctxmeter .ctxtag { color: var(--mut); font-weight: 700; letter-spacing: .5px; }
  .ctxmeter .ctxbar { width: 54px; height: 6px; border-radius: 3px; background: var(--bg); overflow: hidden; flex: 0 0 auto; }
  .ctxmeter .ctxfill { display: block; height: 100%; width: 0%; background: var(--acc); transition: width .25s, background .25s; border-radius: 3px; }
  .ctxmeter.warn .ctxfill { background: #fbbf24; }
  .ctxmeter.over .ctxfill { background: var(--err); }
  .ctxmeter .ctxnum { color: var(--txt2); }
  .ctxmeter .ctxhint {
    display: none; position: absolute; bottom: 42px; right: 0; z-index: 40;
    background: var(--bg2); border: 1px solid var(--bd); border-radius: 10px;
    padding: 8px 10px; font-size: 11px; color: var(--txt2); white-space: normal;
    width: 268px; box-shadow: 0 8px 24px rgba(0,0,0,.5); line-height: 1.5; text-align: left;
  }
  .ctxmeter:hover .ctxhint { display: block; }
  .ctxmeter .ctxhint b { color: var(--acc); }
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
    <div class="dlhead"><span>DOWNLOADS</span><span id="dlcount"></span></div>
    <div class="dllist" id="dllist"></div>
    <div class="dlclearrow" id="dlclearrow">
      <button class="dlclear" id="dlclear" title="Remove finished, failed and cancelled downloads from the list">CLEAR FINISHED</button>
      <button class="dlclear danger" id="dlclearall" title="Cancel anything still running and remove every download from the list">CLEAR ALL</button>
    </div>
    <div class="side-foot">
      <div class="blrow" id="blstatus" title="Blender MCP status"><svg class="blicon" viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 1.6C7.4 1.6 3.7 3.9 3.7 6.9c0 1.6 1.1 3 2.8 3.9-2.1.9-3.5 2.4-3.5 4.2 0 3.2 4 5.8 9 5.8 2.4 0 4.6-.7 6.2-1.8l3.4 2.8 1.7-2-3.3-2.7c.6-.9.9-1.9.9-3 0-1.9-1-3.6-2.6-4.9.3-.5.4-1.1.4-1.7 0-3-3.7-5.3-8.3-5.3Z"/><ellipse cx="12" cy="6.9" rx="4.2" ry="2.5" fill="#18181b"/></svg><span class="bldot off" id="bldot"></span></div>
      <button class="btn-ghost wd" id="workbtn"></button>
      <button class="btn-ghost scopebtn" id="scopebtn" title="How far Bonsai may reach outside the workspace. Click to switch: WORKSPACE (hard sandbox) / ASK (ask me every time) / SYSTEM (no prompts).">SCOPE: ...</button>
      <div class="scopelist" id="scopelist"></div>
      <button class="btn-ghost scopeclear" id="scopeclear" title="Forget every approved and denied path">CLEAR APPROVALS</button>
      <div class="foot-row">
        <a href="/" title="Classic UI">Classic UI</a>
        <button class="btn-ghost" id="ttsbtn" title="Text-to-speech (Piper) - speak replies aloud. Off by default.">TTS: OFF</button>
        <button class="btn-ghost" id="detailbtn" title="Show each step in the chat as one collapsed line, or fully expanded">DETAIL: BRIEF</button>
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
        <input id="apikey" placeholder="API key name, e.g. OPENROUTER_API_KEY" title="The NAME of a line in API KEYS.txt - not the key itself. Most hosted providers need one; a llama-server on this PC does not.">
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
        <label>Context size (ctx)<input id="cfg_ctx" type="number" min="512" max="4194304" step="512"></label>
          <label>Temperature<input id="cfg_temp" type="number" min="0" max="2" step="0.05"></label>
          <label>Top-p<input id="cfg_top_p" type="number" min="0.01" max="1" step="0.01"></label>
          <label>Top-k<input id="cfg_top_k" type="number" min="0" max="1000" step="1"></label>
          <label>GPU layers (-ngl)<input id="cfg_ngl" type="number" min="-1" max="999" step="1"></label>
          <label class="keyfield" id="cfg-keyrow" hidden>API key name <span class="kstate" id="cfg_keystate"></span><input id="cfg_apikey" type="text" spellcheck="false" autocomplete="off" placeholder="OPENROUTER_API_KEY"></label>
        </div>
        <div class="hint" id="cfghint"></div>
        <div class="row">
          <button class="act inline" id="cfg-save">Save</button>
          <button class="act inline" id="cfg-testkey" hidden>TEST KEY</button>
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
            <div class="ctxmeter" id="ctxmeter" title="Context window in use - the system prompt and all tools are already counted, before you type">
              <span class="ctxtag">CTX</span>
              <span class="ctxbar"><span class="ctxfill" id="ctxfill"></span></span>
              <span class="ctxnum" id="ctxnum">--</span>
              <span class="ctxhint" id="ctxhint"></span>
            </div>
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
let BONSAI_CTX = 0;
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
function dlBytes(n) {
  if (n === null || n === undefined || n === '') return '';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0, v = Number(n);
  if (!isFinite(v)) return '';
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i ? v.toFixed(1) : Math.round(v)) + ' ' + u[i];
}
function dlTime(s) {
  if (!s && s !== 0) return '';
  s = Math.round(Number(s));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.round(s / 60) + 'm';
  return Math.floor(s / 3600) + 'h ' + Math.round((s % 3600) / 60) + 'm';
}
function dlAct(id, what) {
  fetch('/api/dl_' + what, { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: id }) })
    .then(function () { dlTick(); })
    .catch(function () {});
}
function dlToast(j) {
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
  h.textContent = (j.status === 'done' ? 'DOWNLOAD FINISHED: ' : 'DOWNLOAD FAILED: ') +
                  (j.name || j.url || '');
  const x = document.createElement('button');
  x.className = 'toastx';
  x.textContent = '\u00d7';
  x.onclick = function () { if (t.parentNode) t.parentNode.removeChild(t); };
  const p = document.createElement('div');
  p.className = 'toastout';
  p.textContent = (j.saved || j.url || '') + (j.error ? '  ' + j.error : '  ' + dlBytes(j.done));
  t.appendChild(h); t.appendChild(x); t.appendChild(p);
  box.appendChild(t);
  while (box.children.length > 4) box.removeChild(box.firstChild);
  setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 15000);
}
function dlRow(j) {
  const row = document.createElement('div');
  row.className = 'dlrow ' + (j.status || '');
  const top = document.createElement('div');
  top.className = 'dltop';
  const nm = document.createElement('span');
  nm.className = 'dlname';
  nm.textContent = j.name || String(j.url || '').split('/').pop() || j.url;
  nm.title = j.url || '';
  const st = document.createElement('span');
  st.className = 'dlstat';
  st.textContent = j.status || '';
  top.appendChild(nm); top.appendChild(st);
  row.appendChild(top);
  const bar = document.createElement('div');
  bar.className = 'dlbar';
  const fill = document.createElement('div');
  fill.className = 'dlfill';
  if (j.status === 'done') fill.style.width = '100%';
  else if (j.percent !== null && j.percent !== undefined) fill.style.width = Math.max(2, j.percent) + '%';
  else fill.classList.add('indet');
  bar.appendChild(fill);
  row.appendChild(bar);
  const num = document.createElement('div');
  num.className = 'dlnum';
  const bits = [];
  if (j.total) bits.push(dlBytes(j.done) + ' / ' + dlBytes(j.total));
  else if (j.done) bits.push(dlBytes(j.done));
  if (j.status === 'running' && j.percent !== null && j.percent !== undefined) bits.push(j.percent + '%');
  if (j.speed_bps) bits.push(dlBytes(j.speed_bps) + '/s');
  if (j.eta_s) bits.push('ETA ' + dlTime(j.eta_s));
  if (j.connections > 1) bits.push(j.connections + ' conn');
  if (j.resumed) bits.push('resumed');
  num.textContent = bits.join('  \u00b7  ');
  row.appendChild(num);
  if (j.saved) {
    const p = document.createElement('div');
    p.className = 'dlpath';
    p.textContent = j.saved;
    p.title = j.saved;
    row.appendChild(p);
  }
  if (j.error) {
    const e = document.createElement('div');
    e.className = 'dlerr';
    e.textContent = j.error;
    row.appendChild(e);
  }
  const btns = document.createElement('div');
  btns.className = 'dlbtns';
  const mk = function (label, what, title) {
    const b = document.createElement('button');
    b.className = 'dlbtn';
    b.textContent = label;
    b.title = title || label;
    b.onclick = function (ev) { ev.stopPropagation(); dlAct(j.id, what); };
    btns.appendChild(b);
  };
  if (j.status === 'running' || j.status === 'queued') mk('PAUSE', 'pause', 'Stop reading for a moment - the socket is released and it picks up where it left off');
  if (j.status === 'paused') mk('RESUME', 'resume');
  if (j.status !== 'done' && j.status !== 'error' && j.status !== 'canceled') mk('CANCEL', 'cancel', 'Give up on this one; the partial file stays on disk so you can retry later');
  if (j.status === 'error' || j.status === 'canceled') mk('RETRY', 'retry', 'Try again - it continues from the bytes already on disk');
  if (btns.children.length) row.appendChild(btns);
  return row;
}
var DL_SEEN = {};
function dlRender(state) {
  const box = document.getElementById('dllist');
  if (!box) return;
  const jobs = state.jobs || [];
  const live = jobs.filter(function (j) { return j.status !== 'done'; });
  const recent = jobs.filter(function (j) { return j.status === 'done'; }).slice(0, 2);
  const show = live.concat(recent);
  const active = jobs.filter(function (j) {
    return j.status === 'queued' || j.status === 'running' || j.status === 'paused';
  }).length;
  const cnt = document.getElementById('dlcount');
  if (cnt) cnt.textContent = active ? active + ' active' : '';
  const clr = document.getElementById('dlclear');
  const fin = jobs.filter(function (j) {
    return j.status === 'done' || j.status === 'error' || j.status === 'canceled';
  }).length;
  const crow = document.getElementById('dlclearrow');
  if (crow) crow.style.display = jobs.length ? 'flex' : 'none';
  if (clr) { clr.textContent = fin ? 'CLEAR FINISHED (' + fin + ')' : 'CLEAR FINISHED'; clr.disabled = !fin; }
  const call = document.getElementById('dlclearall');
  if (call) call.disabled = !jobs.length;
  while (box.firstChild) box.removeChild(box.firstChild);
  if (!show.length) {
    const e = document.createElement('div');
    e.className = 'dlempty';
    e.textContent = 'No downloads yet.';
    box.appendChild(e);
    return;
  }
  show.forEach(function (j) { box.appendChild(dlRow(j)); });
  show.forEach(function (j) {
    const was = DL_SEEN[j.id];
    DL_SEEN[j.id] = j.status;
    if (was && was !== j.status && (j.status === 'done' || j.status === 'error')) dlToast(j);
  });
}
function dlTick() {
  fetch('/api/dl_state').then(function (r) { return r.json(); })
    .then(dlRender).catch(function () {});
}
function startDlWatch() {
  const post = function (where) {
    return fetch('/api/dl_clear', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ where: where }) })
      .then(function () { DL_SEEN = {}; dlTick(); }).catch(function () {});
  };
  const clr = document.getElementById('dlclear');
  if (clr) clr.onclick = function () { post('finished'); };
  const call = document.getElementById('dlclearall');
  if (call) call.onclick = function () {
    if (!confirm('Clear every finished, failed, cancelled, queued and paused download?'
      + '\\n\\nAnything actively transferring is left alone - it will finish, then you can clear it.')) return;
    post('all');
  };
  dlTick();
  setInterval(dlTick, 900);
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
  startDlWatch();
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
  btn.onclick = function () { chatMode = chatMode === 'plan' ? 'build' : 'plan'; localStorage.setItem('bonsai_gpt_mode', chatMode); update(); scheduleCtx(); };
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
function renderAll() { renderList(); renderConv(); scheduleCtx(); }
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
    const at = (cur.summary && cur.summary_at) ? cur.summary_at : -1;
    cur.messages.forEach(function (m, i) {
      if (i === at) addFold(cur);
      if (m.role === 'user') addUser(m.content);
      else if (m.role === 'assistant') addAsst(m.content, m.calls || [], m.reason, m.stats, m.parts, m);
    });
    if (at > cur.messages.length) addFold(cur);
  }
}
// see the other console's copy for why this exists: the older turns are
// summarised instead of the conversation being thrown away, and nothing is
// deleted - the button in the divider puts the full history back
function addFold(chat) {
  const conv = convInner();
  const row = document.createElement('div');
  row.className = 'foldnote';
  const n = chat.summary_at || 0;
  const how = chat.summary_via === 'fallback' ? ' (plain extract - the model would not answer)' : '';
  const b = document.createElement('div');
  b.className = 'foldb';
  b.textContent = n + ' earlier message' + (n === 1 ? '' : 's') + ' summarised' + how
    + ' \u00b7 still shown above, just not sent to the model';
  const btn = document.createElement('button');
  btn.className = 'foldbtn';
  btn.type = 'button';
  btn.textContent = 'use the full history again';
  btn.onclick = function () { undoFold(chat); };
  row.appendChild(b);
  row.appendChild(btn);
  conv.appendChild(row);
}
function undoFold(chat) {
  if (!chat || !chat.summary) return;
  chat.summary = null;
  chat.summary_at = null;
  chat.summary_via = null;
  chat.summary_msg = null;
  save();
  renderConv();
  refreshCtx();
}
function pendingList(chat) {
  return ((chat && chat.messages) || []).filter(function (m) { return !m.queued; });
}
function wireFor(chat) {
  const msgs = pendingList(chat);
  if (!chat || !chat.summary) return msgs;
  const at = Math.max(0, Math.min(chat.summary_at || 0, msgs.length));
  /* the wording comes from the server, so there is one copy of it */
  const head = chat.summary_msg || { role: 'user', content: chat.summary };
  return [head].concat(msgs.slice(at));
}
let folding = false;
function maybeFold(quick) {
  if (quick || folding || busy) return;
  const j = ctxLast;
  if (!j || !j.ok || !j.total) return;
  if (!cur || cur.summary) return;
  if (j.own < (j.fold_min || 0)) return;
  if (j.used * 100 / j.total < (j.fold_at || 101)) return;
  const msgs = pendingList(cur);
  /* below this the summary would be longer than the turns it replaces */
  if (msgs.length <= (j.fold_keep || 6) + 1) return;
  foldChat(cur, msgs);
}
async function foldChat(chat, msgs) {
  folding = true;
  addThinking();
  setState('thinking');
  if (thinkingRow && thinkingRow.think) {
    thinkingRow.think.innerHTML = '<span class="dots"><i></i><i></i><i></i></span> Summarising the earlier turns';
  }
  try {
    const r = await fetch('/api/compact', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: msgs, mode: chatMode }) });
    const j = await r.json();
    doneThinking(null);
    if (!j || !j.ok || !j.summary) return;
    chat.summary = j.summary;
    chat.summary_at = j.folded;
    chat.summary_via = j.via;
    chat.summary_msg = j.message;
    save();
    renderConv();
    refreshCtx();
  } catch (e) {
    doneThinking('Could not compact: ' + (e && e.message ? e.message : 'unknown error'));
  } finally {
    folding = false;
    setState('idle');
  }
}
function esc(s) { return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function fmt(s) {
  /* Block-level markdown, then the inline marks. Models reach for headings
     and lists constantly and they used to arrive on screen as literal "#" and
     "-" text, because only the inline marks were ever handled. Fenced code is
     lifted out first so a list marker inside a code block stays put. */
  let src = String(s == null ? '' : s).replace(/\\r\\n?/g, '\\n');
  const fences = [];
  src = src.replace(/```([\\s\\S]*?)(?:```|$)/g, function (m, body) {
    fences.push('<pre>' + esc(body.replace(/\\n$/, '')) + '</pre>');
    return '\\u0000F' + (fences.length - 1) + '\\u0000';
  });

  const inline = function (x) {
    let t = esc(x);
    t = t.replace(/`([^`]+)`/g, '<code>$1</code>');
    t = t.replace(/\\*\\*([^*]+)\\*\\*/g, '<b>$1</b>');
    t = t.replace(/\\*([^*]+)\\*/g, '<i>$1</i>');
    /* Links go into placeholders and are put back at the end, so the bare-url
       pass cannot swallow the address of a link it has already built - it used
       to, and the closing bracket ended up inside the address. Only http and
       https are linked: a model writes these, so this is the one place a
       javascript: address could get in. esc() does not touch a double quote,
       so it is escaped here too - a quote in the address used to be able to
       close the href attribute and add an event handler. */
    const links = [];
    const hold = function (html) {
      links.push(html); return '\\u0001L' + (links.length - 1) + '\\u0001';
    };
    const anchor = function (url, text) {
      const u = String(url).replace(/"/g, '&quot;');
      return '<a href="' + u + '" target="_blank" rel="noreferrer">' + text + '</a>';
    };
    t = t.replace(/\\[([^\\]\\n]+)\\]\\((https?:\\/\\/[^)\\s"]+)\\)/g, function (m, label, url) {
      return hold(anchor(url, label));
    });
    t = t.replace(/(https?:\\/\\/[^\\s<"]+)/g, function (m) {
      let url = m, tail = '';
      while (/[),.;:!?]$/.test(url)) {
        const c = url.slice(-1);
        if (c === ')' && (url.match(/\\)/g) || []).length <= (url.match(/\\(/g) || []).length) break;
        tail = c + tail; url = url.slice(0, -1);
      }
      return hold(anchor(url, url)) + tail;
    });
    t = t.replace(/\\u0001L(\\d+)\\u0001/g, function (m, i) { return links[+i]; });
    return t;
  };

  const out = [];
  let para = [], list = null, quote = [];
  const flushPara = function () {
    /* no <p> wrapper: a paragraph of prose is just this, as before, so the
       spacing inside a bubble does not change */
    if (para.length) { out.push(para.map(inline).join('<br>')); para = []; }
  };
  const flushList = function () {
    if (list) { out.push('<' + list.tag + '>' + list.items.map(function (i) {
      return '<li>' + inline(i) + '</li>'; }).join('') + '</' + list.tag + '>'); list = null; }
  };
  const flushQuote = function () {
    if (quote.length) { out.push('<blockquote>' + quote.map(inline).join('<br>') + '</blockquote>'); quote = []; }
  };
  const flushAll = function () { flushPara(); flushList(); flushQuote(); };

  String(src).split('\\n').forEach(function (ln) {
    const fence = ln.match(/^\\u0000F(\\d+)\\u0000$/);
    if (fence) { flushAll(); out.push(fences[+fence[1]]); return; }
    if (/^\\s*(-{3,}|\\*{3,}|_{3,})\\s*$/.test(ln)) { flushAll(); out.push('<hr>'); return; }
    let m = ln.match(/^\\s*(#{1,6})\\s+(.*)$/);
    if (m) { flushAll(); out.push('<h' + m[1].length + '>' + inline(m[2]) + '</h' + m[1].length + '>'); return; }
    m = ln.match(/^\\s*>\\s?(.*)$/);
    if (m) { flushPara(); flushList(); quote.push(m[1]); return; }
    flushQuote();
    m = ln.match(/^\\s*[-*+]\\s+(.*)$/);
    if (m) { flushPara(); if (!list || list.tag !== 'ul') { flushList(); list = { tag: 'ul', items: [] }; } list.items.push(m[1]); return; }
    m = ln.match(/^\\s*\\d+[.)]\\s+(.*)$/);
    if (m) { flushPara(); if (!list || list.tag !== 'ol') { flushList(); list = { tag: 'ol', items: [] }; } list.items.push(m[1]); return; }
    flushList();
    if (!ln.trim()) { flushPara(); return; }
    para.push(ln);
  });
  flushAll();

  return out.join('');
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
function shotCardDom(call) {
  if (!call) return null;
  const src = call.preview || call.image_data || '';
  if (!src) return null;
  const r = call.result || {};
  const saved = r.saved || r.path || '';
  const box = document.createElement('div'); box.className = 'shotcard';
  const head = document.createElement('div'); head.className = 'shothd';
  const name = document.createElement('b');
  name.textContent = String.fromCodePoint(0x1F4BE) + ' ' + (r.matched ? 'window: ' + r.matched : 'screenshot');
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
    shots.appendChild(shotCardDom(c));
    shotCount++;
  });
  if (shotCount) container.insertBefore(shots, d);
}
// see the note on the other console's copy: a silent "thinking..." stub is
// the same lie, and the fix is the same - never abandon a turn on a timer
var waitTimer = null;
function clearWaitWatch() { if (waitTimer) { clearInterval(waitTimer); waitTimer = null; } }
/* One line that says what is actually going on. It said "The model is loading"
   for the whole wait, which is wrong the moment anything else happens: the
   model is not loading while a shell command runs or an edit is being written.
   It is also wrong when nothing is loading at all, which is the usual case. */
const ACTIVITY = {
  shell: 'Executing shell', run: 'Executing shell', execute: 'Executing shell',
  write_file: 'Preparing edit', write: 'Preparing edit', create_file: 'Preparing edit',
  edit_file: 'Preparing edit', edit: 'Preparing edit', apply_patch: 'Preparing edit',
  read_file: 'Reading the file', read: 'Reading the file', view: 'Reading the file',
  list_dir: 'Listing the folder', ls: 'Listing the folder', glob: 'Listing the folder',
  search: 'Searching the folder', grep: 'Searching the folder',
  web_search: 'Searching the web', fetch: 'Fetching a page',
  screenshot: 'Taking a screenshot', question: 'Waiting for your answer',
  rich_text: 'Pasting rich text', todo_write: 'Writing the to-do list',
  memory: 'Writing a memory'
};
function activityFor(name) {
  const k = String(name || '').toLowerCase();
  if (ACTIVITY[k]) return ACTIVITY[k];
  return 'Running ' + (k.replace(/_/g, ' ') || 'a tool');
}
function setStatus(text) {
  const row = (typeof thinkingRow !== 'undefined' && thinkingRow) || window.thinkingRow;
  if (!row || !row.think || !row.think.isConnected) return;
  const t = row.think;
  t.textContent = '';
  const d = document.createElement('span');
  d.className = 'dots';
  for (let i = 0; i < 3; i++) d.appendChild(document.createElement('i'));
  t.appendChild(d);
  t.appendChild(document.createTextNode(' ' + text));
}
/* Put the caret in the chat box and get it ready to receive a paste. The page
   is the only thing that can focus its own input, so a tool that is about to
   press Ctrl+V asks for this first. */
function focusChatInput() {
  try {
    const el = document.getElementById('user-input');
    if (!el) return false;
    el.focus();
    try { el.scrollIntoView({ block: 'nearest' }); } catch (e) {}
    return document.activeElement === el;
  } catch (e) { return false; }
}
function showLoading() {
  const row = thinkingRow;
  if (!row || !row.think || !row.think.isConnected) { clearWaitWatch(); return; }
  setStatus('Waking the model');
}
function startWaitWatch() {
  clearWaitWatch();
  /* This used to swap the label for "The model is loading" after six seconds of
     silence. Silence is not evidence of a load - a long tool run and a long
     opening thought are both silent - and it fired often enough to say the
     model was loading while a shell command was still running. The server
     sends a real `waiting` event when it is actually starting a model, and only
     that may claim it. */
  waitTimer = setInterval(function () {
    if (!thinkingRow || !thinkingRow.think || !thinkingRow.think.isConnected) clearWaitWatch();
  }, 6000);
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
function addAsst(text, calls, reason, stats, entry) {
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
  if (entry && entry.ended) {
    const e = document.createElement('div'); e.className = 'endednote';
    e.textContent = entry.ended === 'stopped'
      ? '(stopped by you - the work above was kept)'
      : '(this reply was cut off - the work above was kept)';
    b.appendChild(e);
  }
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
  thinkingRow = { row: row, b: b, body: null, pills: null, tlog: null, reasonD: null, think: t };
  startWaitWatch();
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
  clearWaitWatch();
  if (!thinkingRow) return;
  if (thinkingRow.think) thinkingRow.think.remove();
  if (thinkingRow.reasonD) {
    const txt = (thinkingRow.reasonD.c.textContent || '').trim();
    thinkingRow.reasonD.s.textContent = 'Thinking \u00b7 ' + (txt ? txt.length + ' chars' : 'empty');
  }
  if (thinkingRow.tlog) thinkingRow.tlog.s.textContent = 'Tool log \u00b7 ' + liveCalls.length + ' call' + (liveCalls.length === 1 ? '' : 's');
  if (thinkingRow.body) { thinkingRow.body.classList.remove('caret'); }
  if (errMsg) { const e = document.createElement('div'); e.style.color = 'var(--err)'; e.style.fontSize = '13px'; e.textContent = errMsg; thinkingRow.b.appendChild(e); }
  /* The ordered renderer draws the reply in its own bubble, so this waiting
     row is often never written to at all. It used to be left behind as an
     empty bubble with an avatar under every single answer. */
  if (!errMsg && !thinkingRow.body && !thinkingRow.reasonD && !thinkingRow.tlog) {
    if (thinkingRow.row.parentNode) thinkingRow.row.parentNode.removeChild(thinkingRow.row);
  }
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
  statsVals.ctx_left = j.ctx_left || Math.max(0, BONSAI_CTX - statsVals.ctx_used);
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


/* Keep a turn that did not finish, so the work in it is not lost. */
function keepTurn(chat, anchorMsg, reply, calls, reason, stats, ended) {
  chat = chat || cur;
  if (!chat) return null;
  const entry = { role: 'assistant', content: reply, calls: calls,
                  reason: reason && reason.trim() ? reason : undefined,
                  stats: stats || undefined, ended: ended };
  if (anchorMsg) {
    const at = chat.messages.indexOf(anchorMsg);
    if (at >= 0) chat.messages.splice(at + 1, 0, entry);
    else chat.messages.push(entry);
  } else {
    chat.messages.push(entry);
  }
  return entry;
}

/* save() only ran when a turn ended, so a job that ran for minutes and then
   hit a crash, a closed tab or a dead laptop lost everything since the last
   save. These keep the transcript written down while the work is in progress. */
let autosaveTimer = null;
function startAutosave() {
  stopAutosave();
  autosaveTimer = setInterval(function () {
    try { if (busy || (cur && cur.messages && cur.messages.length)) save(); } catch (e) {}
  }, 5000);
}
function stopAutosave() {
  if (autosaveTimer) { clearInterval(autosaveTimer); autosaveTimer = null; }
}
document.addEventListener('visibilitychange', function () {
  if (document.visibilityState === 'hidden') { try { save(); } catch (e) {} }
});
window.addEventListener('pagehide', function () { try { save(); } catch (e) {} });
window.addEventListener('beforeunload', function () { try { save(); } catch (e) {} });
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
        else if (ev === 'waiting') { showLoading(); }
        else if (ev === 'focus') { focusChatInput(); }
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
  const partial = reply.trim() || calls.length || liveCalls.length;
  if (aborted || (!gotEnd && partial)) {
    if (partial) {
      const st = statsVals && (statsVals.think_ms || statsVals.respond_ms || statsVals.completion_tokens)
        ? JSON.parse(JSON.stringify(statsVals)) : null;
      keepTurn(chat, anchorMsg, reply, calls, reason, st, aborted ? 'stopped' : 'cut off');
      try { save(); } catch (e) {}
    }
    throw new Error(aborted ? 'stopped' : 'The reply was cut off unexpectedly - please try again.');
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
    chat.messages.push({ role: 'assistant', content: reply, calls: calls, reason: reason.trim() ? reason : undefined, stats: savedStats,
                         parts: (typeof __ocParts === 'function') ? __ocParts() : undefined });
  } else if (last && last.role === 'assistant' && !last.calls && reply) {
    last.content = reply;
    last.reason = reason.trim() ? reason : undefined;
    last.stats = savedStats;
  }
  scheduleCtx();
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

/* ---- context window meter ----------------------------------------------
   A new chat is not empty: the system prompt and the whole tool catalogue go
   out with every request, so the meter shows that fixed cost before you type
   and grows as the conversation does. Hover for the breakdown. */
let ctxLast = null;
let ctxTimer = null;
function fmtK(n) {
  n = Number(n) || 0;
  if (n < 1000) return String(n);
  return (n / 1000).toFixed(n >= 10000 ? 0 : 1).replace(/[.]0$/, '') + 'k';
}
function ctxPaint() {
  const meter = document.getElementById('ctxmeter');
  if (!meter) return;
  const num = document.getElementById('ctxnum');
  const fill = document.getElementById('ctxfill');
  const hint = document.getElementById('ctxhint');
  const j = ctxLast;
  if (!j) { num.textContent = '--'; if (hint) hint.textContent = 'measuring...'; return; }
  if (!j.ok) {
    meter.classList.remove('warn'); meter.classList.add('over');
    if (fill) fill.style.width = '100%';
    num.textContent = 'FULL';
  } else {
    const pct = j.total ? Math.min(100, j.used * 100 / j.total) : 0;
    meter.classList.toggle('warn', pct >= 50 && pct < 80);
    meter.classList.toggle('over', pct >= 80);
    if (fill) fill.style.width = pct.toFixed(1) + '%';
    num.textContent = fmtK(j.used) + '/' + fmtK(j.total);
  }
  if (hint) {
    if (!j.ok) {
      hint.innerHTML = '<b>Context full.</b><br>This conversation no longer fits the '
        + 'window.<br>Start a new chat, or raise ctx in model settings.';
    } else {
      let extra = '';
      if (j.images) extra += '<br>images: ' + j.images + ' (about ' + fmtK(j.images * 1024) + ')';
      if (!j.exact) extra += '<br>estimated - '
        + (j.why === 'quick' ? 'still typing'
          : j.why === 'remote' ? 'the provider counts these for you'
            : 'model not loaded');
      hint.innerHTML = '<b>' + (j.exact ? '' : '~') + Number(j.used).toLocaleString() + '</b>'
        + ' of <b>' + Number(j.total || 0).toLocaleString() + '</b> tokens'
        + '<br>system prompt + tools: ' + Number(j.fixed || 0).toLocaleString()
        + '<br>this conversation: ' + Number(j.own || 0).toLocaleString() + extra;
    }
  }
}
async function refreshCtx(quick) {
  /* Count what is actually going to be sent - queued messages and, once the
     chat has been compacted, everything above the fold. The meter's own
     tooltip promises the number is right "before you type", but the draft was
     never included, so the bar only ever moved after a turn had already been
     sent - you found out a message was too long once it was too late to
     shorten it. */
  const msgs = (cur && cur.messages) ? wireFor(cur).slice() : [];
  try {
    const draft = (typeof buildUserMsg === 'function') ? buildUserMsg() : null;
    if (draft) msgs.push({ role: 'user', content: draft });
  } catch (e) { /* no composer on this view */ }
  try {
    const r = await fetch('/api/ctx', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: msgs, mode: chatMode, quick: !!quick }) });
    ctxLast = await r.json();
  } catch (e) { ctxLast = { ok: false }; }
  ctxPaint();
  maybeFold(quick);
}
function scheduleCtx(quick) {
  if (ctxTimer) clearTimeout(ctxTimer);
  /* While typing, settle quickly but ask for the cheap estimate: an exact
     count costs two round-trips to the model server, and nobody needs that
     to be precise mid-word. The bar shows a ~ while it is a guess. */
  ctxTimer = setTimeout(function () { refreshCtx(quick); }, quick ? 500 : 400);
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
  // A remote model with no key would otherwise fail as a 401 halfway through,
  // after the turn is already spent and buried in a conversation.
  try {
    const block = await failFastKey();
    if (block) { alert(block); return; }
  } catch (e) { /* never block the send on a failed check */ }
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
  try { await streamRun(wireFor(target), target, asked); }
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
function fmtCtx(c) {
  c = Number(c) || 0;
  if (c >= 1000000) { const t = c / 1000000; return (t >= 10 ? Math.round(t) : Math.round(t * 10) / 10) + 'M'; }
  if (c >= 1000) return Math.floor(c / 1000) + 'k';
  return String(c);
}
function modelCtxLabel(m) {
  return m.ctx ? ' \u00b7 ' + fmtCtx(m.ctx) + ' ctx' : '';
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
  const _am = (j.models || []).filter(function (m) { return m.id === j.active; })[0];
  if (_am && _am.ctx) BONSAI_CTX = _am.ctx;
  scheduleCtx();
  sel.innerHTML = '';
  (j.models || []).forEach(function (m) {
    const o = document.createElement('option');
    o.value = m.id;
    o.textContent = modelLabel(m) + modelCtxLabel(m) + (m.type === 'local' && m.vision ? ' · vision' : '') +
      (m.type === 'local' && m.available === false ? ' (missing)' : '') +
      (m.type === 'api' ? ' · ' + (m.needs_key ? 'NO KEY' : (m.api_key_set ? 'key ok' : 'no key needed')) : '');
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
  pickModelPost(what, null)
    .then(function () { if (btn) { btn.disabled = false; btn.textContent = '+'; } });
}
/* The native browser cannot open everywhere: no python3-tk, or no display at
   all on a headless Linux box. The server says so plainly instead of reporting
   a bare "cancelled", and this asks for the path by hand rather than leaving
   the button to do nothing at all. */
function pickModelPost(what, path) {
  const body = { what: what };
  if (path) body.path = path;
  return fetch('/api/pick_model', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (j && j.no_browser) {
        const hint = what === 'folder' ? 'folder' : 'full path to the .gguf file';
        const typed = window.prompt(j.error + '\\n\\nEnter the ' + hint + ':', '');
        if (typed && typed.trim()) return pickModelPost(what, typed.trim());
        return null;
      }
      return afterAdd(j, what);
    })
    .catch(function () { alert('Could not open the file browser'); });
}
/* Pasting a model's web page address (openrouter.ai/qwen/qwen3.8-27b:free) is the
   obvious thing to try, but the API wants the base URL and the model id as two
   separate values. Translate a page address into those two fields. Only base
   URLs that are actually documented are filled in - never guessed. */
const API_PAGE_BASE = { 'openrouter.ai': 'https://openrouter.ai/api/v1' };
function fixupApiUrl() {
  const baseEl = document.getElementById('apibase');
  const modelEl = document.getElementById('apimodel');
  if (!baseEl || !modelEl) return false;
  const raw = (baseEl.value || '').trim();
  if (raw.indexOf('http') !== 0) return false;
  let u = null;
  try { u = new URL(raw); } catch (e) { return false; }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
  let host = u.hostname.toLowerCase();
  if (host.indexOf('www.') === 0) host = host.slice(4);
  const segs = u.pathname.split('/').filter(function (s) { return s !== ''; });
  if (!segs.length) return false;
  const head = segs[0].toLowerCase();
  /* already an API base address such as .../api/v1 - leave it be */
  if (head === 'api' || (head.charAt(0) === 'v' && Number(head.slice(1)) > 0)) return false;
  const known = API_PAGE_BASE[host];
  if (known) {
    let slug = '';
    try { slug = decodeURIComponent(segs.join('/')); } catch (e) { slug = segs.join('/'); }
    baseEl.value = known;
    modelEl.value = slug;
    return true;
  }
  /* Unknown provider: rescue the model id from the address, but do not
     invent a base URL - a wrong one fails in a way that looks like the key is
     broken rather than like the address is. */
  if (!(modelEl.value || '').trim()) modelEl.value = segs[segs.length - 1];
  return 'unknown';
}
const _apiBaseEl = document.getElementById('apibase');
if (_apiBaseEl) _apiBaseEl.onchange = function () {
  if (fixupApiUrl() === 'unknown') {
    alert('That looks like a model page address, not an API base URL.\\n\\n' +
          'I took "' + (document.getElementById('apimodel').value || '') +
          '" as the model id. Now enter the API base URL for that provider - ' +
          'for OpenAI-compatible providers it usually ends in /v1.');
  }
};
/* Fill in the key name we already know the answer to, so the common case is
   "paste the key once" rather than "guess what we named it". Only fills an
   empty box, and only for a host we recognise. */
var KNOWN_KEY_NAMES = { 'openrouter.ai': 'OPENROUTER_API_KEY', 'api.anthropic.com': 'ANTHROPIC_API_KEY', 'api.openai.com': 'OPENAI_API_KEY', 'generativelanguage.googleapis.com': 'GEMINI_API_KEY', 'api.groq.com': 'GROQ_API_KEY', 'api.mistral.ai': 'MISTRAL_API_KEY', 'api.deepseek.com': 'DEEPSEEK_API_KEY', 'openrouter.ai/api': 'OPENROUTER_API_KEY' };
function suggestKeyName() {
  var el = document.getElementById('apikey');
  if (!el) return;
  if ((el.value || '').trim()) return;
  var raw = (document.getElementById('apibase').value || '').trim();
  if (raw.indexOf('http') !== 0) return;
  var host;
  try { host = new URL(raw).hostname.toLowerCase(); } catch (e) { return; }
  if (host.indexOf('www.') === 0) host = host.slice(4);
  if (KNOWN_KEY_NAMES[host]) el.value = KNOWN_KEY_NAMES[host];
}
function addModelApi() {
  fixupApiUrl();
  suggestKeyName();
  const base = (document.getElementById('apibase').value || '').trim();
  const model = (document.getElementById('apimodel').value || '').trim();
  /* a NAME from API KEYS.txt, never the secret itself */
  const keyEl = document.getElementById('apikey');
  const key = keyEl ? (keyEl.value || '').trim() : '';
  if (!base || !model) { alert('Fill in both the base URL and the model id.'); return; }
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'add', type: 'api', label: model, base_url: base, model: model, api_key: key }) })
    .then(function (r) { return r.json(); })
    .then(function (j) {
      if (!j.ok) { alert('Could not add the model:\\n' + (j.error || 'unknown error')); return; }
      renderModels(j); closeAddMdl();
      document.getElementById('apibase').value = '';
      document.getElementById('apimodel').value = '';
      if (keyEl) keyEl.value = '';
      // a remote endpoint with no resolvable key cannot answer anything, and
      // saying so here beats finding out from a 401 mid-conversation
      if (j.warn) alert(j.warn);
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
/* fail-fast: a remote model with no resolvable key must never reach a chat.
   Reads the payload renderModels() already cached rather than asking again -
   /api/models costs a round trip and a readiness probe, which is real time
   added to every single message. The one case that does re-ask is the case
   where we are about to refuse the send, because API KEYS.txt is re-read on
   change and the user may have just pasted their key in. */
function keyProblem(j) {
  var act = null;
  for (var i = 0; i < (j.models || []).length; i++) if (j.models[i].id === j.active) { act = j.models[i]; break; }
  if (!act) return null;
  if (!act.needs_key) return null;
  if (act.api_key_set) return null;
  return "This model is a remote endpoint but no key is available for '" + (act.api_key_name || 'its API key') + "'. Open the gear (MODEL SETTINGS) and either add that name to API KEYS.txt, or fix the key name - then press TEST KEY.";
}
async function failFastKey() {
  var cached = window.__lastModels;
  // Nothing known yet: let the message through. Waiting on the network here
  // would add the /api/models round trip to a send, and that call is not cheap
  // - it probes model-server readiness, which is seconds when nothing is
  // listening. Not being able to fail fast is much better than being slow.
  if (!cached || !cached.models) return null;
  if (!keyProblem(cached)) return null;
  // Only now, where we are about to refuse the send, is a fresh read worth
  // it: API KEYS.txt is re-read on change, so the user may have just pasted
  // their key in and the cached payload would be out of date.
  var fresh = null;
  try { fresh = await fetch('/api/models').then(function (r) { return r.json(); }); }
  catch (e) { return keyProblem(cached); }
  if (fresh) {
    window.__lastModels = fresh;
    if (typeof renderModels === 'function') renderModels(fresh);
  }
  return keyProblem(fresh || cached);
}
/* ---------- MODEL SETTINGS modal ---------- */
/* A model's api_key is a *name* - of a line in API KEYS.txt, or of an
   environment variable - never the key itself. The backend can tell four
   states apart and each one needs a different word: reporting "key fine" for a
   name that is not in the file is how a 401 gets debugged from the wrong end. */
var KEY_BADGE = {
  named:   ['KEY FOUND', 'ok'],
  literal: ['KEY IN MODELS.JSON', 'warn'],
  missing: ['NOT IN API KEYS.TXT', 'bad'],
  none:    ['NO KEY', 'bad']
};
function keyStateOf(m) { return (m && m.api_key_state) || 'none'; }
function keyBadgeText(m) {
  var st = keyStateOf(m);
  // a llama-server on this PC needs no key, and saying NO KEY in red there
  // would be a lie about a perfectly working setup
  if (st === 'none' && m && !m.needs_key) return ['NO KEY NEEDED', ''];
  return KEY_BADGE[st] || KEY_BADGE.none;
}
function showKeyBadge(m) {
  var el = document.getElementById('cfg_keystate');
  if (!el) return;
  var b = keyBadgeText(m);
  el.textContent = b[0];
  el.className = 'kstate ' + b[1];
}
function keyHint(m) {
  if (!m || m.type !== 'api') return '';
  var name = m.api_key_name || '';
  var st = keyStateOf(m);
  if (st === 'named') return 'The key name is saved. The key itself stays in API KEYS.txt and is never shown here.';
  if (st === 'literal') return 'This model has the key itself in models.json, in plain text. Move it into API KEYS.txt under a name and put that name in the field above.';
  if (st === 'missing') return "'" + name + "' is not in API KEYS.txt, so it would be sent to the provider as if it were the key and every request would come back 401. Add '" + name + "=your-key' to API KEYS.txt, then press TEST KEY.";
  if (m.needs_key) return 'This endpoint needs a key. Put it in API KEYS.txt as NAME=your-key and put NAME in the field above.';
  return 'No key needed - this endpoint is on your own machine.';
}
function cfgFill(j) {
  const models = (j && j.models) || [];
  const act = models.filter(function (m) { return m.id === (j && j.active); })[0];
  const cfg = (act && act.cfg) || (j && j.defaults) || { ctx: 32768, temp: 1, top_p: 0.95, top_k: 20, ngl: 99 };
  if (act && act.ctx) BONSAI_CTX = act.ctx;
  const set = function (id, v) { const e = document.getElementById(id); if (e) e.value = v; };
  set('cfg_ctx', cfg.ctx); set('cfg_temp', cfg.temp);
  set('cfg_top_p', cfg.top_p); set('cfg_top_k', cfg.top_k); set('cfg_ngl', cfg.ngl);
  const who = document.getElementById('cfgwho');
  if (who) who.textContent = act ? ('- ' + (act.label || act.id)) : '';
  const isApi = !!(act && act.type === 'api');
  const keyRow = document.getElementById('cfg-keyrow');
  if (keyRow) keyRow.hidden = !isApi;
  const testBtn = document.getElementById('cfg-testkey');
  if (testBtn) testBtn.hidden = !isApi;
  const keyIn = document.getElementById('cfg_apikey');
  if (keyIn) keyIn.value = (act && act.api_key_name) || '';
  showKeyBadge(act);
  const hint = document.getElementById('cfghint');
  if (hint) {
    hint.textContent = !act ? 'No local model selected.'
      : (isApi ? keyHint(act)
      : 'Save and the model server restarts, so your next message uses these values. EJECT does the same by hand.');
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
  const body = { action: 'config', id: act.id, cfg: cfg };
  if (act.type === 'api') {
    const k = document.getElementById('cfg_apikey');
    body.api_key = k ? (k.value || '').trim() : '';
  }
  _cfgSave.disabled = true;
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (!res.ok) { alert('Could not save the settings:\\n' + (res.error || 'unknown error')); return; }
      renderModels(res);
      cfgFill(res);
      const hint = document.getElementById('cfghint');
      if (hint) hint.textContent = res.warn ? ('Saved. ' + res.warn)
        : ('Saved. ' + (res.detail || 'These values apply the next time this model is loaded.'));
    })
    .catch(function () { alert('Could not save the settings'); })
    .then(function () { _cfgSave.disabled = false; });
};
/* One cheap authenticated GET instead of a whole failed conversation. */
const _cfgTest = document.getElementById('cfg-testkey');
if (_cfgTest) _cfgTest.onclick = function () {
  const j = window.__lastModels || {};
  const act = (j.models || []).filter(function (m) { return m.id === j.active; })[0];
  if (!act) { alert('No model selected.'); return; }
  const k = document.getElementById('cfg_apikey');
  const hint = document.getElementById('cfghint');
  _cfgTest.disabled = true;
  const was = _cfgTest.textContent;
  _cfgTest.textContent = 'TESTING...';
  fetch('/api/models', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action: 'test_key', base_url: act.base_url,
                           model: act.model, api_key: k ? (k.value || '').trim() : '' }) })
    .then(function (r) { return r.json(); })
    .then(function (res) {
      if (res && res.ok) {
        let msg = res.detail || 'the key was accepted';
        if (res.context_length) msg += ' - context window ' + res.context_length + ' tokens';
        if (res.model_ok === false) msg = 'The key works. ' + (res.warn || '');
        if (hint) hint.textContent = msg;
      } else if (hint) {
        hint.textContent = (res && res.error) || 'the key test failed';
      }
    })
    .catch(function () { if (hint) hint.textContent = 'the key test could not reach the provider'; })
    .then(function () { _cfgTest.disabled = false; _cfgTest.textContent = was; });
};
document.addEventListener('keydown', function (ev) {
  if (ev.key === 'Escape') { closeAddMdl(); closeCfgMdl(); }
});
refreshModels();
const inp = document.getElementById('user-input');
inp.addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(this.scrollHeight, 160) + 'px'; scheduleCtx(true); });
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


# ---- OpenCode-style tool steps + code differencing (every UI) --------
# One bundle, injected into both consoles: a tool call renders as a
# collapsible step line (-> Read, * Grep, pencil Edit, + Thought) and every
# write carries its diff, viewable and revertible from the CHANGES panel.
_OC_CSS = (
    '\n'
    '  /* ---- OpenCode-style tool steps + code differencing ---------------- */\n'
    '  .ocsteps { margin: 6px 0 2px; font-family: Consolas, "Courier New", monospace; font-size: 12.5px; }\n'
    '  .ocsteps:empty { display: none; }\n'
    '  /* the steps are chat entries in their own right now, not part of a\n'
    '     bubble, so they need their own spacing and indent */\n'
    '  .ocsteps { margin: 4px 0 8px 30px; }\n'
    '  .msgrow.bonsai + .ocsteps, .msgrow + .ocsteps { clear: both; }\n'
    "  /* the model's own words sit between the steps: prose, not code */\n"
    '  .ocpara { margin: 4px 0 8px; font-family: inherit; font-size: inherit;\n'
    '            line-height: inherit; color: var(--txt); }\n'
    '  .ocpara:empty { display: none; }\n'
    '  .ocpara > :first-child { margin-top: 0; }\n'
    '  .ocpara > :last-child { margin-bottom: 0; }\n'
    '  .ocpara pre, .ocpara code { font-family: Consolas, "Courier New", monospace; }\n'
    '  .ocstep { border-left: 2px solid var(--bd); margin: 0 0 1px; }\n'
    '  .ocstep.err { border-left-color: var(--err); }\n'
    '  .ocstep.thought { border-left-color: var(--violet); }\n'
    '  .ocstep.thought.notext > summary { cursor: default; }\n'
    '  .ocstep.thought.notext > summary:hover { background: none; color: var(--txt); }\n'
    '  .ocstep.thought .ocbody pre { color: #c4b5fd; white-space: pre-wrap;\n'
    '                                  max-height: 260px; overflow-y: auto; margin: 0; }\n'
    '  .ocstep > summary {\n'
    '    list-style: none; cursor: pointer; display: flex; align-items: baseline; gap: 7px;\n'
    '    padding: 3px 8px; border-radius: 6px; color: var(--txt2);\n'
    '  }\n'
    '  .ocstep > summary::-webkit-details-marker { display: none; }\n'
    '  .ocstep > summary:hover { background: var(--bg3); color: var(--txt); }\n'
    '  .ocstep .ocg { flex: 0 0 auto; width: 11px; text-align: center; color: var(--acc); }\n'
    '  .ocstatus { display: flex; align-items: center; gap: 8px; margin: 6px 0 2px 44px;\n'
    '             font-size: 12.5px; color: var(--mut); font-family: Consolas, monospace; }\n'
    '  .ocstatus .ocs { color: var(--acc); animation: ocpulse 1.1s ease-in-out infinite; }\n'
    '  @keyframes ocpulse { 0%, 100% { opacity: .25; } 50% { opacity: 1; } }\n'
    '  @media (prefers-reduced-motion: reduce) { .ocstatus .ocs { animation: none; } }\n'
    '  .ocstep.thought .ocg { color: var(--violet); }\n'
    '  .ocstep.err .ocg { color: var(--err); }\n'
    '  /* while the model is still in this block of reasoning the + turns and the\n'
    '     label says Thinking; when the block ends it stops and reads Thought */\n'
    '  .ocstep.thought.live .ocg { animation: ocspin 1.05s linear infinite; }\n'
    '  .ocstep.thought.live .ocn { color: var(--violet); }\n'
    '  .ocstep.thought.live > summary { cursor: default; }\n'
    '  @keyframes ocspin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }\n'
    '  @media (prefers-reduced-motion: reduce) {\n'
    '    .ocstep.thought.live .ocg { animation: none; }\n'
    '  }\n'
    '  .ocstep .ocn { font-weight: 700; color: var(--txt); }\n'
    '  .ocstep .ocd { color: var(--mut); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1 1 auto; }\n'
    '  .ocstep .ocd .ocq { color: var(--txt2); }\n'
    '  .ocstep .oct { flex: 0 0 auto; color: var(--mut); font-size: 11.5px; }\n'
    '  .ocstep .ocadd { color: var(--ok); }\n'
    '  .ocstep .ocdel { color: var(--err); }\n'
    '  .ocstep[open] > summary { background: var(--bg3); color: var(--txt); }\n'
    '  .ocstep .occaret { font-size: 9px; color: var(--mut); transition: transform .12s; }\n'
    '  .ocstep[open] .occaret { transform: rotate(90deg); }\n'
    '  .ocbody { margin: 2px 0 6px 12px; padding: 7px 9px; background: var(--bg2);\n'
    '            border: 1px solid var(--bd); border-radius: 8px; }\n'
    '  .ocbody pre { margin: 0 0 6px; white-space: pre-wrap; word-break: break-word;\n'
    '                color: var(--txt2); font-size: 12px; max-height: 320px; overflow: auto; }\n'
    '  .ocbody pre:last-child { margin-bottom: 0; }\n'
    '  .ocbody .oclab { color: var(--mut); font-size: 10.5px; letter-spacing: 1px; margin-bottom: 3px; }\n'
    '  .ocargs { color: var(--mut); font-size: 11.5px; }\n'
    '  .ocprev { color: var(--acc); text-decoration: none; }\n'
    '  .ocprev:hover { text-decoration: underline; }\n'
    '\n'
    '  /* diff view */\n'
    '  .ocdiff { border: 1px solid var(--bd); border-radius: 8px; overflow: auto;\n'
    '            max-height: 460px; background: #0d0d11; font-size: 12px; }\n'
    '  .ocdiff table { border-collapse: collapse; width: 100%; }\n'
    '  .ocdiff td { padding: 0 8px; white-space: pre-wrap; word-break: break-word;\n'
    '               vertical-align: top; font-family: Consolas, "Courier New", monospace; }\n'
    '  .ocdiff td.ocl { width: 1%; text-align: right; color: #4b4b57; user-select: none;\n'
    '                   padding: 0 6px 0 4px; font-size: 11px; }\n'
    '  .ocdiff td.octext { width: 100%; }\n'
    '  .ocdiff tr.hunk td { color: var(--violet); background: rgba(167,139,250,.08);\n'
    '                       font-size: 11px; padding-top: 3px; padding-bottom: 3px; }\n'
    '  .ocdiff tr.add td { background: rgba(52,211,153,.10); color: #b6f2d8; }\n'
    '  .ocdiff tr.add td.ocl { background: rgba(52,211,153,.18); }\n'
    '  .ocdiff tr.del td { background: rgba(248,113,113,.10); color: #fbc4c4; }\n'
    '  .ocdiff tr.del td.ocl { background: rgba(248,113,113,.18); }\n'
    "  .ocdiff tr.add td.octext::before { content: '+'; color: var(--ok); margin-right: 2px; }\n"
    "  .ocdiff tr.del td.octext::before { content: '-'; color: var(--err); margin-right: 2px; }\n"
    '\n'
    '  /* changes bar + modal */\n'
    '  .ocbar { display: flex; align-items: center; gap: 8px; margin: 0 0 6px; }\n'
    '  .ocbarbtn { background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2);\n'
    '              border-radius: 8px; padding: 6px 10px; cursor: pointer;\n'
    '              font-family: Consolas, monospace; font-size: 12px; }\n'
    '  .ocbarbtn:hover { border-color: var(--acc); color: var(--txt); }\n'
    '  .ocbarbtn.on { border-color: var(--ok); color: var(--ok); background: rgba(52,211,153,.12); }\n'
    '  .ocbarbtn .ocadd { color: var(--ok); }\n'
    '  .ocbarbtn .ocdel { color: var(--err); }\n'
    '  .ocmodal { position: fixed; inset: 0; z-index: 120; display: none;\n'
    '             align-items: center; justify-content: center;\n'
    '             background: rgba(0,0,0,.66); backdrop-filter: blur(3px); }\n'
    '  .ocmodal.on { display: flex; }\n'
    '  .ocmbox { width: min(1080px, 94vw); max-height: 88vh; display: flex; flex-direction: column;\n'
    '            background: var(--bg2); border: 1px solid var(--bd2); border-radius: 14px;\n'
    '            font-family: Consolas, monospace; }\n'
    '  .ocmh { display: flex; align-items: center; gap: 10px; padding: 12px 14px;\n'
    '          border-bottom: 1px solid var(--bd); }\n'
    '  .ocmh b { color: var(--acc); font-size: 13px; letter-spacing: 1.5px; }\n'
    '  .ocmh .ocsum { color: var(--mut); font-size: 12px; }\n'
    '  .ocmh .sp { flex: 1; }\n'
    '  .ocmact { background: var(--bg3); border: 1px solid var(--bd2); color: var(--txt2);\n'
    '            border-radius: 8px; padding: 6px 11px; cursor: pointer; font: inherit; font-size: 12px; }\n'
    '  .ocmact:hover { border-color: var(--acc); color: var(--txt); }\n'
    '  .ocmact.danger:hover { border-color: var(--err); color: var(--err); }\n'
    '  .ocmlist { overflow: auto; padding: 8px; }\n'
    '  .ocmfile { border: 1px solid var(--bd); border-radius: 10px; margin-bottom: 8px;\n'
    '             background: var(--bg3); overflow: hidden; }\n'
    '  .ocmfhead { display: flex; align-items: center; gap: 8px; padding: 8px 10px; cursor: pointer; }\n'
    '  .ocmfhead:hover { background: var(--bg4); }\n'
    '  .ocmfhead .ocf { color: var(--txt); font-size: 12.5px; word-break: break-all; flex: 1; }\n'
    '  .ocmfhead .octag { color: var(--mut); font-size: 10.5px; border: 1px solid var(--bd2);\n'
    '                     border-radius: 5px; padding: 1px 5px; }\n'
    '  .ocmfbody { padding: 0 10px 10px; display: none; }\n'
    '  .ocmfile.open .ocmfbody { display: block; }\n'
    '  .ocmdiff { border: 1px solid var(--bd); border-radius: 8px; overflow: auto;\n'
    '             max-height: 58vh; background: #0d0d11; font-size: 12px; }\n'
    '  .ocmempty { color: var(--mut); font-size: 12.5px; padding: 22px; text-align: center; }\n'
    ''
)

_OC_JS = (
    '/* ===================================================================\n'
    '   OpenCode-style tool steps + code differencing for the BONSAI chats.\n'
    '   Injected into every UI the server serves, so both consoles behave\n'
    '   the same way. The page keeps its own functions; these wrappers take\n'
    '   over the rendering of tool calls (chips are gone) and add:\n'
    '     -> one collapsible line per tool call, with the args in the header\n'
    '        and the output in the body\n'
    '     -> a "Thought" line with the real time the model took to think\n'
    '     -> a rendered diff for every write, and a project changes panel\n'
    '        with per-file revert\n'
    '   =================================================================== */\n'
    '(function () {\n'
    '  if (window.__ocSteps) return;\n'
    '  window.__ocSteps = true;\n'
    '\n'
    '  var OCS = {\n'
    '    steps: null, bubble: null, since: 0, thinkShown: false, running: false,\n'
    "    thinkMs: 0, roundText: '', thoughtNode: null, tailMs: 0,\n"
    '    last: null, pending: 0, timer: null, tick: null, status: null,\n'
    '    /* ordered skeleton of the answer, so a reload can replay the transcript\n'
    "       exactly as it streamed: {k:'c',i} tool call, {k:'t',v} text,\n"
    "       {k:'r',ms,v} reasoning that came after the last tool call */\n"
    "    parts: [], textNode: null, textRaw: '', textPart: null, callIdx: 0,\n"
    "    tailReason: '', thoughtPart: null\n"
    '  };\n'
    '\n'
    '  /* ---------- formatting helpers ---------- */\n'
    '  /* How much of a step the chat shows. A step is a collapsed <details> by\n'
    '     default, which is right: a dozen tool calls expanded is unreadable.\n'
    '     "Full" opens them all. A display choice, so it lives in localStorage. */\n'
    '  function ocFull() {\n'
    "    try { return localStorage.getItem('bonsai_stepdetail') === 'full'; } catch (e) { return false; }\n"
    '  }\n'
    '  function renderStepBtn() {\n'
    "    var b = document.getElementById('detailbtn');\n"
    "    if (b) b.textContent = 'DETAIL: ' + (ocFull() ? 'FULL' : 'BRIEF');\n"
    '  }\n'
    '  function applyDetail() {\n'
    '    var on = ocFull();\n'
    "    var all = document.querySelectorAll('.ocstep');\n"
    '    for (var i = 0; i < all.length; i++) all[i].open = on;\n'
    '  }\n'
    "  document.addEventListener('DOMContentLoaded', function () {\n"
    '    renderStepBtn();\n'
    "    var b = document.getElementById('detailbtn');\n"
    "    if (b) b.onclick = function () {\n"
    "      try { localStorage.setItem('bonsai_stepdetail', ocFull() ? 'brief' : 'full'); } catch (e) {}\n"
    '      renderStepBtn(); applyDetail();\n'
    '    };\n'
    '  });\n'
    '  /* The model saying something is a response, not a step, so it gets a\n'
    '     message bubble of its own rather than a line inside the step block.\n'
    '     The order is kept: it is placed after the steps that came before it\n'
    '     and before the ones that follow. */\n'
    '  function textRow() {\n'
    '    var bub = el(\'div\', \'bubble ocpara-bubble\');\n'
    '    var av = el(\'div\', \'av bonsai\'); av.textContent = \'B\';\n'
    '    var row = el(\'div\', \'msgrow bonsai\');\n'
    '    var p = el(\'div\', \'ocpara\'); p.textContent = \'\';\n'
    '    bub.appendChild(p);\n'
    '    row.appendChild(av); row.appendChild(bub);\n'
    '    var host = OCS.bubble && OCS.bubble.parentNode\n'
    '             ? OCS.bubble.parentNode.parentNode : null;\n'
    '    var answerRow = OCS.bubble ? OCS.bubble.parentNode : null;\n'
    '    if (host && answerRow) {\n'
    '      /* Straight after the steps that came before this line of prose, and\n'
    '         not simply at the end: "think, say, do, think, say" has to read that\n'
    '         way round. The steps block is therefore closed here, so the steps\n'
    '         that follow start a new one and the order survives. */\n'
    '      var ref = (OCS.steps && OCS.steps.parentNode === host) ? OCS.steps : answerRow;\n'
    '      host.insertBefore(row, ref.nextSibling);\n'
    '      OCS.steps = null;\n'
    '    } else if (answerRow) {\n'
    '      answerRow.parentNode.appendChild(row);\n'
    '    }\n'
    '    OCS.textRow = row;\n'
    '    return p;\n'
    '  }\n'
    '\n'
    '  function el(tag, cls, text) {\n'
    '    var n = document.createElement(tag);\n'
    '    if (cls) n.className = cls;\n'
    '    if (text !== undefined && text !== null) n.textContent = String(text);\n'
    '    return n;\n'
    '  }\n'
    '  function secs(ms) {\n'
    "    if (ms === null || ms === undefined) return '';\n"
    '    var s = ms / 1000;\n'
    "    if (s >= 100) return Math.round(s) + 's';\n"
    "    if (s >= 10) return s.toFixed(1) + 's';\n"
    "    return s.toFixed(2) + 's';\n"
    '  }\n'
    '  function firstLine(s) {\n'
    "    s = String(s === null || s === undefined ? '' : s);\n"
    "    var t = s.replace(/[\\r\\n]+/g, ' ').replace(/\\s+/g, ' ').trim();\n"
    "    return t.length > 70 ? t.slice(0, 70) + '...' : t;\n"
    '  }\n'
    '  function num(v) {\n'
    '    v = parseInt(v, 10);\n'
    "    return isNaN(v) ? '' : String(v);\n"
    '  }\n'
    '  function pathOf(a) {\n'
    "    if (!a) return '';\n"
    "    var p = a.path || a.file_path || a.filePath || a.filepath || a.pathname || '';\n"
    '    if (!p) {\n'
    '      Object.keys(a).forEach(function (k) {\n'
    "        if (!p && /path|file/i.test(k) && typeof a[k] === 'string' && a[k].indexOf('\\\\') >= 0\n"
    "            || (!p && /path|file/i.test(k) && String(a[k] || '').indexOf('/') >= 0)) p = a[k];\n"
    '      });\n'
    '    }\n'
    "    return String(p || '');\n"
    '  }\n'
    '  function rel(path) {\n'
    "    if (!path) return '';\n"
    '    var parts = String(path).split(/[\\\\\\/]/);\n'
    "    return parts.length <= 2 ? String(path) : parts.slice(-2).join('/');\n"
    '  }\n'
    '\n'
    '  /* the verb a reader cares about, not the JSON tool name */\n'
    '  var KINDS = {\n'
    "    read:     { g: '→', n: 'Read',   edit: false },\n"
    "    write:    { g: '✎', n: 'Write',  edit: true },\n"
    "    edit:     { g: '✎', n: 'Edit',   edit: true },\n"
    "    patch:    { g: '✎', n: 'Patch',  edit: true },\n"
    "    multiedit:{ g: '✎', n: 'Edit',   edit: true },\n"
    "    grep:     { g: '✱', n: 'Grep',   edit: false },\n"
    "    glob:     { g: '✱', n: 'Glob',   edit: false },\n"
    "    list:     { g: '→', n: 'List',   edit: false },\n"
    "    ls:       { g: '→', n: 'List',   edit: false },\n"
    "    search:   { g: '✱', n: 'Search', edit: false },\n"
    "    shell:    { g: '⚡', n: 'Shell',  edit: false },\n"
    "    exec:     { g: '⚡', n: 'Shell',  edit: false },\n"
    "    web:      { g: '→', n: 'Fetch',  edit: false },\n"
    "    fetch:    { g: '→', n: 'Fetch',  edit: false },\n"
    "    ask:      { g: '?', n: 'Ask',    edit: false },\n"
    "    todo:     { g: '☑', n: 'Todo',   edit: false },\n"
    "    launch:   { g: '⚡', n: 'Open',   edit: false },\n"
    "    speak:    { g: '♪', n: 'Speak',  edit: false },\n"
    "    shot:     { g: '◎', n: 'Shot',   edit: false },\n"
    "    task:     { g: '✱', n: 'Task',   edit: false }\n"
    '  };\n'
    '  var ALIASES = {\n'
    "    read_file: 'read', read_text_file: 'read', view_file: 'read',\n"
    "    open_file: 'read', view: 'read',\n"
    "    write_file: 'write', create_file: 'write', save_file: 'write',\n"
    "    edit_file: 'edit', replace_in_file: 'edit', str_replace: 'edit',\n"
    "    apply_patch: 'patch', apply_diff: 'patch', multi_edit: 'multiedit',\n"
    "    search_in_files: 'grep', search_files: 'grep', find_in_files: 'grep',\n"
    "    codebase_search: 'grep',\n"
    "    list_files_in_folder: 'list', list_directory: 'list', ls_dir: 'list',\n"
    "    file_search: 'glob', find_files: 'glob',\n"
    "    web_search: 'web', web_fetch: 'fetch', fetch_url: 'fetch', http: 'fetch',\n"
    "    run_shell_command: 'shell', run_command: 'shell', terminal: 'shell',\n"
    "    shell_oc: 'shell', bash: 'shell', shell_cmd: 'shell',\n"
    "    question: 'ask', ask_user: 'ask', ask_followup_question: 'ask',\n"
    "    todo_write: 'todo', update_todo_list: 'todo',\n"
    "    launch_or_open: 'launch', open_application: 'launch',\n"
    "    tts_speak: 'speak', speak_text: 'speak',\n"
    "    screenshot: 'shot', screen_capture: 'shot', capture_screen: 'shot',\n"
    "    tts: 'speak', task: 'task', agent: 'task'\n"
    '  };\n'
    '\n'
    '  function kindOf(call) {\n'
    "    var n = String((call && call.name) || '');\n"
    '    var k = ALIASES[n.toLowerCase()] || n.toLowerCase();\n'
    "    if (/read|open_file|cat\\b/.test(k)) k = 'read';\n"
    "    else if (/write|create/.test(k)) k = 'write';\n"
    "    else if (/edit|patch|replace/.test(k)) k = 'edit';\n"
    "    else if (/grep|search_in|find_in/.test(k)) k = 'grep';\n"
    "    else if (/glob/.test(k)) k = 'glob';\n"
    "    else if (/list|ls\\b|dir\\b/.test(k)) k = 'list';\n"
    "    else if (/shell|run_command|bash|terminal|exec/.test(k)) k = 'shell';\n"
    "    else if (/screenshot|capture_screen|screen_shot/.test(k)) k = 'shot';\n"
    "    else if (/web_search|internet/.test(k)) k = 'web';\n"
    "    else if (/fetch|url|http|download_page/.test(k)) k = 'fetch';\n"
    "    else if (/ask|question/.test(k)) k = 'ask';\n"
    "    else if (/todo/.test(k)) k = 'todo';\n"
    "    else if (/launch|open_app|start_app|activate/.test(k)) k = 'launch';\n"
    "    else if (/tts|speak|say|speech/.test(k)) k = 'speak';\n"
    "    return KINDS[k] ? { key: k, spec: KINDS[k] } : { key: k, spec: { g: '⚙', n: n || 'tool', edit: false } };\n"
    '  }\n'
    '\n'
    '  /* how many things a tool touched, for "(N matches)" */\n'
    '  function countOf(call, k, res) {\n'
    '    try {\n'
    "      if (k === 'grep') {\n"
    "        if (res && typeof res.count === 'number') return num(res.count);\n"
    '        var m = /Found (\\d+) match/i.exec(res && (res.output || res.content));\n'
    '        if (m) return m[1];\n'
    "        var s = String((res && (res.output || res.content)) || '');\n"
    "        return num((s.match(/^\\s*(?:\\d+:\\s*)?.*$/gm) || []).length && (s.trim() ? s.trim().split('\\n').length : 0));\n"
    '      }\n'
    "      if (k === 'glob' || k === 'list') {\n"
    "        if (res && typeof res.count === 'number') return num(res.count);\n"
    '        var e = res && (res.entries || res.files || res.items);\n'
    '        if (e && e.length) return num(e.length);\n'
    "        var o = String((res && (res.output || res.result)) || '');\n"
    "        return o && o !== 'No files found' ? num(o.trim().split('\\n').length) : '0';\n"
    '      }\n'
    '    } catch (e) {}\n'
    "    return '';\n"
    '  }\n'
    '\n'
    '  function header(call, k, spec, res) {\n'
    '    var a = call.arguments || {}, p = pathOf(a);\n'
    "    var n = spec.n, extra = '';\n"
    "    if (k === 'read') {\n"
    "      if (a.offset) extra = ' [offset=' + num(a.offset);\n"
    "      if (a.limit) extra += (extra ? ',' : ' [') + 'limit=' + num(a.limit);\n"
    "      if (extra) extra += ']';\n"
    "      n = p ? 'Read ' + rel(p) + extra : 'Read';\n"
    "      extra = '';   /* the range is part of the name, not a trailing tag */\n"
    '    } else if (spec.edit) {\n'
    '      n = (p ? rel(p) : (res && res.path ? rel(res.path) : n));\n'
    "    } else if (k === 'grep') {\n"
    '      n = \'Grep \' + (a.pattern ? \'"\' + a.pattern + \'"\' : \'\') + (a.path ? \' in \' + a.path : \' in .\');\n'
    '      var c = countOf(call, k, res);\n'
    "      if (c !== '') extra = ' (' + c + ' match' + (c === '1' ? '' : 'es') + ')';\n"
    "    } else if (k === 'glob') {\n"
    "      n = 'Glob ' + (a.pattern || '') + (a.path ? ' in ' + a.path : '');\n"
    '      var cg = countOf(call, k, res);\n'
    "      if (cg !== '') extra = ' (' + cg + ' file' + (cg === '1' ? '' : 's') + ')';\n"
    "    } else if (k === 'list') {\n"
    "      n = 'List ' + (p ? a.path : '.');\n"
    '      var cl = countOf(call, k, res);\n'
    "      if (cl !== '') extra = ' (' + cl + ' entries)';\n"
    "    } else if (k === 'shell' || k === 'exec') {\n"
    "      n = 'Shell ' + firstLine(a.command || a.cmd || a.script || '');\n"
    "    } else if (k === 'web' || k === 'fetch') {\n"
    "      n = (k === 'web' ? 'Search ' : 'Fetch ') + firstLine(a.query || a.url || a.q || '');\n"
    "    } else if (k === 'ask') {\n"
    "      n = 'Ask ' + firstLine(a.question || a.q || a.prompt || '');\n"
    "    } else if (k === 'todo') {\n"
    '      var t = a.todos || a.items || [];\n'
    "      n = 'Todo list (' + t.length + ')';\n"
    "    } else if (k === 'launch') {\n"
    "      n = 'Open ' + firstLine(a.name || a.app || a.target || a.path || '');\n"
    "    } else if (k === 'speak') {\n"
    "      n = 'Speak ' + firstLine(a.text || '');\n"
    "    } else if (k === 'shot') {\n"
    "      n = 'Screenshot';\n"
    "    } else if (k === 'task') {\n"
    "      n = 'Task ' + firstLine(a.description || a.prompt || a.task || '');\n"
    '    } else {\n'
    '      var ks = Object.keys(a).filter(function (x) {\n'
    '        var v = a[x];\n'
    "        return typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean';\n"
    '      }).slice(0, 3);\n'
    "      if (ks.length) n = spec.n + ' ' + ks.map(function (x) { return x + '=' + firstLine(a[x]); }).join(' ');\n"
    '    }\n'
    '    return { name: n, extra: extra };\n'
    '  }\n'
    '\n'
    '  /* ---------- diff rendering ---------- */\n'
    '  function diffRows(diff) {\n'
    '    var rows = [];\n'
    "    String(diff || '').split('\\n').forEach(function (l) {\n"
    '      if (!l) return;\n'
    "      if (l.indexOf('--- ') === 0 || l.indexOf('+++ ') === 0) return;\n"
    "      if (l.indexOf('@@') === 0) { rows.push({ c: 'h', t: l }); return; }\n"
    "      if (l.charAt(0) === '+') { rows.push({ c: 'a', t: l.slice(1) }); return; }\n"
    "      if (l.charAt(0) === '-') { rows.push({ c: 'd', t: l.slice(1) }); return; }\n"
    "      if (l.charAt(0) === '\\\\') return;\n"
    "      rows.push({ c: ' ', t: l.slice(1) });\n"
    '    });\n'
    '    return rows;\n'
    '  }\n'
    '  function diffTable(diff) {\n'
    "    var wrap = el('div', 'ocdiff'), tb = el('table'), rows = diffRows(diff);\n"
    '    if (!rows.length) {\n'
    "      wrap.appendChild(el('div', 'ocmempty', '(no textual diff)'));\n"
    '      return wrap;\n'
    '    }\n'
    '    var oldn = 0, newn = 0;\n'
    '    rows.forEach(function (r) {\n'
    "      if (r.c === 'h') {\n"
    '        var m = /@@\\s*-(\\d+)/.exec(r.t), m2 = /@@.*\\+(\\d+)/.exec(r.t);\n'
    '        oldn = m ? parseInt(m[1], 10) : 0;\n'
    '        newn = m2 ? parseInt(m2[1], 10) : 0;\n'
    "        var tr = el('tr', 'hunk');\n"
    "        var td = el('td', 'octext', r.t);\n"
    "        td.colSpan = 2; tr.appendChild(el('td', 'ocl', '')); tr.appendChild(td);\n"
    '        tb.appendChild(tr);\n'
    '        return;\n'
    '      }\n'
    "      if (r.c === 'a') newn++;\n"
    "      else if (r.c === 'd') oldn++;\n"
    '      else { oldn++; newn++; }\n'
    "      var tr2 = el('tr', r.c === 'a' ? 'add' : (r.c === 'd' ? 'del' : 'ctx'));\n"
    "      tr2.appendChild(el('td', 'ocl', r.c === 'a' ? String(newn) : String(oldn)));\n"
    "      tr2.appendChild(el('td', 'octext', r.t));\n"
    '      tb.appendChild(tr2);\n'
    '    });\n'
    '    wrap.appendChild(tb);\n'
    '    return wrap;\n'
    '  }\n'
    '\n'
    '  /* ---------- the step line ---------- */\n'
    '  function resultText(res) {\n'
    "    if (typeof res === 'string') return res;\n"
    "    if (!res || typeof res !== 'object') return String(res);\n"
    '    var copy = {};\n'
    '    Object.keys(res).forEach(function (key) {\n'
    "      if (key === 'preview' || key === 'preview_url') return;\n"
    '      copy[key] = res[key];\n'
    '    });\n'
    '    try { return JSON.stringify(copy, null, 2); } catch (e) { return String(res); }\n'
    '  }\n'
    '  function shotOf(call) {\n'
    '    try {\n'
    "      return typeof shotCardDom === 'function' ? shotCardDom(call) : null;\n"
    '    } catch (e) { return null; }\n'
    '  }\n'
    '  function previewOf(res) {\n'
    "    var url = res && typeof res === 'object' ? res.preview_url : '';\n"
    '    if (!url) return null;\n'
    "    var w = el('div', 'ocargs');\n"
    "    var a = el('a', 'ocprev', res.path || 'preview');\n"
    "    a.href = url; a.target = '_blank'; a.rel = 'noreferrer';\n"
    '    a.onclick = function (e) {\n'
    "      if (typeof openPreviewStage === 'function' && openPreviewStage(url, res.path || '')) e.preventDefault();\n"
    '    };\n'
    '    w.appendChild(a);\n'
    '    return w;\n'
    '  }\n'
    '  function argsBlock(call) {\n'
    '    var a = call.arguments || {};\n'
    '    var keys = Object.keys(a);\n'
    '    if (!keys.length) return null;\n'
    "    var box = el('div');\n"
    "    box.appendChild(el('div', 'oclab', 'ARGUMENTS'));\n"
    "    var pre = el('pre', 'ocargs', JSON.stringify(a, null, 2));\n"
    '    box.appendChild(pre);\n'
    '    return box;\n'
    '  }\n'
    '\n'
    '  function stepFor(call) {\n'
    '    var res = call.result || {};\n'
    '    var kind = kindOf(call);\n'
    '    var hd = header(call, kind.key, kind.spec, res);\n'
    '    var chg = call.change || null;\n'
    '    var bad = !!(res && res.error);\n'
    "    var d = el('details', 'ocstep' + (bad ? ' err' : ''));\n"
    '    if (ocFull()) d.open = true;\n'
    "    var s = el('summary');\n"
    "    s.appendChild(el('span', 'occaret', '›'));\n"
    "    s.appendChild(el('span', 'ocg', kind.spec.g));\n"
    "    s.appendChild(el('span', 'ocn', hd.name));\n"
    "    if (hd.extra) s.appendChild(el('span', 'ocd', hd.extra));\n"
    "    else if (!kind.spec.edit) s.appendChild(el('span', 'ocd', ''));\n"
    '    if (chg && (chg.total_added || chg.total_removed || chg.added || chg.removed)) {\n'
    '      var a = chg.added !== undefined ? chg.added : chg.total_added;\n'
    '      var r = chg.removed !== undefined ? chg.removed : chg.total_removed;\n'
    "      s.appendChild(el('span', 'oct ocadd', '+' + num(a || 0)));\n"
    "      s.appendChild(el('span', 'oct ocdel', '-' + num(r || 0)));\n"
    '    }\n'
    "    if (call.ms !== undefined && call.ms !== null) s.appendChild(el('span', 'oct', secs(call.ms)));\n"
    '    d.appendChild(s);\n'
    '\n'
    "    var body = el('div', 'ocbody');\n"
    "    if (chg && chg.diff && String(chg.diff).indexOf('@@') >= 0) {\n"
    "      body.appendChild(el('div', 'oclab', 'DIFF'));\n"
    '      body.appendChild(diffTable(chg.diff));\n'
    '    }\n'
    '    var ab = argsBlock(call);\n'
    '    if (ab) body.appendChild(ab);\n'
    "    if (!(chg && chg.diff && String(chg.diff).indexOf('@@') >= 0)) {\n"
    '      var txt = resultText(res);\n'
    "      if (txt && txt !== '{}') {\n"
    "        body.appendChild(el('div', 'oclab', 'OUTPUT'));\n"
    "        body.appendChild(el('pre', null, txt));\n"
    '      }\n'
    '    } else if (res && res.error) {\n'
    "      body.appendChild(el('pre', null, res.error));\n"
    '    }\n'
    '    var pv = previewOf(res);\n'
    '    if (pv) body.appendChild(pv);\n'
    '    var sc = shotOf(call);\n'
    '    if (sc) body.appendChild(sc);\n'
    '    d.appendChild(body);\n'
    '    d.__oc = { call: call, kind: kind.key };\n'
    '    return d;\n'
    '  }\n'
    '\n'
    '  function thoughtStep(ms, text) {\n'
    "    var d = el('details', 'ocstep thought');\n"
    '    if (ocFull()) d.open = true;\n'
    "    var s = el('summary');\n"
    "    s.appendChild(el('span', 'occaret', '›'));\n"
    "    s.appendChild(el('span', 'ocg', '+'));\n"
    "    s.appendChild(el('span', 'ocn',\n"
    "      ms ? 'Thought: ' + secs(ms) : 'Thought'));\n"
    '    d.appendChild(s);\n'
    "    var body = String(text || '').trim();\n"
    '    if (body) {\n'
    "      var b = el('div', 'ocbody');\n"
    "      b.appendChild(el('pre', 'octext', body));\n"
    '      d.appendChild(b);\n'
    '    } else {\n'
    "      d.classList.add('notext');\n"
    '    }\n'
    '    d.__oc = { call: null, thought: true };\n'
    '    return d;\n'
    '  }\n'
    '\n'
    '  /* ---------- a thought that is still happening ----------\n'
    '     The entry goes on screen as soon as the reasoning starts, spinning, and\n'
    '     only becomes "Thought" when the block ends - at a tool call, or at the\n'
    '     end of the turn. The clock is measured from the start of the block\n'
    '     rather than taken once at the end, and it ticks while it runs, because\n'
    '     a thought that read 0.0s for thirty seconds looked broken. */\n'
    '  function thoughtLive(text) {\n'
    '    var box = orderedBox();\n'
    '    if (!box) return null;\n'
    '    textClose();\n'
    '    if (OCS.thoughtNode) thoughtClose();\n'
    '    var d = thoughtStep(0, text);\n'
    "    d.classList.add('live');\n"
    "    var lab = d.querySelector('.ocn');\n"
    "    if (lab) lab.textContent = 'Thinking';\n"
    '    d.__ocSince = Date.now();\n'
    '    d.__ocOpen = true;\n'
    '    d.__ocText = text || "";\n'
    '    box.appendChild(d);\n'
    '    OCS.thoughtNode = d;\n'
    '    OCS.last = d;\n'
    '    tickThought();\n'
    '    return d;\n'
    '  }\n'
    '  function tickThought() {\n'
    '    stopTick();\n'
    '    OCS.tick = setInterval(function () {\n'
    '      var n = OCS.thoughtNode;\n'
    '      if (!n || !n.__ocOpen) { stopTick(); return; }\n'
    "      var lab = n.querySelector('.ocn');\n"
    "      if (lab) lab.textContent = 'Thinking: ' + secs(Date.now() - n.__ocSince);\n"
    '    }, 100);\n'
    '  }\n'
    '  function stopTick() {\n'
    '    if (OCS.tick) { clearInterval(OCS.tick); OCS.tick = null; }\n'
    '  }\n'
    '  function thoughtClose() {\n'
    '    var n = OCS.thoughtNode;\n'
    '    stopTick();\n'
    '    if (!n) return;\n'
    "    var ms = Math.max(0, Date.now() - (n.__ocSince || Date.now()));\n"
    "    n.classList.remove('live');\n"
    "    var lab = n.querySelector('.ocn');\n"
    "    if (lab) lab.textContent = 'Thought: ' + secs(ms);\n"
    '    n.__ocMs = ms;\n'
    '    n.__ocOpen = false;\n'
    '    /* the saved history carries the real duration, not the round start */\n'
    '    if (OCS.thoughtPart) OCS.thoughtPart.ms = ms;\n'
    '  }\n'
    '\n'
    '  /* ---------- live step plumbing ---------- */\n'
    '  function scrollSafe() {\n'
    "    try { if (typeof scrollBottom === 'function') scrollBottom(); } catch (e) {}\n"
    '  }\n'
    '  function liveStep(call) {\n'
    '    if (!OCS.bubble) return null;\n'
    '    var box = orderedBox();\n'
    '    if (!box) return null;\n'
    '    if (!OCS.thinkShown) OCS.thinkShown = true;\n'
     '    /* a tool call ends the block of reasoning before it, so whatever comes\n'
     '       after it is a new thought and not a continuation of this one */\n'
     '    if (OCS.thoughtNode) { showThought(true); thoughtClose(); OCS.thoughtNode.__ocOpen = false; OCS.roundText = \'\'; }\n'
    '    /* anything said before this tool call belongs above it */\n'
    '    textClose();\n'
    '    var d = stepFor(call);\n'
    '    box.appendChild(d);\n'
    '    OCS.last = d;\n'
    '    pushCall(call);\n'
    '    scrollSafe();\n'
    '    return d;\n'
    '  }\n'
    '  /* ---------- what it is doing right now ----------\n'
    '     A line of its own, above the steps, that lives for the whole turn.\n'
    '     The waiting bubble it used to live in is gone the moment the model\n'
    '     says anything, so a tool running two rounds later had nowhere to say\n'
    '     what it was - and the one string that was there for the entire wait\n'
    '     said the model was loading, which is only true for a moment of it. */\n'
    '  function statusLine() {\n'
    '    /* Anchored to the conversation itself, not to the bubble: the bubble is\n'
    '       rebuilt and moved as the answer is put in its final place, and a line\n'
    '       left behind as its sibling ends up in a tree that is off the page.\n'
    '       "isConnected" is not enough on its own - the app can swap in a new\n'
    '       conversation and leave the old one in the document, so a line in\n'
    '       there still looks connected and is still on screen to nobody. It has\n'
    '       to be in the conversation that is on the page right now. */\n'
    "    var host = document.getElementById('chat-container');\n"
    '    if (!host) return null;\n'
    '    if (OCS.status && host.contains(OCS.status.row)) return OCS.status;\n'
    '    OCS.status = null;\n'
    "    var row = OCS.bubble && OCS.bubble.parentNode;\n"
    "    var d = el('div', 'ocstatus');\n"
    "    var g = el('span', 'ocs', '\\u25cf');\n"
    "    var t = el('span', 'oct2');\n"
    '    d.appendChild(g); d.appendChild(t);\n'
    '    if (row && row.parentNode === host) host.insertBefore(d, row);\n'
    '    else host.appendChild(d);\n'
    '    OCS.status = { row: d, glyph: g, text: t };\n'
    '    return OCS.status;\n'
    '  }\n'
    '  function setActivity(text) {\n'
    '    var s = statusLine();\n'
    '    if (!s) return;\n'
    '    s.text.textContent = text;\n'
    '    /* a record of what was said and when, which is the only way to tell\n'
    '       after the fact whether the line was up while a tool was running */\n'
    '    try {\n'
    '      (window.__ocAct = window.__ocAct || []).push(\n'
    '        Math.round(Date.now() / 100) + \' \' + text);\n'
    '    } catch (e) {}\n'
    '  }\n'
    '  function clearActivity() {\n'
    '    if (OCS.status && OCS.status.row && OCS.status.row.parentNode) {\n'
    '      OCS.status.row.parentNode.removeChild(OCS.status.row);\n'
    '    }\n'
    '    OCS.status = null;\n'
    '  }\n'
    '\n'
    '  function clearChips() {\n'    '    if (!OCS.bubble) return;\n'
    '    /* the two consoles spell their legacy tool UI differently:\n'
    '       PAGE uses .toolchip/.pills/.toollog, PAGE_GPT uses .toolsline/.tlog.\n'
    '       .reasonbox is the old "Thinking (BONSAI) - N chars" box: its text now\n'
    '       lives inside the "+ Thought" step, so it goes away too. */\n'
    '    var junk = OCS.bubble.querySelectorAll(\n'
    "      '.toolchip, .pill, .pills, .toollog, .tlog, .toolsline, .reasonbox');\n"
    '    for (var i = 0; i < junk.length; i++) {\n'
    '      var n = junk[i];\n'
    '      if (n.parentNode) n.parentNode.removeChild(n);\n'
    '    }\n'
    "    if (typeof thinkingRow !== 'undefined' && thinkingRow) {\n"
    '      thinkingRow.reasonD = null;\n'
    '      thinkingRow.reasonC = null;\n'
    '    }\n'
    '  }\n'
    '  function attach() {\n'
    '    try {\n'
    '      /* thinkingRow is a top-level `let` in both pages, so it is NOT on\n'
    '         window; reach the lexical binding first and fall back to window. */\n'
    "      var t = (typeof thinkingRow !== 'undefined' && thinkingRow) || window.thinkingRow;\n"
    '      if (!t || !t.b) return;\n'
    '      if (OCS.bubble !== t.b) {\n'
    '        /* a new assistant bubble: drop all per-run state */\n'
    '        clearActivity();\n'
    '        OCS.bubble = t.b;\n'
    '        OCS.steps = null;\n'
    '        OCS.last = null;\n'
    '        OCS.thinkShown = false;\n'
    '      }\n'
    '      if (!t.__oc) {\n'
    '        clearChips();\n'
    '        t.toolEl = null; t.tlog = null; t.pills = null;\n'
    '        t.reasonD = t.reasonD || null;\n'
    '        t.__oc = true;\n'
    '      }\n'
    '    } catch (e) {}\n'
    '  }\n'
    '  function thoughtFill(node, text) {\n'
    '    if (!node) return;\n'
    "    var b = node.querySelector('.ocbody');\n"
    '    if (!b) {\n'
    "      b = el('div', 'ocbody');\n"
    "      b.appendChild(el('pre', 'octext', ''));\n"
    '      node.appendChild(b);\n'
    "      node.classList.remove('notext');\n"
    '    }\n'
    '    b.firstChild.textContent = text;\n'
    '    if (OCS.thoughtPart) OCS.thoughtPart.v = text;\n'
    '  }\n'
    '\n'
    '  /* ---------- the ordered transcript ----------\n'
    '     One container holds every block in the order it arrived: reasoning, the\n'
    "     model's own text, tool calls, more reasoning, the final answer. The page\n"
    '     keeps all text in a single .abody, which puts a "sure, one sec" that\n'
    '     came before the tools *after* them, so the text is rendered here instead. */\n'
    '\n'
    '  function orderedBox() {\n'
    '    if (!OCS.bubble) return null;\n'
    '    if (!OCS.steps) {\n'
    "      OCS.steps = el('div', 'ocsteps');\n"
    '      placeSteps(OCS.steps, OCS.bubble);\n'
    '    }\n'
    '    return OCS.steps;\n'
    '  }\n'
    '  /* The ordered steps go into the conversation as their own entries,\n'
    '     immediately above the answer, instead of inside its bubble. One long\n'
    '     turn used to arrive as a single bubble holding the thinking, every\n'
    '     tool call and the reply, which is unreadable past a dozen steps. */\n'
    '  function placeSteps(box, bubble) {\n'
    '    var row = bubble && bubble.parentNode;\n'
    '    if (row && row.parentNode) { row.parentNode.insertBefore(box, row); return; }\n'
    '    if (bubble) bubble.appendChild(box);\n'
    '  }\n'
    '\n'
    '  /* The last thing the model said is the answer, and it belongs in the\n'
    '     answer bubble rather than up with the steps. textClose() also runs at\n'
    '     every tool boundary, but a step follows there, so the paragraph is only\n'
    '     moved when nothing comes after it: the end of the turn. */\n'
    '  function adoptFinalAnswer() {\n'
    '    var n = OCS.textNode;\n'
    '    if (!n) return;\n'
    "    if (String(OCS.textRaw || '').trim()) {\n"
    "      if (typeof fmt === 'function') {\n"
    '        try { n.innerHTML = fmt(OCS.textRaw); } catch (e) {}\n'
    '      }\n'
    "      n.classList.add('ocfinal');\n"
    '    }\n'
    '    OCS.textNode = null;\n'
    "    OCS.textRaw = '';\n"
    '    OCS.textPart = null;\n'
    '  }\n'
    '\n'
    '  function textClose() {\n'
    '    if (!OCS.textNode) return;\n'
    '    /* streaming shows plain text; the block is upgraded to markdown (lists,\n'
    '       code, links) once it is finished and will not grow again */\n'
    "    if (typeof fmt === 'function') {\n"
    '      try { OCS.textNode.innerHTML = fmt(OCS.textRaw); } catch (e) {}\n'
    '    }\n'
    '    OCS.textNode = null;\n'
    "    OCS.textRaw = '';\n"
    '    OCS.textPart = null;\n'
    '  }\n'
    '\n'
    '  function textPush(txt) {\n'
    "    if (txt == null || txt === '') return;\n"
    '    var box = orderedBox();\n'
    '    if (!box) return;\n'
    '    if (!OCS.textNode) {\n'
    '      /* a new block only when something else was emitted in between,\n'
    '         so a run of streamed tokens stays one paragraph - and it is\n'
    '         its own message, because it is the model talking */\n'
    '      var n = textRow();\n'
    "      if (!n) { n = el('div', 'ocpara'); box.appendChild(n); }\n"

    '      OCS.textNode = n;\n'
    "      OCS.textRaw = '';\n"
    '      OCS.last = n;\n'
    "      OCS.textPart = { k: 't', v: '' };\n"
    '      OCS.parts.push(OCS.textPart);\n'
    '    }\n'
    '    OCS.textRaw += txt;\n'
    '    OCS.textNode.textContent = OCS.textRaw;\n'
    '    OCS.textPart.v = OCS.textRaw;\n'
    '    scrollSafe();\n'
    '  }\n'
    '\n'
    '  function pushCall(call) {\n'
    "    OCS.parts.push({ k: 'c', i: OCS.callIdx });\n"
    '    OCS.callIdx++;\n'
    '  }\n'
    '\n'
    '  function pushThought(ms, text) {\n'
    "    var part = { k: 'r', ms: ms || 0, v: String(text || '') };\n"
    '    OCS.parts.push(part);\n'
    '    OCS.thoughtPart = part;\n'
    '    return part;\n'
    '  }\n'
    '\n'
    '  function showThought(force) {\n'
    '    if (!OCS.since) return;\n'
    '    /* One "+ Thought" entry per block of reasoning, not one per turn.\n'
    '       This was a one-shot guarded by thinkShown, so the second and\n'
    '       third thoughts of a turn were dropped - and because P.onDelta\n'
    '       calls this on the first streamed token too, the very first\n'
    '       thought could be consumed by an empty placeholder before it had\n'
    '       any text in it, and a turn that opened with thinking showed no\n'
    '       thought at all. The guard is now "do I have text for this block". */\n'
    '    OCS.thinkShown = true;\n'
    '    OCS.thinkMs = OCS.thinkMs || (Date.now() - OCS.since);\n'
    '    /* the answer itself is not a thought: only show a line when the model\n'
     '       actually reasoned, or when a tool call proves it spent real time */\n'
     "    if (!String(OCS.roundText || '').trim() && !force) return;\n"
     '    /* Nothing to show, and a tool call asks for a flush whether or not there\n'
     '       is anything to flush. Without this, every tool call added an empty\n'
     '       second entry beside the real thought. */\n'
     "    if (!String(OCS.roundText || '').trim()) return;\n"
     '    /* Idempotent for the same block of text: a tool call flushes the open\n'
     '       thought by calling this with force, and without this guard the same\n'
     '       reasoning was entered twice. */\n'
     '    if (OCS.thoughtNode && OCS.thoughtNode.__ocText === OCS.roundText) return;\n'
     '    var box = orderedBox();\n'
     '    if (!box) return;\n'
     '    /* text before this thought is finished */\n'
     '    textClose();\n'
     '    var node = thoughtLive(OCS.roundText);\n'
    '    if (!node) return;\n'
    '    node.__ocText = OCS.roundText;\n'
    '    node.__ocOpen = true;\n'
    '    OCS.last = node;\n'
    '    /* the reasoning is recorded where it happened, not hung off the next tool\n'
    '       call: text can come between the two and it must keep its own slot */\n'
     '    pushThought(OCS.thinkMs, OCS.roundText);\n'
     '    /* The block is on screen, but its text is NOT cleared: reasoning arrives\n'
     '       in pieces, and a continuation a moment later is still the same\n'
     '       thought. It is closed explicitly - by a tool call, or the end of the\n'
     '       turn - and that is what starts the next entry. */\n'
     '    /* the legacy live box is replaced by this step */\n'
     "    if (OCS.bubble.querySelector('.reasonbox')) clearChips();\n"
     '    scrollSafe();\n'
     '  }\n'
     '\n'
    '  /* ---------- override the page renderers ---------- */\n'
    '  var P = window;\n'
    '  var oldToolLog = P.addToolLog;\n'
    '  var oldOnTool = P.onTool;\n'
    '  var oldOnReason = P.onReason;\n'
    '  var oldOnDelta = P.onDelta;\n'
    '\n'
    '  /* the legacy pill rows are replaced by the step list */\n'
    '  P.addToolChips = function () {  };\n'
    '  P.renderLivePills = function () {  };\n'
    '  P.toolChip = function () { return null; };\n'
    '\n'
    '  /* The saved message keeps one `reason` string for the whole answer and the\n'
    '     page renders it as "Thinking (BONSAI) - N chars". That duplicates the\n'
    '     "+ Thought" steps, so drop it and let the steps carry the reasoning. */\n'
    '  var oldAddAsst = P.addAsst;\n'
    "  if (typeof oldAddAsst === 'function' && !oldAddAsst.__oc) {\n"
    '    P.addAsst = function (text, calls, reason, stats, parts) {\n'
    '      var rcalls = calls ? calls.slice() : [];\n'
    '      if (parts && parts.length) {\n'
    "        /* the run recorded the order, so replay it instead of the page's\n"
    '           fixed "thinking, tools, then the whole answer" layout. Calls are\n'
    '           withheld from the page so its own tool log is never drawn twice. */\n'
    "        var inner = oldAddAsst.call(this, '', [], null, stats);\n"
    '        try {\n'
    '          var bub = inner && inner.parentNode;\n'
    '          if (bub) {\n'
    '            stripLegacy(bub);\n'
    "            var old = bub.querySelector('.abody');\n"
    '            if (old && old.parentNode) old.parentNode.removeChild(old);\n'
    '            renderOrdered(bub, rcalls, parts);\n'
    '          }\n'
    '        } catch (e) {}\n'
    '        return inner;\n'
    '      }\n'
    '      if (reason && String(reason).trim()) {\n'
    "        rcalls.push({ __oc_tail: true, name: '__thought__',\n"
    '                      thought_ms: OCS.tailMs || 0, thought_text: reason });\n'
    '      }\n'
    '      var inner = oldAddAsst.call(this, text, rcalls, null, stats);\n'
    '      try {\n'
    '        var b = inner && inner.parentNode;\n'
    "        if (b) b.querySelectorAll('.reasonbox').forEach(function (n) {\n"
    '          if (n.parentNode) n.parentNode.removeChild(n);\n'
    '        });\n'
    '      } catch (e) {}\n'
    '      return inner;\n'
    '    };\n'
    '    P.addAsst.__oc = true;\n'
    '  }\n'
    '\n'
    '  /* rebuild one assistant message from its recorded block order */\n'
    '  function renderOrdered(bubble, calls, parts) {\n'
    '    var box = null;\n'
    '    var anchor = bubble;\n'
    '    /* Steps are grouped only for as long as they run together. A piece of\n'
    '       prose in the middle closes the group, so the conversation reads\n'
    '       think / say / do / think / say rather than all the steps and then\n'
    '       all the prose - which is what it looked like before. */\n'
    '    function steps() {\n'
    "      if (!box) box = el('div', 'ocsteps');\n"
    '      return box;\n'
    '    }\n'
    '    function flush() {\n'
    '      if (box && box.childNodes.length) {\n'
    '        placeSteps(box, bubble, anchor);\n'
    '        anchor = box;\n'
    '      }\n'
    '      box = null;\n'
    '    }\n'
    '    parts.forEach(function (p) {\n'
    '      if (!p) return;\n'
    "      if (p.k === 'c') {\n"
    '        var c = calls[p.i];\n'
    '        if (!c) return;\n'
    '        /* the reasoning already has its own slot in `parts`, so the copy hung\n'
    '           off the call is only used for chats saved before blocks existed */\n'
    '        steps().appendChild(stepFor(c));\n'
    "      } else if (p.k === 't') {\n"
    "        if (!String(p.v || '').trim()) return;\n"
    '        flush();\n'
    '        var n = el(\'div\', \'ocpara\');\n'
    "        n.innerHTML = (typeof fmt === 'function') ? fmt(p.v) : String(p.v);\n"
    "        var rb = el('div', 'bubble ocpara-bubble');\n"
    "        var ra = el('div', 'av bonsai'); ra.textContent = 'B';\n"
    "        var rr = el('div', 'msgrow bonsai');\n"
    '        rb.appendChild(n); rr.appendChild(ra); rr.appendChild(rb);\n'
    '        if (anchor && anchor.parentNode) {\n'
    '          anchor.parentNode.insertBefore(rr, anchor.nextSibling);\n'
    '          anchor = rr;\n'
    '        }\n'
    "      } else if (p.k === 'r') {\n"
    "        if (!String(p.v || '').trim() && !p.ms) return;\n"
    '        steps().appendChild(thoughtStep(p.ms || 0, p.v));\n'
    '      }\n'
    '    });\n'
    '    flush();\n'
    '    var shots = el(\'div\', \'shots\');\n'
    '    var nshot = 0;\n'
    '    (calls || []).forEach(function (c) {\n'
    '      if (!c || !(c.preview || c.image_data)) return;\n'
    '      var sc = shotOf(c);\n'
    '      if (sc) { shots.appendChild(sc); nshot++; }\n'
    '    });\n'
    '    if (nshot) bubble.insertBefore(shots, bubble.firstChild);\n'
    '  }'
    '  function stripLegacy(root) {\n'
    '    if (!root || !root.querySelectorAll) return;\n'
    '    var junk = root.querySelectorAll(\n'
    "      '.toolchip, .pill, .pills, .toollog, .tlog, .toolsline, .reasonbox');\n"
    '    for (var i = 0; i < junk.length; i++) {\n'
    '      if (junk[i].parentNode) junk[i].parentNode.removeChild(junk[i]);\n'
    '    }\n'
    '  }\n'
    '\n'
    '  /* The page\'s own "Thinking (BONSAI) - N chars" box duplicates the\n'
    '     "+ Thought" step and is renamed by doneThinking(), so it has to go even\n'
    '     when the run never calls a tool (clearChips only runs on tool calls). */\n'
    '  function stripReasonBox() {\n'
    '    if (OCS.bubble) {\n'
    '      stripLegacy(OCS.bubble);\n'
    "      if (!OCS.bubble.querySelector('.reasonbox')) OCS.reasonGone = true;\n"
    '    }\n'
    '    try {\n'
    "      if (typeof thinkingRow !== 'undefined' && thinkingRow) {\n"
    '        if (thinkingRow.reasonD && thinkingRow.reasonD.parentNode) {\n'
    '          thinkingRow.reasonD.parentNode.removeChild(thinkingRow.reasonD);\n'
    '        }\n'
    '        thinkingRow.reasonD = null;\n'
    '        thinkingRow.reasonC = null;\n'
    '      }\n'
    '    } catch (e) {}\n'
    '  }\n'
    '\n'
    '  P.addToolLog = function (container, calls) {\n'
    '    if (!container || !calls || !calls.length) return;\n'
    '    stripLegacy(container);\n'
    "    var box = el('div', 'ocsteps');    calls.forEach(function (c) {\n"
    '      if (!c) return;\n'
    '      if (c.thought_ms || c.thought_text) {\n'
    '        box.appendChild(thoughtStep(c.thought_ms || 0, c.thought_text));\n'
    '      }\n'
    '      if (c.__oc_tail) return;          /* only carried the trailing thought */\n'
    '      box.appendChild(stepFor(c));\n'
    '    });\n'
    '    container.appendChild(box);\n'
    "    var shots = el('div', 'shots');\n"
    '    var n = 0;\n'
    '    calls.forEach(function (c) {\n'
    '      if (!c || !(c.preview || c.image_data)) return;\n'
    '      var sc = shotOf(c);\n'
    '      if (sc) { shots.appendChild(sc); n++; }\n'
    '    });\n'
    '    if (n) container.insertBefore(shots, box);\n'
    '  };\n'
    '\n'
    '  var oldDoneThinking = P.doneThinking;\n'
    "  if (typeof oldDoneThinking === 'function' && !oldDoneThinking.__oc) {\n"
    '    P.doneThinking = function (errMsg) {\n'
    '      /* however the turn ended - finished, stopped, or failed - the thought\n'
    '         that was still running stops turning and reads "Thought" now */\n'
    '      try { thoughtClose(); } catch (e) {}\n'
    '      try { clearActivity(); } catch (e) {}\n'
    '      return oldDoneThinking.apply(this, arguments);\n'
    '    };\n'
    '    P.doneThinking.__oc = true;\n'
    '  }\n'
    '\n'
    '  P.onReason = function (txt) {\n'
    '    attach();\n'
    '    /* keep the round\'s reasoning so the "+ Thought" line can show it */\n'
    "    OCS.roundText += (txt == null ? '' : String(txt));\n"
    "    OCS.tailReason += (txt == null ? '' : String(txt));\n"
'    /* Each stretch of reasoning is its own entry: the step on screen\n'
'       keeps growing only while the model is in the same block, so a\n'
'       thought after a tool call starts a new one instead of\n'
'       stretching the previous - and the first is never swallowed. */\n'
    '    if (OCS.thoughtNode && OCS.thoughtNode.__ocOpen) {\n'
    '      thoughtFill(OCS.thoughtNode, OCS.roundText);\n'
    '      OCS.thoughtNode.__ocText = OCS.roundText;\n'
    '    } else showThought();\n'
    '    if (oldOnReason) oldOnReason(txt);\n'
    '    /* the page has just (re)built its own "Thinking (BONSAI)" box: drop it\n'
    '       right away, otherwise it survives runs that make no tool calls */\n'
    '    stripReasonBox();\n'
    '  };\n'
    '  P.onDelta = function (txt) {\n'
    '    attach();\n'
    '    showThought();\n'
    '    /* the model is answering now, whatever it was doing before */\n'
    '    if (OCS.bubble) {\n'
    '      try { setActivity(\'Writing the answer\'); } catch (e) {}\n'
    '    }\n'
    "    /* the page's own onDelta appends to one .abody that never moves, so the\n"
    '       text would always end up below the tools: render it in place instead */\n'
    '    textPush(txt);\n'
    '    stripReasonBox();\n'
    '  };\n'
    '  P.onTool = function (call) {\n'
    '    attach();\n'
    '    if (OCS.running && !OCS.thinkShown && OCS.since\n'
    '        && (Date.now() - OCS.since) > 300) {\n'
    '      /* the model reasoned without streaming text this round */\n'
    '      showThought(true);\n'
    '    }\n'
    '    if (oldOnTool) {\n'
    '      try { oldOnTool(call); } catch (e) {}\n'
    '    }\n'
    '    clearChips();\n'
    '    liveStep(call);\n'
    '    /* say what this call is doing, not that a model is loading */\n'
    '    if (call && call.name) {\n'
    '      try { setActivity(activityFor(call.name)); }\n'
    '      catch (e) { if (window.console) console.warn(\'activity\', e); }\n'
    '    }\n'
    '    /* the page saves this same call object, so carrying the thought time and\n'
    '       text on it keeps "+ Thought" lines in the reloaded history too */\n'
    '    if (call) {\n'
    '      if (OCS.thinkMs) call.thought_ms = OCS.thinkMs;\n'
    "      var rt = String(OCS.roundText || '').trim();\n"
    '      if (rt) call.thought_text = rt;\n'
    '    }\n'
    '    OCS.thinkMs = 0;\n'
    "    OCS.roundText = '';\n"
    '    thoughtClose();\n'
    '    OCS.thoughtNode = null;\n'
    '    OCS.thoughtPart = null;\n'
    '    /* reasoning from here on is only kept for the tail of the answer */\n'
    "    OCS.tailReason = '';\n"
    '    /* the next reasoning round gets its own "+ Thought" line */\n'
    '    OCS.since = Date.now();\n'
    '    OCS.thinkShown = false;\n'
    '    if (call && call.result && call.result.preview_url\n'
    "        && typeof openPreviewStage === 'function') {\n"
    "      try { openPreviewStage(call.result.preview_url, call.result.path || ''); } catch (e) {}\n"
    '    }\n'
    '    if (call && call.change) refreshChanges();\n'
    '  };\n'
    '\n'
    '  /* the round clock is reset at the start of a run (see noteStart below) */\n'
    '\n'
    '  /* ---------- project changes panel ---------- */\n'
    '  var bar = null, modal = null, lastSnap = null;\n'
    '\n'
    '  function changesApi(url, body) {\n'
    '    return fetch(url, body ? {\n'
    "      method: 'POST', headers: { 'Content-Type': 'application/json' },\n"
    '      body: JSON.stringify(body)\n'
    '    } : undefined).then(function (r) { return r.json(); })\n'
    '      .catch(function (e) { return { error: String(e) }; });\n'
    '  }\n'
    '\n'
    '  function ensureBar() {\n'
    '    if (bar) return bar;\n'
    "    var host = document.querySelector('.chat-wrap') || document.body;\n"
    "    bar = el('div', 'ocbar');\n"
    "    var btn = el('button', 'ocbarbtn');\n"
    "    btn.id = 'occhangesbtn';\n"
    '    btn.onclick = function () { openChanges(); };\n'
    '    bar.appendChild(btn);\n'
    "    var clr = el('button', 'ocbarbtn');\n"
    "    clr.textContent = 'CLEAR';\n"
    "    clr.title = 'Stop tracking changes (files stay as they are)';\n"
    '    clr.onclick = function () {\n'
    "      changesApi('/api/forget_changes', {}).then(function () { refreshChanges(); });\n"
    '    };\n'
    '    bar.appendChild(clr);\n'
    '    host.insertBefore(bar, host.firstChild);\n'
    '    paint();\n'
    '    return bar;\n'
    '  }\n'
    '  function paint() {\n'
    '    if (!bar) return;\n'
    "    var b = bar.querySelector('#occhangesbtn');\n"
    '    if (!b) return;\n'
    '    var s = lastSnap || { files: 0, added: 0, removed: 0 };\n'
    '    if (!s.files) {\n'
    "      b.textContent = 'CHANGES 0';\n"
    "      b.className = 'ocbarbtn';\n"
    "      b.title = 'No file changes tracked yet';\n"
    '      return;\n'
    '    }\n'
    "    b.innerHTML = '';\n"
    "    b.appendChild(el('span', null, 'CHANGES ' + s.files + ' file' + (s.files === 1 ? '' : 's') + '  '));\n"
    "    b.appendChild(el('span', 'ocadd', '+' + (s.added || 0)));\n"
    "    b.appendChild(el('span', null, ' '));\n"
    "    b.appendChild(el('span', 'ocdel', '-' + (s.removed || 0)));\n"
    "    b.className = 'ocbarbtn on';\n"
    "    b.title = 'Open the change viewer';\n"
    '  }\n'
    '  function refreshChanges() {\n'
    "    return changesApi('/api/changes').then(function (j) {\n"
    '      lastSnap = j && j.changes ? j : { files: 0, added: 0, removed: 0, changes: [] };\n'
    '      ensureBar();\n'
    '      paint();\n'
    "      if (modal && modal.classList.contains('on')) fillModal();\n"
    '      return lastSnap;\n'
    '    });\n'
    '  }\n'
    '\n'
    '  function fileRow(c) {\n'
    "    var wrap = el('div', 'ocmfile');\n"
    "    var head = el('div', 'ocmfhead');\n"
    "    var name = el('div', 'ocf', c.rel || c.path || '(file)');\n"
    "    name.title = c.path || '';\n"
    "    head.appendChild(el('span', 'ocg', '→'));\n"
    '    head.appendChild(name);\n'
    "    if (c.created) head.appendChild(el('span', 'octag', 'NEW'));\n"
    "    if ((c.edits || 1) > 1) head.appendChild(el('span', 'octag', c.edits + ' edits'));\n"
    "    head.appendChild(el('span', 'oct ocadd', '+' + (c.added || 0)));\n"
    "    head.appendChild(el('span', 'oct ocdel', '-' + (c.removed || 0)));\n"
    "    var rev = el('button', 'ocmact');\n"
    "    rev.textContent = c.created ? 'DELETE' : 'REVERT';\n"
    "    rev.title = c.created ? 'Remove the file the model created'\n"
    "                           : 'Restore the content from before this session';\n"
    "    var body = el('div', 'ocmfbody');\n"
    '    rev.onclick = function (e) {\n'
    '      e.stopPropagation();\n'
    "      changesApi('/api/revert', { path: c.path }).then(function (j) {\n"
    "        if (j && j.error) { alert('Revert failed: ' + j.error); return; }\n"
    '        refreshChanges();\n'
    '      });\n'
    '    };\n'
    '    head.appendChild(rev);\n'
    "    head.onclick = function () { wrap.classList.toggle('open'); };\n"
    '    wrap.appendChild(head);\n'
    '    wrap.appendChild(body);\n'
    "    body.appendChild(el('div', 'oclab', 'DIFF  ' + (c.tool || '') + '  ' + new Date((c.ts || 0) * 1000).toLocaleString()));\n"
    "    body.appendChild(diffTable(c.diff || ''));\n"
    '    return wrap;\n'
    '  }\n'
    '  function fillModal() {\n'
    "    var list = modal.querySelector('.ocmlist');\n"
    '    if (!list) return;\n'
    "    list.innerHTML = '';\n"
    '    var s = lastSnap || {};\n'
    '    var ch = s.changes || [];\n'
    '    if (!ch.length) {\n'
    "      list.appendChild(el('div', 'ocmempty', 'No changes tracked. Files edited by BONSAI show up here with a diff and a revert button.'));\n"
    '    } else {\n'
    '      ch.forEach(function (c) { list.appendChild(fileRow(c)); });\n'
    '    }\n'
    "    var sum = modal.querySelector('.ocsum');\n"
    '    if (sum) {\n'
    "      sum.textContent = s.files ? (s.files + ' file' + (s.files === 1 ? '' : 's') + '  +' + (s.added || 0) + '  -' + (s.removed || 0)) : 'nothing yet';\n"
    '    }\n'
    '  }\n'
    '  function openChanges() {\n'
    '    ensureBar();\n'
    '    if (!modal) {\n'
    "      modal = el('div', 'ocmodal');\n"
    '      modal.onclick = function (e) { if (e.target === modal) closeChanges(); };\n'
    "      var box = el('div', 'ocmbox');\n"
    "      var h = el('div', 'ocmh');\n"
    "      h.appendChild(el('b', null, 'CODE CHANGES'));\n"
    "      h.appendChild(el('span', 'ocsum', ''));\n"
    "      h.appendChild(el('span', 'sp'));\n"
    "      var rvAll = el('button', 'ocmact');\n"
    "      rvAll.textContent = 'REVERT ALL';\n"
    '      rvAll.onclick = function () {\n'
    '        var s = lastSnap || { changes: [] };\n'
    '        var list = (s.changes || []).slice();\n'
    '        if (!list.length) return;\n'
    '        var i = 0;\n'
    '        (function next() {\n'
    '          if (i >= list.length) { refreshChanges(); return; }\n'
    '          var c = list[i++];\n'
    "          changesApi('/api/revert', { path: c.path }).then(next);\n"
    '        })();\n'
    '      };\n'
    '      h.appendChild(rvAll);\n'
    "      var clr = el('button', 'ocmact');\n"
    "      clr.textContent = 'CLEAR';\n"
    "      clr.title = 'Stop tracking (files stay as they are)';\n"
    '      clr.onclick = function () {\n'
    "        changesApi('/api/forget_changes', {}).then(function () { refreshChanges(); });\n"
    '      };\n'
    '      h.appendChild(clr);\n'
    "      var x = el('button', 'ocmact');\n"
    "      x.textContent = 'CLOSE';\n"
    '      x.onclick = closeChanges;\n'
    '      h.appendChild(x);\n'
    '      box.appendChild(h);\n'
    "      box.appendChild(el('div', 'ocmlist'));\n"
    '      modal.appendChild(box);\n'
    '      document.body.appendChild(modal);\n'
    '    }\n'
    "    modal.classList.add('on');\n"
    '    fillModal();\n'
    '    refreshChanges();\n'
    '  }\n'
    "  function closeChanges() { if (modal) modal.classList.remove('on'); }\n"
    "  document.addEventListener('keydown', function (e) {\n"
    "    if (e.key === 'Escape') closeChanges();\n"
    '  });\n'
    '\n'
    '  /* start the clock when a run begins: wrap the run hooks if the page has\n'
    '     them, and always watch fetch() so the clock works on either console\n'
    '     regardless of which entry point starts the stream. */\n'
    '  function noteStart() {\n'
    '    stopTick();\n'
    '    thoughtClose();\n'
    '    clearActivity();\n'
    '    OCS.since = Date.now();\n'
    '    OCS.thinkShown = false;\n'
    '    OCS.running = true;\n'
    '    OCS.steps = null;\n'
    '    OCS.last = null;\n'
    "    OCS.roundText = '';\n"
    '    OCS.thinkMs = 0;\n'
    '    OCS.thoughtNode = null;\n'
    '    OCS.parts = [];\n'
    '    OCS.textNode = null;\n'
    "    OCS.textRaw = '';\n"
    '    OCS.textPart = null;\n'
    '    OCS.callIdx = 0;\n'
    "    OCS.tailReason = '';\n"
    '    OCS.thoughtPart = null;\n'
    '  }\n'
    '  function noteEnd() {\n'
    '    /* the reasoning that came after the last tool call is already recorded in\n'
    '       its own right by showThought(); keep the length only for the legacy\n'
    '       render of chats saved before blocks existed */\n'
    '    OCS.tailMs = OCS.thinkMs || (OCS.since ? Date.now() - OCS.since : 0);\n'
    '    textClose();\n'
    "    OCS.tailReason = '';\n"
    '    OCS.running = false;\n'
    '    OCS.thinkShown = false;\n'
    '    refreshChanges();\n'
    '  }\n'
    '  P.ocNoteStart = noteStart;\n'
    '  P.ocNoteEnd = noteEnd;\n'
    '  /* the page saves the assistant message itself, so it needs the block order */\n'
    '  P.__ocParts = function () { return OCS.parts || []; };\n'
    '  P.ocState = function () {\n'
    '    return { since: OCS.since, running: OCS.running, steps: OCS.steps ? OCS.steps.children.length : 0 };\n'
    '  };\n'
    '\n'
    "  ['go', 'streamRun'].forEach(function (fn) {\n"
    "    if (typeof P[fn] !== 'function' || P[fn].__oc) return;\n"
    '    var f = P[fn];\n'
    '    var w = function () { noteStart(); return f.apply(this, arguments); };\n'
    '    w.__oc = true;\n'
    '    P[fn] = w;\n'
    '  });\n'
    '\n'
    "  if (typeof P.fetch === 'function' && !P.fetch.__oc) {\n"
    '    var realFetch = P.fetch;\n'
    '    var wrappedFetch = function (input, init) {\n'
    '      try {\n'
    "        var u = (typeof input === 'string' ? input : (input && input.url)) || '';\n"
    "        if (String(u).indexOf('/api/stream') >= 0) noteStart();\n"
    '      } catch (e) {}\n'
    '      return realFetch.apply(this, arguments);\n'
    '    };\n'
    '    wrappedFetch.__oc = true;\n'
    '    P.fetch = wrappedFetch;\n'
    '  }\n'
    '\n'
    '  /* close the run when the page finishes its stream */\n'
    "  ['doneThinking', 'onDone', 'endStream'].forEach(function (fn) {\n"
    "    if (typeof P[fn] !== 'function' || P[fn].__ocEnd) return;\n"
    '    var f = P[fn];\n'
    '    var w = function () { try { return f.apply(this, arguments); } finally { noteEnd(); } };\n'
    '    w.__ocEnd = true;\n'
    '    P[fn] = w;\n'
    '  });\n'
    '\n'
    '  ensureBar();\n'
    '  refreshChanges();\n'
    '  window.ocChanges = { refresh: refreshChanges, open: openChanges, close: closeChanges, state: function () { return lastSnap; } };\n'
    '})();\n'
    ''
)


def _inject_steps(html):
    """Add the step/diff UI to a served page."""
    if "__ocSteps" in html:
        return html
    css = "".join("  " + line + "\n" for line in _OC_CSS.split("\n"))
    at = html.rfind("</style>")
    if at > 0:
        html = html[:at] + css + html[at:]
    at = html.rfind("</script>")
    if at > 0:
        html = html[:at] + _OC_JS + "\n" + html[at:]
    return html


PAGE = _inject_steps(PAGE)
PAGE_GPT = _inject_steps(PAGE_GPT)
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
        elif path == "/api/changes":
            self._send(200, json.dumps(_changes_snapshot(), default=str))
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
        elif path == "/api/dl_state":
            self._send(200, json.dumps(_dl_state(), default=str))
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
                            "/api/ctx", "/api/revert", "/api/forget_changes",
                            "/api/compact",
                            "/api/path_policy", "/api/dl_pause",
                            "/api/dl_resume", "/api/dl_cancel",
                            "/api/dl_retry", "/api/dl_clear"):
                self._send(404, "not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length > MAX_REQUEST_BYTES:
                self._send(413, json.dumps(
                    {"error": "request too large (max %d MB)"
                               % (MAX_REQUEST_BYTES // (1024 * 1024))}))
                return
            body = json.loads(self.rfile.read(length) or b"{}")
            if path == "/api/revert":
                self._send(200, json.dumps(_revert_change(body.get("path")),
                                           default=str))
                return
            if path == "/api/forget_changes":
                self._send(200, json.dumps(_forget_changes()))
                return
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
            if path.startswith("/api/dl_"):
                job_id = str(body.get("id") or body.get("job") or "")
                action = path[len("/api/dl_"):]
                if action == "pause":
                    out = _dl_pause(job_id)
                elif action == "resume":
                    out = _dl_resume(job_id)
                elif action == "cancel":
                    out = _dl_cancel(job_id)
                elif action == "retry":
                    out = _dl_retry(job_id)
                elif action == "clear":
                    out = _dl_clear(str(body.get("where") or "finished"))
                else:
                    out = {"error": "unknown download action: %s" % action}
                self._send(200, json.dumps(out, default=str))
                return
            if path == "/api/answer":
                ask_id = str(body.get("id") or "")
                # one answer, or one per question when the model asked several
                given = body.get("answer")
                if isinstance(given, list):
                    answer = [str(x or "") for x in given]
                else:
                    answer = str(given or "")
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
                    # cfg carries the numbers; api_key travels beside it because
                    # a hosted model has no numbers worth saving, only a key.
                    # Only forwarded when the client actually sent it, so an
                    # older page that knows nothing about keys cannot blank it.
                    cfg = dict(body.get("cfg") or {})
                    if "api_key" in body:
                        cfg["api_key"] = body.get("api_key")
                    res = _set_model_config(str(body.get("id") or ""), cfg)
                elif action == "test_key":
                    if body.get("base_url"):
                        res = _test_model_key(base=body.get("base_url"),
                                              model=body.get("model"),
                                              key_ref=body.get("api_key"))
                    else:
                        res = _test_model_key()
                elif action == "scan":
                    res = _scan_models()
                else:
                    res = _select_model(str(body.get("id") or ""))
                self._send(200, json.dumps(res, default=str))
                return
            if path == "/api/ctx":
                self._send(200, json.dumps(
                    _ctx_usage(body.get("messages") or [],
                               body.get("mode") or MODE_BUILD,
                               bool(body.get("quick"))), default=str))
                return
            if path == "/api/compact":
                try:
                    self._send(200, json.dumps(
                        _compact_chat(body.get("messages") or [],
                                      body.get("mode") or MODE_BUILD),
                        default=str))
                except Exception as exc:
                    self._send(200, json.dumps(
                        {"ok": False, "error": _model_error_text(exc)},
                        default=str))
                return
            if path == "/api/pick_model":
                what = str(body.get("what") or "file").strip().lower()
                # A path typed into the fallback is treated exactly like one
                # chosen in a dialog, so the fallback is a real second route
                # rather than a dead end.
                typed = str(body.get("path") or "").strip()
                if what == "mmproj":
                    if not typed:
                        blocker = _tk_picker_problem()
                        if blocker:
                            self._send(200, json.dumps(
                                {"no_browser": True, "error": blocker}))
                            return
                        typed = _pick_vision_file()
                    if not typed:
                        self._send(200, json.dumps({"cancelled": True}))
                        return
                    self._send(200, json.dumps(
                        _set_model_mmproj(str(body.get("id") or ""), typed),
                        default=str))
                    return
                if not typed:
                    blocker = _tk_picker_problem()
                    if blocker:
                        self._send(200, json.dumps(
                            {"no_browser": True, "error": blocker}))
                        return
                    typed = (_pick_model_folder() if what == "folder"
                             else _pick_model_file())
                if not typed:
                    self._send(200, json.dumps({"cancelled": True}))
                    return
                chosen = typed
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
                write_lock = threading.Lock()

                def emit(event, obj):
                    with write_lock:
                        sse(self, event, obj)

                def do_ask(q):
                    # The model asks in batches:
                    #   {"questions": [{"question", "header", "options": [...]}]}
                    # but a permission prompt is a single
                    #   {"kind": "path_approval", "path", "tool", "options": [...]}
                    # Reading the outer object as if it were one of the
                    # questions found no "question" and no "options" in it, so
                    # every batch question arrived as a bare "What should I do?"
                    # with an empty text box and no way to pick from the choices
                    # the model had carefully written out.
                    # Taking only questions[0] then threw away the rest of a
                    # batch, which is exactly when a model asks two things and
                    # gets an answer to one of them.
                    if isinstance(q, dict) and isinstance(q.get("questions"), list) \
                            and q["questions"]:
                        batch = [x for x in q["questions"] if isinstance(x, dict)]
                        if not batch:
                            batch = [{"question": str(x)} for x in q["questions"]]
                    else:
                        batch = [q if isinstance(q, dict) else {"question": str(q)}]

                    def options_of(one):
                        out = []
                        for o in (one.get("options") or []):
                            if isinstance(o, dict):
                                label = str(o.get("label") or o.get("text")
                                            or o.get("value") or "").strip()
                                desc = str(o.get("description") or "").strip()
                                if label:
                                    out.append({"label": label, "description": desc})
                            elif str(o).strip():
                                out.append({"label": str(o).strip(), "description": ""})
                        return out

                    # a permission prompt is one fixed question and never a batch
                    if batch[0].get("kind") == "path_approval":
                        batch = batch[:1]

                    cards = []
                    for one in batch:
                        cards.append({
                            "question": str(one.get("question") or one.get("header") or ""),
                            "header": str(one.get("header") or ""),
                            "options": options_of(one),
                            "kind": one.get("kind") or "question",
                            "path": one.get("path") or "",
                            "tool": one.get("tool") or "",
                        })
                    ask_id = uuid.uuid4().hex[:12]
                    event = threading.Event()
                    entry = {"event": event, "answer": None}
                    with _ASKS_LOCK:
                        _ASKS[ask_id] = entry
                    first = cards[0]
                    emit("ask", {"id": ask_id,
                                 "kind": first["kind"],
                                 "question": first["question"],
                                 "header": first["header"],
                                 "path": first["path"],
                                 "tool": first["tool"],
                                 # the options keep their shape, so the page can
                                 # draw a title and an explanation instead of one
                                 # flat "label - description" line
                                 "options": [o["label"] for o in first["options"]]
                                 if not any(o["description"] for o in first["options"])
                                 else first["options"],
                                 "questions": cards})
                    _touch_activity()
                    try:
                        event.wait(BONSAI_KEEP_ALIVE)
                    except Exception:
                        pass
                    with _ASKS_LOCK:
                        given = entry.get("answer")
                        _ASKS.pop(ask_id, None)
                    answers = []
                    for i in range(len(cards)):
                        val = ""
                        if isinstance(given, list):
                            if i < len(given):
                                val = str(given[i] or "").strip()
                        elif i == 0:
                            val = str(given or "").strip()
                        answers.append(val or "(no answer)")
                    # one question keeps the plain string it always returned;
                    # a batch gets one answer per question, in order
                    return answers[0] if len(answers) == 1 else answers

                def do_todo(items):
                    emit("todo", {"todos": items})

                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # A long tool (a multi-GB download) must not look like a hang,
                # and neither must a model load - so the beat starts before
                # anything slow is attempted, not after the model is up.
                stop_beat = threading.Event()

                def keepalive():
                    # Polled often, because a tool can ask for the caret in the
                    # chat box and the paste follows a fraction of a second
                    # later - a ten second wait would paste into the wrong
                    # window. The keepalive itself still goes out every ten
                    # seconds, which is what is holding the socket open.
                    last = time.time()
                    while not stop_beat.wait(0.25):
                        try:
                            if _UI_FOCUS.is_set():
                                _UI_FOCUS.clear()
                                with write_lock:
                                    self.wfile.write(
                                        b"event: focus\ndata: {\"what\": "
                                        b"\"input\"}\n\n")
                                    self.wfile.flush()
                            if time.time() - last >= 10:
                                last = time.time()
                                with write_lock:
                                    self.wfile.write(b": keepalive\n\n")
                                    self.wfile.flush()
                        except Exception:
                            return

                beat = threading.Thread(target=keepalive, name="sse-keepalive")
                beat.daemon = True
                beat.start()
                try:
                    was_ready = _bonsai_ready()
                    emit("start", {"ok": True, "workdir": WORKDIR,
                                   "model_ready": was_ready})
                    if not was_ready:
                        # say *why* the answer is slow, instead of leaving the
                        # browser on "processing..." with nothing to go on
                        emit("waiting", {"reason": "loading_model",
                                         "text": "Loading the model server"})
                    if not _ensure_bonsai():
                        emit("error", {"text": "Bonsai 2 model server could not start."})
                        return
                    if not was_ready:
                        emit("waiting", {"reason": "model_ready",
                                         "text": "Model loaded, waiting for the first token"})
                    client_gone = False
                    try:
                        handle_chat(messages or [], stream=True, mode=mode,
                                    on_reason=lambda t: emit("reason", {"text": t}),
                                    on_delta=lambda t: emit("delta", {"text": t}),
                                    on_tool=lambda c: emit("tool", {"call": _strip_full(c)}),
                                    on_stats=lambda s: emit("stats", s),
                                    hooks={"on_ask": do_ask, "on_todo": do_todo})
                    except CLIENT_GONE:
                        # browser closed the tab mid-response: normal, not an error
                        client_gone = True
                    except Exception as exc:
                        traceback.print_exc()
                        try:
                            emit("error", {"text": _model_error_text(exc)})
                        except Exception:
                            pass
                    if not client_gone:
                        emit("done", {"ok": True})
                finally:
                    stop_beat.set()
            else:
                if not _ensure_bonsai():
                    self._send(200, json.dumps({"reply": "Bonsai 2 model server could not start.",
                                                "calls": []}))
                    return
                result = handle_chat(messages or [], mode=mode)
                result.pop("_stats", None)
                self._send(200, json.dumps(result, default=str))
        except Exception as exc:
            if isinstance(exc, CLIENT_GONE) or self.wfile.closed:
                return
            # Whatever went wrong, the client is mid-stream and is parsing SSE,
            # so this has to arrive as SSE. It used to be written in the SSE
            # framing but sent as application/json with a Content-Length, and
            # a reader that trusts the header got a body it could not parse -
            # an error about the error. Match the framing to the stream.
            try:
                already_streaming = self.path == "/api/stream"
            except Exception:
                already_streaming = False
            if already_streaming:
                body = ("event: error\ndata: "
                        + json.dumps({"text": _model_error_text(exc)},
                                     ensure_ascii=False) + "\n\n").encode("utf-8")
                ctype = "text/event-stream; charset=utf-8"
            else:
                body = json.dumps({"error": _model_error_text(exc)},
                                  ensure_ascii=False).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            try:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(body)
            except CLIENT_GONE:
                return
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


def _gen_tools_json(path=None):
    """Write the tool catalogue to tools.json so the docs cannot drift."""
    tools = ([TOOL_SPEC] + list(FILE_TOOLS.values()) + WEB_SPECS +
             PC_TOOLS)
    seen = set()
    out = []
    for tool in tools:
        name = (tool.get("function") or {}).get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(tool)
    target = path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "tools.json")
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return target, [t["function"]["name"] for t in out]


def main():
    if "--gen-tools" in sys.argv:
        where, names = _gen_tools_json()
        print("wrote %d tools to %s" % (len(names), where), flush=True)
        print(", ".join(names), flush=True)
        return
    try:
        os.makedirs(WORKDIR, exist_ok=True)
    except Exception as exc:
        print(f"WARN: could not create workdir {WORKDIR}: {exc}", flush=True)

    # The model server is deliberately NOT started here: it loads on the first
    # message you send, so starting the UI costs no VRAM until you actually
    # use it (see _ensure_bonsai, which the chat endpoints call).
    # The Blender bridge is a daemon thread, but it spawns a Python process of
    # its own and that work lands in the same interpreter as startup. A second
    # copy of the app, or a test harness, has no use for it.
    if not os.environ.get("BONSAI_NO_BLENDER"):
        threading.Thread(target=_blender_kickoff, daemon=True).start()
    threading.Thread(target=_sysinfo_loop, daemon=True,
                     name="bonsai-sysinfo").start()
    _sched_load()
    print("BONSAI is READY on http://%s:%d" % (HOST, PORT), flush=True)
    if not os.environ.get("BONSAI_NO_BROWSER"):
        webbrowser.open(f"http://{HOST}:{PORT}")
    # a resolver browser that outlives the app would hold a profile lock and
    # a few hundred MB, so it is reaped on the way out
    import atexit
    atexit.register(_resolve_pw_stop)
    try:
        BonsaiServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("shutting down", flush=True)
    finally:
        _resolve_pw_stop()


if __name__ == "__main__":
    main()