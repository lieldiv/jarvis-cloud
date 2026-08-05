"""
stocks_service.py — zero-LLM stock quote lookup, backing the HUD's "מניות"
button.

Same "deterministic bypass" philosophy as /api/agenda/week and
daily_briefing.py: this never goes through Groq/Gemini at all — just a
plain HTTP call to Yahoo Finance's public (unofficial, no API key or
signup needed) endpoints, so a search can't be broken by a bad LLM
response and doesn't cost anything against either free-tier quota.

Two calls per lookup: a symbol search (so a user can type "apple" instead
of needing to know "AAPL"), then a chart fetch for that ticker to compute
today's and this week's change. Unofficial API — no SLA, no key, can
change or start blocking a host's IP without notice; every call is wrapped
so a failure degrades to a clear error dict instead of a stack trace.
"""

import logging

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below
import requests

logger = logging.getLogger("jarvis.stocks")

_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"
_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
# A bare requests User-Agent gets a 403 from this endpoint; any real
# browser-looking one satisfies it.
_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _resolve_symbol(query: str):
    res = requests.get(
        _SEARCH_URL,
        params={"q": query, "quotesCount": 5, "newsCount": 0},
        headers=_HEADERS, timeout=6,
    )
    res.raise_for_status()
    quotes = [
        q for q in res.json().get("quotes", [])
        if q.get("symbol") and q.get("quoteType") == "EQUITY"
    ]
    return quotes[0] if quotes else None  # Yahoo already ranks these by relevance


def get_quote(query: str) -> dict:
    """Returns a data dict on success:
      {symbol, name, exchange, currency, price, change_today, change_today_pct,
       change_week, change_week_pct, day_high, day_low, week_52_high, week_52_low}
    or {"error": "..."} on failure — never raises, since this is called
    directly from a Flask route with no LLM in between to smooth over an
    unexpected shape."""
    query = (query or "").strip()
    if not query:
        return {"error": "What company or ticker should I look up, sir?"}
    try:
        match = _resolve_symbol(query)
        if not match:
            return {"error": f"Couldn't find a ticker matching '{query}', sir."}
        symbol = match["symbol"]

        res = requests.get(
            _CHART_URL.format(symbol=symbol),
            params={"range": "5d", "interval": "1d"},
            headers=_HEADERS, timeout=6,
        )
        res.raise_for_status()
        result = res.json()["chart"]["result"][0]
        meta = result["meta"]
        closes = [c for c in result["indicators"]["quote"][0]["close"] if c is not None]
        if not closes:
            return {"error": f"No price data available for {symbol} right now, sir."}

        price = meta.get("regularMarketPrice", closes[-1])
        prev_close = meta.get("previousClose") or meta.get("chartPreviousClose") or closes[-1]
        week_open = closes[0]

        change_today = price - prev_close
        change_week = price - week_open

        return {
            "symbol": symbol,
            "name": match.get("shortname") or match.get("longname") or symbol,
            "exchange": meta.get("fullExchangeName", match.get("exchange", "")),
            "currency": meta.get("currency", ""),
            "price": round(price, 2),
            "change_today": round(change_today, 2),
            "change_today_pct": round(change_today / prev_close * 100, 2) if prev_close else 0,
            "change_week": round(change_week, 2),
            "change_week_pct": round(change_week / week_open * 100, 2) if week_open else 0,
            "day_high": round(meta.get("regularMarketDayHigh", price), 2),
            "day_low": round(meta.get("regularMarketDayLow", price), 2),
            "week_52_high": round(meta.get("fiftyTwoWeekHigh", 0) or 0, 2),
            "week_52_low": round(meta.get("fiftyTwoWeekLow", 0) or 0, 2),
        }
    except requests.RequestException as e:
        logger.warning(f"Stock quote fetch failed for '{query}': {e}")
        return {"error": "Couldn't reach the stock data service right now, sir."}
    except Exception as e:
        logger.error(f"Stock quote parsing failed for '{query}': {e}")
        return {"error": "Something went wrong reading that stock's data, sir."}
