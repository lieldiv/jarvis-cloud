"""
J.A.R.V.I.S backend — Flask + Groq + edge-tts

Major changes from the original version:
  1. SECURITY: no hardcoded API key fallback, debug mode off by default,
     server binds to localhost only unless explicitly opted into LAN access,
     and app launching no longer runs through a shell (removes command injection).
  2. BRAIN: intent detection is no longer "if 'weather' in text" string matching.
     The LLM is given real tools (function calling) and decides which to call,
     so it understands phrasing the old keyword matching would miss entirely
     (and doesn't misfire on words like "closed" containing "close").
  3. MEMORY: the assistant now keeps short-term conversation history, so
     follow-up questions ("what about tomorrow?") work.
  4. RELIABILITY: every external call (weather, TTS, subprocess, pywhatkit)
     is wrapped so one failure returns a spoken error instead of crashing
     the request or leaving the user with a silent HUD.
  5. NEW TOOLS: play_on_spotify (real Spotify Web API playback, not just
     opening the app), draw_shape_in_paint (mouse automation to draw a
     circle), and calculate (safe AST-based arithmetic, typed into the
     Calculator app so it's visible on screen). See README for setup
     and known limitations of each.
"""

import ast
import asyncio
import json
import logging
import operator
import os
import queue
import re
import secrets
import socket
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# --- Resolve the app's real "home" directory and make it the working
# directory, BEFORE any other local import runs --------------------------
# Everything below this line that opens a relative path — load_dotenv()'s
# .env, jarvis.log, .google_cache/, .ms_cache.bin, .spotify_cache, and
# cert_bootstrap's .certs/ bundle — assumes cwd is "the folder this app
# lives in". That's true by luck when a developer runs `python app.py`
# from inside the project folder, but NOT guaranteed for:
#   - a packaged PyInstaller .exe launched by double-click (Explorer sets
#     cwd to the .exe's folder, but a desktop shortcut with a different
#     "Start in" value, or a scheduled task, can set cwd to anywhere), or
#   - `python C:\path\to\app.py` run from a different directory.
# Pinning cwd explicitly here removes that whole class of "works on my
# machine, mysteriously can't find .env on this one" bug reports.
if getattr(sys, "frozen", False):
    # PyInstaller sets sys.frozen=True and sys.executable to the .exe path
    # itself (not the Python interpreter) — its folder is where .env,
    # .certs/, and the cache files are expected to live alongside it.
    _APP_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _APP_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(_APP_DIR)

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below
import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
from groq import APIConnectionError, BadRequestError, Groq, RateLimitError

from tts import generate_tts_base64

# Optional, Windows-desktop-automation-only imports. These are guarded so the
# server still boots (e.g. for testing on another OS) even if one is missing.
try:
    import pyautogui
except Exception:
    pyautogui = None

try:
    import pywhatkit
except Exception:
    pywhatkit = None

try:
    import pygetwindow as gw
except Exception:
    gw = None

try:
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth
except Exception:
    spotipy = None

