"""
tavily_service.py — live search grounding via Tavily's search API.

Groq's models (used everywhere else in this app) are static — frozen at
their training cutoff, no internet access at all. This is the one place
JARVIS can actually know about something that happened recently (general
news/current events — stock/market questions are handled separately and
for free by stocks_service.get_market_summary, no search needed there):
Tavily's free tier (no credit card, 1,000 requests/month) is purpose-built
for exactly this — feed it a question, get back an LLM-synthesized answer
plus the sources it drew from.

Replaces an earlier Gemini-based version of this same tool: Gemini's free
tier turned out to require Google Cloud billing setup even to use it,
which defeated the point of a "free" search tool. Tavily's free tier
genuinely needs nothing but an email.

Deliberately kept to exactly one function, wired to exactly one TOOLS entry
(app.py's get_current_info) — this is NOT a second general-purpose LLM
backend and not a way around find_nearby_places' "don't search Google like
crazy" scoping. SYSTEM_PROMPT spells out when the model is allowed to use it.

Setup: https://tavily.com -> sign up (free, no card) -> an API key is
generated automatically, starts with "tvly-". Put it in TAVILY_API_KEY.
"""

import logging
import os

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below
import requests

logger = logging.getLogger("jarvis.tavily")

TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY")
CONFIGURED = bool(TAVILY_API_KEY)

_SEARCH_URL = "https://api.tavily.com/search"


def search_current_info(query: str) -> str:
    """Answers a query that needs live/current information, grounded in a
    real web search Tavily performs itself (unlike find_nearby_places,
    which only opens a link for the user to read — this reads results and
    answers directly). Returns a plain-text answer, or an explanatory
    string if unconfigured/the call fails — never raises, since this
    always runs inside Groq's tool-call loop, which expects a string back
    either way."""
    if not CONFIGURED:
        return "Live search isn't set up, sir — TAVILY_API_KEY is missing."
    query = (query or "").strip()
    if not query:
        return "What would you like me to look up, sir?"
    try:
        res = requests.post(
            _SEARCH_URL,
            headers={"Authorization": f"Bearer {TAVILY_API_KEY}"},
            json={"query": query, "include_answer": "advanced", "max_results": 5},
            timeout=15,
        )
        res.raise_for_status()
        answer = (res.json().get("answer") or "").strip()
        return answer or "I searched but didn't get a usable answer, sir."
    except requests.RequestException as e:
        logger.error(f"Tavily search failed: {e}")
        return "I couldn't complete that search right now, sir — please try again shortly."
