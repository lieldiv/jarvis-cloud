"""
gemini_service.py — narrow live-search grounding via Google's Gemini API.

Groq's models (used everywhere else in this app) are static — frozen at
their training cutoff, no internet access at all. This is the one place
JARVIS can actually know about something that happened recently (stock
moves, news, scores): Google AI Studio's free tier (no credit card) gives
Gemini a real Google Search grounding tool — when asked, the model actually
searches and reads results before answering, instead of guessing from old
training data.

Deliberately kept to exactly one function, wired to exactly one TOOLS entry
(app.py's get_current_info) — this is NOT a second general-purpose LLM
backend and not a way around find_nearby_places' "don't search Google like
crazy" scoping. SYSTEM_PROMPT spells out when the model is allowed to use it.

Setup: https://aistudio.google.com/apikey -> "Create API key" (free tier,
no billing/card required). Put it in GEMINI_API_KEY.
"""

import logging
import os

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below

logger = logging.getLogger("jarvis.gemini")

try:
    from google import genai
    from google.genai import types
    GEMINI_LIBS_AVAILABLE = True
except Exception:
    GEMINI_LIBS_AVAILABLE = False

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
CONFIGURED = bool(GEMINI_LIBS_AVAILABLE and GEMINI_API_KEY)

# Confirmed live against the real key: "gemini-2.5-flash" now 404s —
# "This model ... is no longer available to new users" — Google retired it
# for newly-created API keys/projects even though it still shows up in
# models.list(). "-latest" is a Google-provided alias to whatever current
# model that tier actually points to, specifically so this doesn't happen
# again the next time Google reshuffles the lineup.
_MODEL = "gemini-flash-latest"

_client = None
if CONFIGURED:
    try:
        _client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Gemini client init failed: {e}")
        CONFIGURED = False


def search_current_info(query: str) -> str:
    """Answers a query that needs live/current information, grounded in an
    actual Google Search the model performs itself (unlike find_nearby_places,
    which only opens a link for the user to read — this reads results and
    answers directly). Returns a plain-text answer, or an explanatory string
    if unconfigured/the call fails — never raises, since this always runs
    inside Groq's tool-call loop, which expects a string back either way."""
    if not CONFIGURED:
        return "Live search isn't set up, sir — GEMINI_API_KEY is missing."
    query = (query or "").strip()
    if not query:
        return "What would you like me to look up, sir?"
    try:
        response = _client.models.generate_content(
            model=_MODEL,
            contents=query,
            config=types.GenerateContentConfig(
                tools=[types.Tool(google_search=types.GoogleSearch())],
            ),
        )
        text = (response.text or "").strip()
        return text or "I searched but didn't get a usable answer, sir."
    except Exception as e:
        logger.error(f"Gemini grounded search failed: {e}")
        return "I couldn't complete that search right now, sir — please try again shortly."