# Explicit path (not the bare load_dotenv() default) so this is immune to
# python-dotenv's own cwd/caller-frame search heuristics, which behave
# inconsistently inside a frozen PyInstaller executable — _APP_DIR/cwd was
# already pinned above, so just use it directly.
load_dotenv(os.path.join(_APP_DIR, ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("jarvis.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("jarvis")

# --- Computer-use / self-healing / sandboxing additions ---------------------
from guardrails import refuse_if_elevated, ensure_workspace, resolve_confirmation, get_pending_meta
import file_tools
import vision_action
import self_healing
import event_stream

refuse_if_elevated()   # exits the process if launched as admin/root
ensure_workspace()
# -----------------------------------------------------------------------------

# --- Schedule / email management (the assistant's central use case) --------
import productivity_service
import daily_briefing
import google_service
# -----------------------------------------------------------------------------

app = Flask(__name__)
# MULTI-USER BUILD: this key now signs actual persistent login sessions
# (session["user_id"]), not just the few-seconds-long OAuth CSRF state the
# single-tenant version used it for — a random fallback per process restart
# would silently log every signed-in user out on every redeploy/container
# restart, which on a host like Render can happen often. FLASK_SECRET_KEY
# must be set as a real, stable secret in the hosting platform's env vars;
# the random fallback stays only so local dev without a .env entry doesn't
# hard-crash, but it means local sessions won't survive a restart either.
_secret_key_env = os.environ.get("FLASK_SECRET_KEY")
if not _secret_key_env:
    logger.warning(
        "FLASK_SECRET_KEY isn't set — using a random key for this process only. "
        "Every signed-in user will be logged out on the next restart. Set a "
        "stable FLASK_SECRET_KEY before deploying."
    )
app.secret_key = _secret_key_env or secrets.token_hex(32)
app.permanent_session_lifetime = timedelta(days=30)
# Without this, Flask caches templates/index.html in memory the first time
# it's rendered (outside debug mode) — edits to the HUD's HTML/CSS/JS would
# silently not show up until the process was restarted.
app.config["TEMPLATES_AUTO_RELOAD"] = True

# ---------------------------------------------------------------------------
# Groq client setup
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
if not GROQ_API_KEY:
    # SystemExit (unlike RuntimeError) prints just this message to stderr,
    # no Python traceback — much less alarming for a first run, especially
    # from a packaged .exe where a traceback looks like a crash/bug report.
    raise SystemExit(
        "\n[J.A.R.V.I.S cannot start]\n"
        "GROQ_API_KEY is not set.\n\n"
        "Copy .env.example to .env (same folder as this program) and put your "
        "key in it, or set the GROQ_API_KEY environment variable before running.\n"
        "Get a free key at https://console.groq.com/keys\n"
    )

groq_client = Groq(api_key=GROQ_API_KEY)

# llama-3.3-70b-versatile (used in the original script) was deprecated by Groq
# on 2026-06-17 and is scheduled to shut down soon after. openai/gpt-oss-120b is
# Groq's recommended replacement and supports tool calling, but its free-tier
# rate limit is tight enough that a handful of commands in quick succession
# (each already 2-3 Groq calls internally — see MAX_TOOL_ROUNDS) exhausts it
# fast. openai/gpt-oss-20b is faster and gets a much more generous free-tier
# budget, at a small capability cost this assistant's fairly simple
# tool-calling/conversation use case doesn't really need the 120b for.
MODEL_NAME = "openai/gpt-oss-20b"

MAX_TOOL_ROUNDS = 3          # safety cap on chained tool calls per request
MAX_HISTORY_MESSAGES = 24    # ~12 user/assistant turns of memory
# gpt-oss models spend tokens on a hidden "analysis" channel before the
# final answer/tool-call JSON, and Groq counts that against max_tokens too.
# 200 was too tight for multi-field tool calls (e.g. create_calendar_event
# has 6 args) — the JSON got cut off mid-object, which Groq reports back as
# a 400 "tool_use_failed" instead of just truncating gracefully.
MAX_TOKENS = 1024

# The model has no built-in notion of "now" — without this it either guesses
# a date or, worse, takes the user's spoken clock time (their local time) and
# stamps it with a 'Z' (UTC) suffix unchanged, silently shifting every
# calendar event by the local UTC offset. Injected fresh into every request
# in run_llm() below rather than baked into the static SYSTEM_PROMPT.
LOCAL_TZ = ZoneInfo(os.environ.get("JARVIS_TIMEZONE", "Asia/Jerusalem"))


def _time_context() -> str:
    now = datetime.now(LOCAL_TZ)
    offset = now.strftime("%z")  # e.g. "+0300"
    offset = f"{offset[:3]}:{offset[3:]}"  # -> "+03:00"
    return (
        f"Current date and time: {now.strftime('%A, %d %B %Y, %H:%M')} "
        f"({LOCAL_TZ.key}, UTC{offset}). Resolve relative dates/times ('today', "
        "'tomorrow', 'in 2 hours', 'next Tuesday') against this. When calling "
        "create_calendar_event, write start_iso/end_iso as local wall-clock time "
        f"with this exact offset, e.g. '2026-08-05T14:00:00{offset}' for 2pm — "
        "never convert to UTC/'Z' yourself, the offset above already tells the "
        "calendar which zone that clock time is in."
    )


# Multi-user build: one history per signed-in user, keyed by their Google
# account id. In-memory (not in users.db) — losing chat history on a server
# restart is an acceptable phase-1 trade-off; calendar/mail tokens (the data
# that actually matters) are the persisted part.
_conversation_histories: dict[str, list] = {}


def _history_for(user_id: str) -> list:
    return _conversation_histories.setdefault(user_id, [])

# Multi-user cloud build: pyautogui/os.startfile/taskkill only make sense
# controlling a real Windows desktop, and there is none here — no single
# user's phone/browser session has "a desktop" for this server to touch.
# Defaults to enabled so local Windows dev/testing of this same codebase
# still works unchanged; the deployed cloud host sets this to "false".
DESKTOP_TOOLS_ENABLED = os.environ.get("JARVIS_DESKTOP_TOOLS", "true").lower() == "true"

# The open_application/computer_use paragraph below describes tools that
# ACTIVE_TOOLS (further down) omits entirely when DESKTOP_TOOLS_ENABLED is
# false — no point spending tokens telling the model how to use tools it
# was never actually given, on every single request.
_DESKTOP_CONTROL_GUIDANCE = (
    " Never use computer_use for Spotify playback. You control this "
    "computer the way a person does otherwise: open_application launches "
    "an app, and computer_use then takes a screenshot and clicks/types/"
    "drags inside it — use computer_use only for on-screen actions that "
    "have no dedicated tool (clicking a button, filling a form, drawing "
    "something, browser navigation). IMPORTANT: opening an app is rarely "
    "the whole job — if the user's request implies doing something inside "
    "the app and no dedicated tool covers it, you MUST call computer_use "
    "as a follow-up after open_application returns, in the same turn. "
    "Never treat 'I opened it' as a finished task when the user asked for "
    "an action inside it."
    if DESKTOP_TOOLS_ENABLED else
    " There's no Windows desktop for you to control in this deployment — "
    "don't offer to open or click around inside desktop applications."
)

SYSTEM_PROMPT = (
    "You are J.A.R.V.I.S., Tony Stark's AI assistant. Respond in concise, "
    "polished British English, and address the user as 'sir'. Keep answers "
    "under 30 words unless the user clearly asks for detail. The user may "
    "speak or type to you in Hebrew — understand it, but always reply in "
    "English.\n\n"
    "Your central job is managing the user's schedule and inbox — treat "
    "get_daily_agenda, get_calendar_events, and list_emails as your default "
    "tools whenever the user asks anything about their day, what's coming "
    "up, or what's in their inbox, even loosely phrased ('what's on my "
    "plate', 'anything from Dana'). You may propose creating an event or "
    "sending an email (create_calendar_event, send_email), but these only "
    "ever register a confirmation request — you cannot act on the user's "
    "calendar or mailbox without them explicitly approving it afterward. "
    "Always phrase these as offers ('Shall I add that, sir?'), never as "
    "already-done. set_reminder is different — it executes immediately, no "
    "approval needed, since it's a private note-to-self rather than an "
    "action on the user's real calendar/mailbox; use it whenever they ask "
    "to be reminded of something later.\n\n"
    "Beyond that, you are a general-purpose agent for whatever the user "
    "needs. For Spotify specifically, ALWAYS use play_on_spotify to play or "
    "search for a song — it controls real playback via the Spotify API "
    "(opens the app itself if needed) and is far more reliable than "
    "clicking around the screen." + _DESKTOP_CONTROL_GUIDANCE +
    " Also available: get_weather, "
    "web_search (opens results in the browser), play_media (YouTube, for "
    "non-Spotify video/music), and calculate. Use write_and_test_code when "
    "asked to write and verify a script. Call the right tool instead of "
    "guessing or saying you can't; if a command doesn't match any tool, "
    "just respond conversationally."
)

# Spotify API config — see README for how to create these credentials. Both
# must be set for Spotify control to work; the tool degrades gracefully
# (spoken message, no crash) if they're missing.
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SPOTIFY_SCOPE = "user-modify-playback-state user-read-playback-state"

# Paint automation config — these are RELATIVE (0.0-1.0) coordinates within
# the maximized Paint window, not pixels. They're a starting guess, not a
# guarantee: the exact ribbon position depends on your Windows/Paint version
# and DPI scaling. Run `python calibrate_paint.py` and adjust these if the
# click misses the Ellipse tool or the canvas.
PAINT_ELLIPSE_TOOL_REL = (0.435, 0.135)   # Home ribbon -> Shapes -> Ellipse
PAINT_CANVAS_START_REL = (0.35, 0.45)     # where the drag starts
PAINT_SIZE_OFFSETS = {                    # drag distance, added to the start point
    "small": (0.08, 0.08),
    "medium": (0.18, 0.18),
    "large": (0.30, 0.30),
}

# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------
# App launching used to be `subprocess.Popen(f"start {app_name}", shell=True)`,
# which runs the raw string through cmd.exe — anything typed or transcribed
# containing `&`, `|`, or `;` would be interpreted as a second command. The
# map below launches known apps directly (shell=False), and os.startfile is
# used for anything else since it goes through ShellExecute, not a shell.

APP_MAP = {
    "chrome": {"type": "url", "target": "https://www.google.com"},
    "google chrome": {"type": "url", "target": "https://www.google.com"},
    "spotify": {"type": "startfile", "target": "spotify:"},
    "notepad": {"type": "exe", "target": "notepad.exe"},
    "calculator": {"type": "exe", "target": "calc.exe"},
    "calc": {"type": "exe", "target": "calc.exe"},
    "paint": {"type": "exe", "target": "mspaint.exe"},
    "explorer": {"type": "exe", "target": "explorer.exe"},
    "file explorer": {"type": "exe", "target": "explorer.exe"},
}

# Only apps we can name an exact process for get a taskkill mapping. Anything
# else falls back to closing the active window (Alt+F4) like the original did.
CLOSE_MAP = {
    "chrome": "chrome.exe",
    "google chrome": "chrome.exe",
    "spotify": "Spotify.exe",
    "notepad": "notepad.exe",
    "paint": "mspaint.exe",
}


def _fuzzy_lookup(name: str, table: dict):
    """Exact match first, then a loose substring match either direction."""
    if name in table:
        return name, table[name]
    for key, value in table.items():
        if key in name or name in key:
            return key, value
    return None, None


def get_live_weather(location: str) -> str:
    location = (location or "").strip() or "Tel Aviv"
    try:
        url = f"https://wttr.in/{requests.utils.quote(location)}?format=%l:+%c+%t"
        res = requests.get(url, timeout=6)
        res.raise_for_status()
        return res.text.strip()
    except requests.RequestException as e:
        logger.warning(f"Weather fetch failed for '{location}': {e}")
        return f"Weather data is unavailable for {location} right now."


def _open_via_start_menu(app_name: str) -> bool:
    """Generic fallback covering literally any installed app (traditional
    .exe or Store/UWP): press Win, type the name, hit Enter — the same
    thing a person does, relying on Windows' own indexed search instead of
    us maintaining an exhaustive exe/path table. Requires pyautogui, same as
    the other desktop-automation tools in this file."""
    if not pyautogui:
        return False
    try:
        pyautogui.press("win")
        time.sleep(0.6)   # Start menu open, search box focused
        pyautogui.write(app_name, interval=0.03)
        time.sleep(0.8)   # let Windows Search populate results
        pyautogui.press("enter")
        return True
    except Exception as e:
        logger.warning(f"Start-menu launch failed for '{app_name}': {e}")
        return False


def open_application(app_name: str) -> str:
    raw_name = (app_name or "").strip()
    if not raw_name:
        return "Which application should I open, sir?"
    key = raw_name.lower()
    matched_name, entry = _fuzzy_lookup(key, APP_MAP)

    if not DESKTOP_TOOLS_ENABLED:
        return "I can't open desktop applications from here, sir — this is the cloud-hosted build, there's no Windows desktop for me to control."

    if entry:
        try:
            if entry["type"] == "url":
                webbrowser.open(entry["target"])
            elif entry["type"] == "exe":
                subprocess.Popen([entry["target"]], shell=False)
            elif entry["type"] == "startfile" and hasattr(os, "startfile"):
                os.startfile(entry["target"])  # Windows-only, no shell parsing
            return f"Opening {matched_name}, sir."
        except Exception as e:
            logger.warning(f"Mapped launch failed for '{matched_name}', falling back to Start-menu search: {e}")

    # Not in the small fast-path map (or its launch failed) — Start-menu
    # search handles essentially every installed app without us needing to
    # know its exe name or install path ahead of time.
    if _open_via_start_menu(raw_name):
        return f"Opening {raw_name}, sir."

    if not hasattr(os, "startfile"):
        return f"I couldn't launch {raw_name}, sir."
    try:
        os.startfile(raw_name)
        return f"Attempting to launch {raw_name}, sir."
    except Exception as e:
        logger.error(f"Failed to open '{raw_name}': {e}")
        return f"I couldn't launch {raw_name}, sir."


def close_application(app_name: str) -> str:
    if not DESKTOP_TOOLS_ENABLED:
        return "I can't close desktop applications from here, sir — this is the cloud-hosted build, there's no Windows desktop for me to control."

    raw_name = (app_name or "").strip()
    key = raw_name.lower()
    matched_name, exe = _fuzzy_lookup(key, CLOSE_MAP)

    try:
        if exe:
            subprocess.run(["taskkill", "/f", "/im", exe], capture_output=True, timeout=5)
            return f"Closed {matched_name}, sir."

        if pyautogui:
            pyautogui.hotkey("alt", "f4")
            return "Closing the active window, sir."

        return f"I don't have a way to close '{raw_name}' on this system, sir."
    except Exception as e:
        logger.error(f"Failed to close '{raw_name}': {e}")
        return f"I couldn't close {raw_name}, sir."


def web_search(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "What would you like me to search for, sir?"
    webbrowser.open(f"https://www.google.com/search?q={requests.utils.quote(query)}")
    return f"Searching Google for {query}, sir."


def play_media(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "What would you like me to play, sir?"
    if pywhatkit:
        try:
            pywhatkit.playonyt(query)
            return f"Playing {query} on YouTube, sir."
        except Exception as e:
            logger.warning(f"pywhatkit failed for '{query}': {e}")
    # Fallback: pywhatkit missing or it failed (it's known to be flaky) —
    # open search results instead of failing silently.
    webbrowser.open(f"https://www.youtube.com/results?search_query={requests.utils.quote(query)}")
    return f"I've opened YouTube results for {query}, sir."


_spotify_client = None


def get_spotify_client():
    """Lazily build (and cache) an authenticated Spotify client. Returns
    None if credentials aren't configured or auth fails, so callers can
    degrade to a spoken message instead of crashing the request.

    On first use this opens a browser for you to log in and approve access
    — see spotify_login.py to do that once up front instead of mid-command.
    """
    global _spotify_client
    if _spotify_client is not None:
        return _spotify_client
    if not (spotipy and SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET):
        return None
    try:
        auth_manager = SpotifyOAuth(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET,
            redirect_uri=SPOTIFY_REDIRECT_URI,
            scope=SPOTIFY_SCOPE,
            cache_path=".spotify_cache",
            open_browser=True,
        )
        _spotify_client = spotipy.Spotify(auth_manager=auth_manager)
        return _spotify_client
    except Exception as e:
        logger.error(f"Spotify auth failed: {e}")
        return None


def play_on_spotify(query: str) -> str:
    query = (query or "").strip()
    if not query:
        return "What should I play on Spotify, sir?"

    sp = get_spotify_client()
    if not sp:
        return "Spotify isn't configured yet, sir — add your API credentials to .env."

    try:
        results = sp.search(q=query, type="track", limit=1)
        tracks = results.get("tracks", {}).get("items", [])
        if not tracks:
            return f"I couldn't find '{query}' on Spotify, sir."

        track = tracks[0]
        track_name = track["name"]
        artist_name = track["artists"][0]["name"] if track["artists"] else ""

        devices = sp.devices().get("devices", [])
        if not devices:
            # No Spotify Connect device is active yet — open the desktop app
            # and give it a moment to register itself as a playback target.
            open_application("spotify")
            time.sleep(4)
            devices = sp.devices().get("devices", [])

        if not devices:
            return "I couldn't find an active Spotify device, sir. Please open Spotify first."

        sp.start_playback(device_id=devices[0]["id"], uris=[track["uri"]])
        return f"Playing {track_name} by {artist_name} on Spotify, sir."

    except spotipy.exceptions.SpotifyException as e:
        logger.error(f"Spotify playback error: {e}")
        if getattr(e, "http_status", None) == 403:
            return "Spotify playback control needs a Premium account, sir."
        return "I ran into a problem controlling Spotify, sir."
    except Exception as e:
        logger.error(f"Spotify error: {e}")
        return "I ran into a problem reaching Spotify, sir."


def draw_shape_in_paint(shape: str = "circle", size: str = "medium") -> str:
    if not pyautogui:
        return "Drawing automation isn't available on this system, sir."
    if shape.lower() not in ("circle", "ellipse", "oval"):
        return f"I can currently only draw circles in Paint, sir — not {shape}."

    try:
        open_application("paint")
        time.sleep(1.5)

        left, top = 0, 0
        width, height = pyautogui.size()

        if gw:
            windows = gw.getWindowsWithTitle("Paint")
            if windows:
                win = windows[0]
                win.maximize()
                win.activate()
                time.sleep(0.5)
                left, top, width, height = win.left, win.top, win.width, win.height

        tool_x = left + int(width * PAINT_ELLIPSE_TOOL_REL[0])
        tool_y = top + int(height * PAINT_ELLIPSE_TOOL_REL[1])
        pyautogui.click(tool_x, tool_y)
        time.sleep(0.3)

        offset_x, offset_y = PAINT_SIZE_OFFSETS.get(size, PAINT_SIZE_OFFSETS["medium"])
        start_x = left + int(width * PAINT_CANVAS_START_REL[0])
        start_y = top + int(height * PAINT_CANVAS_START_REL[1])
        end_x = start_x + int(width * offset_x)
        end_y = start_y + int(height * offset_y)

        pyautogui.moveTo(start_x, start_y)
        pyautogui.keyDown("shift")   # constrains the ellipse to a perfect circle
        pyautogui.dragTo(end_x, end_y, duration=0.6, button="left")
        pyautogui.keyUp("shift")

        return "I've drawn a circle in Paint, sir."
    except Exception as e:
        logger.error(f"Paint drawing failed: {e}")
        return "I ran into a problem drawing in Paint, sir."


# Safe arithmetic evaluator for the calculator tool. Deliberately NOT using
# eval() — this walks the parsed expression tree and only allows numbers
# with +-*/%** operators, so it can't execute arbitrary code even though the
# expression string ultimately comes from LLM-interpreted speech.
_MATH_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Mod: operator.mod, ast.Pow: operator.pow,
    ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _safe_eval(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("Unsupported expression")


def _format_number(n):
    return str(int(n)) if isinstance(n, float) and n.is_integer() else str(round(n, 6))


def calculate(expression: str) -> str:
    expression = (expression or "").strip()
    if not expression:
        return "What would you like me to calculate, sir?"
    try:
        result = _safe_eval(ast.parse(expression, mode="eval").body)
    except ZeroDivisionError:
        return "That's a division by zero, sir."
    except Exception as e:
        logger.warning(f"Calculate failed for '{expression}': {e}")
        return f"I couldn't compute '{expression}', sir."

    # Also type it into the real Calculator app so it's visible on screen —
    # Windows Calculator accepts full keyboard input, so no coordinate
    # clicking is needed here (unlike Paint).
    if pyautogui:
        try:
            open_application("calculator")
            time.sleep(1)
            pyautogui.write(expression.replace(" ", ""), interval=0.05)
            pyautogui.press("enter")
        except Exception as e:
            logger.warning(f"Couldn't type into Calculator: {e}")

    return f"{expression} equals {_format_number(result)}, sir."


# JSON-schema tool definitions handed to the model. Groq's function-calling
# format is OpenAI-compatible: {"type": "function", "function": {...}}.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current live weather for a city or location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name, e.g. 'Tel Aviv' or 'London'"}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_application",
            "description": "Open or launch a desktop application, e.g. chrome, spotify, notepad, calculator, paint, file explorer.",
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_application",
            "description": "Close or quit a running desktop application.",
            "parameters": {
                "type": "object",
                "properties": {"app_name": {"type": "string"}},
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search Google for a query and open the results in the browser.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_on_spotify",
            "description": (
                "Search for and play a song on Spotify via the real Spotify API — "
                "opens the Spotify app itself if it isn't already running with an "
                "active device. Always use this for Spotify requests, never "
                "computer_use."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Song and/or artist to search for, e.g. 'Bohemian Rhapsody Queen'."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_media",
            "description": "Play a song or video on YouTube — only for non-Spotify requests.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": (
                "Compute an arithmetic expression and type it into the Calculator app. "
                "Use a clean expression with only digits, + - * / ( ) . and no words, "
                "e.g. '12*7' or '(45+30)/5'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"expression": {"type": "string"}},
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_daily_agenda",
            "description": (
                "Get a full summary of the user's day: today's calendar events "
                "plus unread emails, merged across whichever of Google/Outlook "
                "are connected. Use this for broad requests like 'what's my day "
                "look like' or 'brief me'."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_calendar_events",
            "description": "List upcoming calendar events over the next N days (default 1 = today).",
            "parameters": {
                "type": "object",
                "properties": {
                    "days_ahead": {"type": "number", "description": "How many days ahead to look, e.g. 1 for today, 7 for this week."}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_emails",
            "description": "List recent emails (unread by default), merged across whichever of Gmail/Outlook are connected.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unread_only": {"type": "boolean"},
                    "max_results": {"type": "number"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_calendar_event",
            "description": (
                "Propose creating a calendar event. This does NOT create it immediately — "
                "it registers a confirmation request the user must approve. Use ISO 8601 "
                "datetimes with the local UTC offset given in the current-time system "
                "message (e.g. '2026-08-05T14:00:00+03:00') — do not convert to 'Z'/UTC."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "start_iso": {"type": "string"},
                    "end_iso": {"type": "string"},
                    "description": {"type": "string"},
                    "location": {"type": "string"},
                    "provider": {"type": "string", "description": "'google', 'microsoft', or 'auto' (default)"},
                },
                "required": ["summary", "start_iso", "end_iso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "Propose sending an email. This does NOT send it immediately — "
                "it registers a confirmation request the user must approve."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "provider": {"type": "string", "description": "'google', 'microsoft', or 'auto' (default)"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Set a reminder for a future time — delivered by email at that "
                "time (there's no other reliable way to reach the user later if "
                "they're not looking at the HUD right then). Executes immediately, "
                "no approval needed — it's a private note-to-self, not an action "
                "on anyone's real calendar or mailbox. Use ISO 8601 with the local "
                "UTC offset given in the current-time system message, same as "
                "create_calendar_event."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "What to be reminded of, e.g. 'call the dentist'."},
                    "remind_at_iso": {"type": "string"},
                },
                "required": ["text", "remind_at_iso"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "computer_use",
            "description": (
                "General screen control for anything without a dedicated tool: "
                "clicking UI elements, filling in forms, browser navigation, "
                "dragging, typing into any app. Describe the visual goal in "
                "plain language, e.g. 'open Gmail and click Compose'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"goal": {"type": "string"}},
                "required": ["goal"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_and_test_code",
            "description": (
                "Write a Python script to accomplish a goal, run it in the "
                "sandbox, and automatically fix and re-run it if it errors, "
                "until it succeeds or the retry limit is reached."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "initial_code": {"type": "string", "description": "First draft of the script"},
                },
                "required": ["goal", "initial_code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_workspace",
            "description": "List files in the JARVIS workspace (or a subfolder of it).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_workspace_file",
            "description": "Read a text file from the JARVIS workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_workspace_file",
            "description": "Write/overwrite a text file in the JARVIS workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_workspace_path",
            "description": (
                "Delete a file or folder in the JARVIS workspace. Requires "
                "user confirmation before it actually happens."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
]

# open_application/close_application/computer_use are always dead weight in
# this cloud build (DESKTOP_TOOLS_ENABLED=false — no Windows desktop for any
# of them to control) but their full JSON schemas were still being sent to
# Groq on every single request regardless, burning tokens against the
# free-tier rate limit for tools that could only ever return a canned
# refusal. Computed once at import time (DESKTOP_TOOLS_ENABLED doesn't
# change at runtime), not filtered per-request.
_DESKTOP_ONLY_TOOL_NAMES = {"open_application", "close_application", "computer_use"}
ACTIVE_TOOLS = TOOLS if DESKTOP_TOOLS_ENABLED else [
    t for t in TOOLS if t["function"]["name"] not in _DESKTOP_ONLY_TOOL_NAMES
]

def _computer_use(user_id, args):
    return vision_action.vision_action_loop(
        groq_client, args.get("goal", ""),
        on_step=lambda evt: event_stream.push_event(evt, user_id=user_id),
    )


def _write_and_test_code(user_id, args):
    outcome = self_healing.self_healing_code_loop(
        groq_client, MODEL_NAME, args.get("goal", ""), args.get("initial_code", ""),
        on_attempt=lambda evt: event_stream.push_event(evt, user_id=user_id),
        user_id=user_id,
    )
    if outcome["success"]:
        return f"Succeeded after {outcome['attempts']} attempt(s), sir. Output: {outcome['stdout'][:300]}"
    return f"Couldn't get it working after {outcome['attempts']} attempts, sir. Last error: {outcome['stderr'][-300:]}"


def _delete_workspace_path(user_id, args):
    result = file_tools.delete_path(args.get("path", ""), user_id=user_id)
    if result["status"] == "confirmation_required":
        event_stream.push_event({
            "type": "confirmation_required", "token": result["token"], "message": result["message"],
        }, user_id=user_id)
        return f"Please confirm, sir: {result['message']} (token {result['token']})"
    return result["message"]


def _create_calendar_event(user_id, args):
    result = productivity_service.request_create_calendar_event(
        user_id,
        summary=args.get("summary", ""),
        start_iso=args.get("start_iso", ""),
        end_iso=args.get("end_iso", ""),
        description=args.get("description", ""),
        location=args.get("location", ""),
        provider=args.get("provider", "auto"),
    )
    if result["status"] == "confirmation_required":
        event_stream.push_event({
            "type": "confirmation_required", "token": result["token"], "message": result["message"],
            "kind": result.get("kind", "generic"), "details": result.get("details"),
        }, user_id=user_id)
        return f"I've drawn up '{args.get('summary', '')}' for your approval, sir — check the HUD."
    return result["message"]


def _send_email(user_id, args):
    result = productivity_service.request_send_email(
        user_id,
        to=args.get("to", ""),
        subject=args.get("subject", ""),
        body=args.get("body", ""),
        provider=args.get("provider", "auto"),
    )
    if result["status"] == "confirmation_required":
        event_stream.push_event({
            "type": "confirmation_required", "token": result["token"], "message": result["message"],
            "kind": result.get("kind", "generic"), "details": result.get("details"),
        }, user_id=user_id)
        return f"I've drawn up that email for your approval, sir — check the HUD."
    return result["message"]


def _set_reminder(user_id, args):
    result = productivity_service.request_set_reminder(
        user_id, text=args.get("text", ""), remind_at_iso=args.get("remind_at_iso", ""),
    )
    return result["message"]


# Every tool dispatch entry is now built fresh per request, closing over
# that request's signed-in user_id — including the ones below that don't
# touch calendar/mail data. They used to live in a shared, static dict, but
# computer_use/write_and_test_code/the workspace file tools all needed
# user_id too (per-user sandboxing, per-user event_stream delivery), so at
# that point there was nothing left that was actually safe to share.
_STATIC_TOOL_IMPL = {
    "get_weather": lambda args: get_live_weather(args.get("location", "")),
    "open_application": lambda args: open_application(args.get("app_name", "")),
    "close_application": lambda args: close_application(args.get("app_name", "")),
    "web_search": lambda args: web_search(args.get("query", "")),
    "play_on_spotify": lambda args: play_on_spotify(args.get("query", "")),
    "play_media": lambda args: play_media(args.get("query", "")),
    "calculate": lambda args: calculate(args.get("expression", "")),
}


def _build_tool_impl(user_id: str) -> dict:
    impl = dict(_STATIC_TOOL_IMPL)
    impl.update({
        "get_daily_agenda": lambda args: productivity_service.get_daily_agenda_text(user_id),
        "get_calendar_events": lambda args: productivity_service.get_calendar_events_text(user_id, args.get("days_ahead", 1)),
        "list_emails": lambda args: productivity_service.get_emails_text(
            user_id, args.get("unread_only", True), args.get("max_results", 8)
        ),
        "create_calendar_event": lambda args: _create_calendar_event(user_id, args),
        "send_email": lambda args: _send_email(user_id, args),
        "set_reminder": lambda args: _set_reminder(user_id, args),
        "computer_use": lambda args: _computer_use(user_id, args),
        "write_and_test_code": lambda args: _write_and_test_code(user_id, args),
        "list_workspace": lambda args: file_tools.list_dir(args.get("path", "."), user_id=user_id),
        "read_workspace_file": lambda args: file_tools.read_file(args.get("path", ""), user_id=user_id),
        "write_workspace_file": lambda args: file_tools.write_file(args.get("path", ""), args.get("content", ""), user_id=user_id),
        "delete_workspace_path": lambda args: _delete_workspace_path(user_id, args),
    })
    return impl


# ---------------------------------------------------------------------------
# The "brain": one Groq call with tools, looping if the model chains calls
# ---------------------------------------------------------------------------
def _complete_with_retry(messages):
    """Groq occasionally emits truncated/invalid JSON for a tool call
    (400 'tool_use_failed'), especially the first time it tries a call with
    several arguments. That's noisy generation, not a broken request — one
    retry almost always succeeds, so this saves the whole turn from dying
    with 'Neural connection error, sir' over what's usually a fluke.
    """
    try:
        return groq_client.chat.completions.create(
            model=MODEL_NAME, messages=messages, tools=ACTIVE_TOOLS,
            tool_choice="auto", temperature=0.6, max_tokens=MAX_TOKENS,
        )
    except BadRequestError as e:
        code = getattr(e, "body", {}) or {}
        code = (code.get("error") or {}).get("code")
        if code != "tool_use_failed":
            raise
        logger.warning("Tool call JSON malformed, retrying once...")
        return groq_client.chat.completions.create(
            model=MODEL_NAME, messages=messages, tools=ACTIVE_TOOLS,
            tool_choice="auto", temperature=0.6, max_tokens=MAX_TOKENS,
        )


_MARKDOWN_BOLD_ITALIC_RE = re.compile(r"(\*\*\*|\*\*|\*|___|__|_)(.+?)\1")
_MARKDOWN_HEADER_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MARKDOWN_BULLET_RE = re.compile(r"^\s*[\*\-•]\s+", re.MULTILINE)


def _strip_markdown(text: str) -> str:
    """Every response here either gets spoken aloud (edge-tts has no idea
    '*' means "bold", it just mangles the sentence) or shown in the compact
    log/hologram — so markdown syntax needs to come out at the source rather
    than being cleaned up per call site. Applied once, in run_llm, before
    the LLM's free-form text goes anywhere."""
    if not text:
        return text
    text = _MARKDOWN_BOLD_ITALIC_RE.sub(r"\2", text)
    text = _MARKDOWN_HEADER_RE.sub("", text)
    text = _MARKDOWN_BULLET_RE.sub("", text)
    return text


def run_llm(user_text: str, user_id: str) -> str:
    history = _history_for(user_id)
    tool_impl = _build_tool_impl(user_id)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": _time_context()},
    ]
    messages.extend(history[-MAX_HISTORY_MESSAGES:])
    messages.append({"role": "user", "content": user_text})

    final_text = None
    try:
        for _ in range(MAX_TOOL_ROUNDS):
            msg = _complete_with_retry(messages).choices[0].message

            if not msg.tool_calls:
                final_text = msg.content
                break

            messages.append(msg)
            for call in msg.tool_calls:
                fn_name = call.function.name
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                impl = tool_impl.get(fn_name)
                try:
                    result = impl(args) if impl else f"Unknown tool '{fn_name}'."
                except Exception as e:
                    logger.error(f"Tool '{fn_name}' raised: {e}")
                    result = f"The '{fn_name}' action ran into an unexpected problem and didn't complete, sir."
                logger.info(f"tool call: {fn_name}({args}) -> {result}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": fn_name,
                        "content": result,
                    }
                )

        if not final_text:
            final_text = "Done, sir."
        final_text = _strip_markdown(final_text)

    except RateLimitError as e:
        logger.error(f"Groq rate limit hit: {e}")
        return "I've hit my usage limit with Groq for now, sir — give it a few minutes, or we may need to move to a paid tier."
    except APIConnectionError as e:
        logger.error(f"Groq connection error: {e}")
        return "I can't reach Groq right now, sir — check the internet connection."
    except Exception as e:
        logger.error(f"LLM error: {e}")
        return "Neural connection error, sir."

    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": final_text})
    del history[:-MAX_HISTORY_MESSAGES]

    return final_text


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# --- Google sign-in gate ----------------------------------------------------
# First-visit HUD gate: the browser tab itself drives the OAuth redirect
# (rather than google_login.py's separate popup window) so a brand-new user
# can connect Calendar/Gmail/Drive without touching a terminal. Nothing else
# in the HUD is usable until google_service reports a cached, valid token.
@app.route("/api/auth/google/status")
def google_auth_status():
    user_id = session.get("user_id")
    return jsonify({
        "configured": google_service.CONFIGURED,
        "connected": bool(user_id) and google_service.is_connected(user_id),
        "email": session.get("user_email") if user_id else None,
    })


@app.route("/api/auth/google/disconnect", methods=["POST"])
def google_auth_disconnect():
    # This is the multi-user build's "disconnect" — it's really a sign-out:
    # dropping the stored token AND clearing the session, since here Google
    # sign-in is identity, not just a calendar/mail permission toggle.
    user_id = session.get("user_id")
    if user_id:
        google_service.disconnect(user_id)
    session.clear()
    return jsonify({"status": "disconnected"})


@app.route("/auth/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"status": "logged_out"})


@app.route("/auth/google/start")
def google_auth_start():
    if not google_service.CONFIGURED:
        return "Google OAuth isn't configured on this server (missing GOOGLE_CLIENT_ID/SECRET).", 500
    redirect_uri = url_for("google_auth_callback", _external=True)
    login_hint = request.args.get("login_hint") or None
    auth_url, state, code_verifier = google_service.get_authorization_url(redirect_uri, login_hint=login_hint)
    session["google_oauth_state"] = state
    session["google_oauth_code_verifier"] = code_verifier
    return redirect(auth_url)


@app.route("/auth/google/callback")
def google_auth_callback():
    if request.args.get("error"):
        return redirect(url_for("index", google_auth="denied"))

    expected_state = session.pop("google_oauth_state", None)
    code_verifier = session.pop("google_oauth_code_verifier", None)
    if not expected_state or request.args.get("state") != expected_state:
        return redirect(url_for("index", google_auth="error"))

    redirect_uri = url_for("google_auth_callback", _external=True)
    user = google_service.exchange_code(redirect_uri, request.args.get("code"), code_verifier)
    if not user:
        return redirect(url_for("index", google_auth="error"))

    # Sign-in with Google *is* the account here — the same consent screen
    # both proves who they are and grants calendar/mail access, so there's
    # no separate login step. session.permanent so returning users skip
    # straight past the gate instead of re-approving every browser session.
    session.permanent = True
    session["user_id"] = user["id"]
    session["user_email"] = user["email"]
    return redirect(url_for("index", google_auth="ok"))


@app.route("/api/command", methods=["POST"])
def process_command():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"response": "Please sign in first, sir.", "audio": None}), 401

    data = request.json or {}
    text = (data.get("command") or "").strip()

    if not text:
        return jsonify({"response": "Standing by, sir.", "audio": None})

    response_text = run_llm(text, user_id)
    audio_b64 = asyncio.run(generate_tts_base64(response_text))

    return jsonify({"response": response_text, "audio": audio_b64})


@app.route("/api/reset", methods=["POST"])
def reset_conversation():
    user_id = session.get("user_id")
    if user_id:
        _history_for(user_id).clear()
    return jsonify({"status": "cleared"})


def _confirm_owner_denial(token):
    """None if the signed-in session may act on this token, else a message
    to show instead. Without this, event_stream's old broadcast-to-everyone
    behavior meant any signed-in user could see (and, with a guessed/replayed
    token, act on) another user's pending calendar event/email/file-delete —
    this is the actual enforcement point now that meta carries who a
    confirmation belongs to."""
    user_id = session.get("user_id")
    if not user_id:
        return "Please sign in first, sir."
    meta = get_pending_meta(token)
    if meta and meta.get("user_id") and meta["user_id"] != user_id:
        return "That confirmation isn't yours, sir."
    return None


@app.route("/api/confirm/<token>", methods=["POST"])
def confirm_action(token):
    denial = _confirm_owner_denial(token)
    if denial:
        return jsonify({"response": denial, "audio": None}), 403
    approve = bool((request.json or {}).get("approve", False))
    message = resolve_confirmation(token, approve)
    audio_b64 = asyncio.run(generate_tts_base64(message))
    return jsonify({"response": message, "audio": audio_b64})


@app.route("/api/confirm/<token>/edit", methods=["POST"])
def edit_confirmation(token):
    """Rewrites a still-pending proposal's fields before it's approved —
    the HUD's Edit button. Nothing executes here; it only updates what a
    subsequent /api/confirm/<token> approve will act on."""
    denial = _confirm_owner_denial(token)
    if denial:
        return jsonify({"status": "error", "message": denial}), 403
    data = request.json or {}
    kind = data.get("kind")
    if kind == "calendar_event":
        result = productivity_service.edit_pending_calendar_event(
            token,
            summary=data.get("summary", ""),
            start_iso=data.get("start_iso", ""),
            end_iso=data.get("end_iso", ""),
            description=data.get("description", ""),
            location=data.get("location", ""),
        )
    elif kind == "email":
        result = productivity_service.edit_pending_email(
            token,
            to=data.get("to", ""),
            subject=data.get("subject", ""),
            body=data.get("body", ""),
        )
    else:
        return jsonify({"status": "error", "message": "Unknown confirmation kind."}), 400
    return jsonify(result)


@app.route("/api/upcoming-events")
def upcoming_events():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"configured": False, "events": []})
    events = productivity_service.get_upcoming_events_structured(user_id, max_results=3)
    if events is None:
        return jsonify({"configured": False, "events": []})
    return jsonify({"configured": True, "events": events})


@app.route("/api/speak", methods=["POST"])
def speak():
    """Generic text-to-speech — wraps tts.generate_tts_base64 for callers
    that already have plain text ready (e.g. the inbox modal's zero-LLM
    spoken summary) and don't need the full run_llm tool-calling pipeline."""
    text = (request.json or {}).get("text", "").strip()
    if not text:
        return jsonify({"audio": None})
    audio_b64 = asyncio.run(generate_tts_base64(text))
    return jsonify({"audio": audio_b64})


@app.route("/api/inbox")
def inbox():
    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"configured": False, "emails": [], "summary": "Please sign in first, sir."})
    emails = productivity_service.get_inbox_structured(user_id, max_results=8)
    if emails is None:
        return jsonify({"configured": False, "emails": [], "summary": "No mailbox is connected, sir."})
    summary = productivity_service.get_inbox_summary_spoken(emails)
    return jsonify({"configured": True, "emails": emails, "summary": summary})


@app.route("/api/inbox/draft-reply", methods=["POST"])
def inbox_draft_reply():
    data = request.json or {}
    sender_name = (data.get("sender_name") or "the sender").strip()
    subject = (data.get("subject") or "").strip()
    snippet = (data.get("snippet") or "").strip()
    instructions = (data.get("instructions") or "").strip()
    if not instructions:
        return jsonify({"error": "What would you like to say, sir?"}), 400

    prompt = (
        f"Original email — From: {sender_name}. Subject: {subject}. Preview: {snippet}\n\n"
        f"What the user wants to say in reply: {instructions}\n\n"
        "Write ONLY the reply email body as plain text — a short greeting, then a "
        "few sentences that naturally develop the point above in your own words "
        "(don't just restate it tersely in one line), then a polite closing. No "
        "subject line, no markdown, no placeholders like [Your Name]. Match the "
        "language the instructions above are written in."
    )
    try:
        completion = groq_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role": "system", "content": "You draft warm, professional plain-text email replies for J.A.R.V.I.S., an assistant — a real reply with a greeting and closing, not a one-line brush-off. Output only the email body, nothing else."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.6, max_tokens=400,
        )
        draft = _strip_markdown(completion.choices[0].message.content or "").strip()
        return jsonify({"draft": draft})
    except RateLimitError:
        return jsonify({"error": "I've hit my usage limit with Groq for now, sir — give it a few minutes."}), 429
    except Exception as e:
        logger.error(f"Draft-reply failed: {e}")
        return jsonify({"error": "I couldn't draft that reply, sir."}), 500


@app.route("/api/inbox/send-reply", methods=["POST"])
def inbox_send_reply():
    data = request.json or {}
    to = (data.get("to") or "").strip()
    subject = (data.get("subject") or "").strip()
    body = (data.get("body") or "").strip()
    if not to or not body:
        return jsonify({"error": "Missing recipient or message body."}), 400

    user_id = session.get("user_id")
    if not user_id:
        return jsonify({"error": "Please sign in first, sir."}), 401

    reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    # Same confirm-gated path every other write in this app uses — this
    # never sends on its own, it just returns a token for the existing
    # confirm modal to show and the user to approve.
    result = productivity_service.request_send_email(user_id, to, reply_subject, body)
    return jsonify(result)


@app.route("/api/stream")
def stream():
    """SSE feed of live progress: vision-action steps, self-heal attempts,
    and confirmation requests. The HUD subscribes with EventSource.

    Scoped to the signed-in user (see event_stream.py's docstring for why —
    this used to broadcast every event to every connected tab regardless of
    whose it was). user_id is read here, before entering the generator,
    since `session` isn't reliably readable once inside a streaming
    generator without stream_with_context — an anonymous/not-yet-signed-in
    tab still gets a live connection, it just never receives anything,
    which is correct since it has no user to receive events on behalf of."""
    user_id = session.get("user_id")

    def gen():
        q = event_stream.subscribe(user_id) if user_id else None
        try:
            while True:
                try:
                    if q is None:
                        raise queue.Empty
                    event = q.get(timeout=15)
                    yield event_stream.format_sse(event)
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            if q is not None:
                event_stream.unsubscribe(user_id, q)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Started at import time (not inside `if __name__ == "__main__"`) so it
# actually runs under gunicorn too — Render's `gunicorn app:app` imports
# this module without ever executing the __main__ block below, so the old
# __main__-only call never fired in production at all. Safe with exactly
# one worker (this app's fixed --workers 1): each worker process would
# otherwise start its own copy and double/triple-fire reminders and the
# weekly summary. The WERKZEUG_RUN_MAIN check still exists for local `python
# app.py` debug-mode runs, where Werkzeug's reloader re-executes this module
# in a child process — gunicorn never sets that env var, so this always
# proceeds to start it exactly once there.
if os.environ.get("FLASK_DEBUG", "false").lower() != "true" or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
    daily_briefing.start_scheduler()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"

    # Binding to 0.0.0.0 with debug=True exposes the Werkzeug interactive
    # debugger to your whole network — anyone who can reach it gets arbitrary
    # code execution. Default to localhost-only; opt into LAN access explicitly.
    allow_lan = os.environ.get("JARVIS_ALLOW_LAN", "false").lower() == "true"
    host = "0.0.0.0" if allow_lan else "127.0.0.1"

    # Windows has silently let a second app.py process bind this same port
    # before (confirmed twice while building the Google auth flow, e.g.
    # double-launching from a terminal + an editor's "Run" button) — once
    # that happens, each request lands on whichever process wins the race,
    # so code changes look like they "didn't take" when they actually just
    # hit the stale instance. Refuse to start a second one instead of
    # adding to the pile.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        probe_host = "127.0.0.1" if host == "0.0.0.0" else host
        if probe.connect_ex((probe_host, port)) == 0:
            raise SystemExit(
                f"Another JARVIS instance is already listening on port {port}. "
                "Stop it first — check Task Manager, or in PowerShell: "
                "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" "
                "| Select ProcessId,CommandLine"
            )

    webbrowser.open(f"http://127.0.0.1:{port}")
    app.run(host=host, port=port, debug=debug_mode, threaded=True)
