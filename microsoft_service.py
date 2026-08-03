"""
microsoft_service.py — Outlook/365 Calendar + Mail via Microsoft Graph.

Setup (see README):
  1. https://portal.azure.com/ -> Azure Active Directory -> App registrations
     -> New registration. Supported account types: whichever matches you
     (work/school and/or personal Microsoft accounts).
  2. Authentication -> Add a platform -> "Mobile and desktop applications" ->
     check `http://localhost` as a redirect URI, and enable
     "Allow public client flows".
  3. API permissions -> Microsoft Graph -> Delegated: Calendars.ReadWrite,
     Mail.Read, Mail.Send, User.Read.
  4. Put the Application (client) ID in .env as MS_CLIENT_ID. MS_TENANT_ID
     defaults to "common" (personal + work/school accounts); set it to your
     tenant ID if you want to restrict to one org.
  5. Run `python microsoft_login.py` once — opens a browser to consent, then
     caches the token in .ms_cache.bin.

Uses plain `requests` against the Graph REST API rather than the (heavier,
less stable) Graph SDK — msal handles auth, requests handles the calls.
"""

import logging
import os

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below
import requests

logger = logging.getLogger("jarvis.microsoft")

try:
    import msal
    MSAL_AVAILABLE = True
except Exception:
    MSAL_AVAILABLE = False

MS_CLIENT_ID = os.environ.get("MS_CLIENT_ID")
MS_TENANT_ID = os.environ.get("MS_TENANT_ID", "common")

CACHE_PATH = ".ms_cache.bin"
AUTHORITY = f"https://login.microsoftonline.com/{MS_TENANT_ID}"
SCOPES = ["Calendars.ReadWrite", "Mail.Read", "Mail.Send", "User.Read"]
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"

CONFIGURED = bool(MSAL_AVAILABLE and MS_CLIENT_ID)


def _load_cache():
    cache = msal.SerializableTokenCache()
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        except Exception as e:
            logger.warning(f"Couldn't load Microsoft token cache: {e}")
    return cache


def _save_cache(cache):
    if cache.has_state_changed:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(cache.serialize())


def _app(cache):
    return msal.PublicClientApplication(MS_CLIENT_ID, authority=AUTHORITY, token_cache=cache)


def get_access_token(interactive: bool = False):
    """Silent-first token acquisition, same shape as google_service's
    get_credentials(): runtime tool calls pass interactive=False so a voice
    command never blocks on a browser popup; microsoft_login.py passes True.
    """
    if not CONFIGURED:
        return None

    cache = _load_cache()
    app = _app(cache)

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result and interactive:
        try:
            result = app.acquire_token_interactive(SCOPES)
        except Exception as e:
            logger.error(f"Microsoft interactive login failed: {e}")
            result = None

    _save_cache(cache)

    if result and "access_token" in result:
        return result["access_token"]
    return None


def _headers(interactive: bool = False):
    token = get_access_token(interactive)
    if not token:
        return None
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        # calendarView times below are naive (no offset) — this pins their
        # interpretation to UTC instead of the mailbox's configured zone.
        "Prefer": 'outlook.timezone="UTC"',
    }


def list_calendar_events(time_min_iso: str, time_max_iso: str, max_results: int = 15):
    """Returns a list of {summary, start, end, location} dicts, or None if
    Microsoft 365 isn't configured/authenticated."""
    headers = _headers()
    if not headers:
        return None
    try:
        res = requests.get(
            f"{GRAPH_ROOT}/me/calendarView",
            headers=headers,
            params={
                "startDateTime": time_min_iso,
                "endDateTime": time_max_iso,
                "$top": max_results,
                "$orderby": "start/dateTime",
                "$select": "subject,start,end,location",
            },
            timeout=10,
        )
        res.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Graph calendar list failed: {e}")
        return None

    events = []
    for item in res.json().get("value", []):
        events.append({
            "summary": item.get("subject", "(no title)"),
            "start": item.get("start", {}).get("dateTime"),
            "end": item.get("end", {}).get("dateTime"),
            "location": (item.get("location") or {}).get("displayName", ""),
            "source": "Outlook",
        })
    return events


def create_calendar_event(summary: str, start_iso: str, end_iso: str, description: str = "", location: str = "") -> str:
    headers = _headers()
    if not headers:
        return "Outlook Calendar isn't connected, sir — run microsoft_login.py first."
    body = {
        "subject": summary,
        "body": {"contentType": "Text", "content": description},
        "start": {"dateTime": start_iso, "timeZone": "UTC"},
        "end": {"dateTime": end_iso, "timeZone": "UTC"},
        "location": {"displayName": location},
    }
    try:
        res = requests.post(f"{GRAPH_ROOT}/me/events", headers=headers, json=body, timeout=10)
        res.raise_for_status()
        return f"Created '{summary}' on your Outlook Calendar, sir."
    except requests.Timeout:
        logger.error("Graph calendar create timed out")
        return "Outlook took too long to respond, sir — the network may be slow. Please try again."
    except requests.RequestException as e:
        logger.error(f"Graph calendar create failed: {e}")
        return "I couldn't create that event on Outlook Calendar, sir — check the internet connection and try again."


def list_recent_emails(max_results: int = 8, unread_only: bool = True):
    """Returns a list of {subject, sender, snippet} dicts, or None if
    Microsoft 365 isn't configured/authenticated."""
    headers = _headers()
    if not headers:
        return None
    params = {
        "$top": max_results,
        "$select": "subject,from,bodyPreview",
        "$orderby": "receivedDateTime desc",
    }
    if unread_only:
        params["$filter"] = "isRead eq false"
    try:
        res = requests.get(f"{GRAPH_ROOT}/me/mailFolders/inbox/messages", headers=headers, params=params, timeout=10)
        res.raise_for_status()
    except requests.RequestException as e:
        logger.error(f"Graph mail list failed: {e}")
        return None

    emails = []
    for item in res.json().get("value", []):
        sender = (item.get("from") or {}).get("emailAddress", {})
        emails.append({
            "subject": item.get("subject", "(no subject)"),
            "sender": sender.get("name") or sender.get("address", "(unknown)"),
            "snippet": item.get("bodyPreview", ""),
            "source": "Outlook",
        })
    return emails


def send_email(to: str, subject: str, body: str) -> str:
    headers = _headers()
    if not headers:
        return "Outlook Mail isn't connected, sir — run microsoft_login.py first."
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
    }
    try:
        res = requests.post(f"{GRAPH_ROOT}/me/sendMail", headers=headers, json=payload, timeout=10)
        res.raise_for_status()
        return f"Sent the email to {to}, sir."
    except requests.Timeout:
        logger.error("Graph send mail timed out")
        return "Outlook took too long to respond, sir — the network may be slow. Please try again."
    except requests.RequestException as e:
        logger.error(f"Graph send mail failed: {e}")
        return "I couldn't send that email, sir — check the internet connection and try again."
