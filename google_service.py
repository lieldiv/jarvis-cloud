"""
google_service.py — Google Calendar + Gmail, via the official Google APIs
(no scraping, no simulated clicks — same "real API" approach as Spotify).

MULTI-USER BUILD: every function that touches a token now takes a `user_id`
(Google's own stable account id) and reads/writes that user's row via
users.py's SQLite store, instead of the single-tenant free 400 שקל version's
one shared .google_cache/token.json file. "Sign in with Google" and "connect
Google Calendar/Gmail" are the same flow here — the openid/email/profile
scopes below identify *who* signed in, the calendar/gmail scopes grant what
JARVIS can do on their behalf, all in one consent screen.

Setup (see README for the full walkthrough):
  1. https://console.cloud.google.com/ -> new project -> enable the
     "Google Calendar API" and "Gmail API".
  2. For local dev: an OAuth client of type "Desktop app" (loopback redirect
     works without pre-registering it). For the deployed cloud build: a
     SEPARATE OAuth client of type "Web application" with the production
     HTTPS callback URL registered as an authorized redirect URI — Desktop
     clients don't accept arbitrary HTTPS redirect URIs. Put whichever one
     you're running against in .env / the host's env vars as
     GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.

Scopes requested are deliberately split: read scopes are what JARVIS uses
for the daily agenda and email summaries; the write scopes
(calendar.events, gmail.send) are only ever invoked through
productivity_service.py's confirm-before-acting wrapper, matching the
"read-only + suggestions" autonomy level this assistant is built for.
"""

import logging
import base64
import os
from email.mime.text import MIMEText

import cert_bootstrap  # noqa: F401 — must run before any HTTPS-making import below
import users

logger = logging.getLogger("jarvis.google")

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    GOOGLE_LIBS_AVAILABLE = True
except Exception:
    GOOGLE_LIBS_AVAILABLE = False

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")

# openid/email/profile identify the signed-in user (used to create/look up
# their row in users.py); the rest is what JARVIS is allowed to do for them.
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.readonly",
]

CONFIGURED = bool(GOOGLE_LIBS_AVAILABLE and GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _client_config():
    return {
        "installed": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def get_credentials(user_id: str, interactive: bool = False):
    """Load this user's stored credentials, refreshing if needed.
    `interactive` is kept for signature parity with the old single-tenant
    version but is unused here — there's no "pop a browser for the server
    process" concept in a multi-user web app; sign-in only ever happens via
    the web redirect flow (get_authorization_url/exchange_code below).
    Returns None if unconfigured or the user has no usable token.
    """
    if not CONFIGURED or not user_id:
        return None

    token_json = users.get_google_token(user_id)
    if not token_json:
        return None

    try:
        creds = Credentials.from_authorized_user_info(_parse_token(token_json), SCOPES)
    except Exception as e:
        logger.warning(f"Couldn't load stored Google token for user {user_id}: {e}")
        return None

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_creds(user_id, creds)
            return creds
        except Exception as e:
            logger.warning(f"Google token refresh failed for user {user_id}: {e}")
            return None

    return None


def _parse_token(token_json: str) -> dict:
    import json
    return json.loads(token_json)


def _save_creds(user_id: str, creds) -> None:
    users.save_google_token(user_id, creds.to_json())


def disconnect(user_id: str) -> None:
    """Drops this user's stored token — local-only, doesn't revoke the grant
    on Google's side, so reconnecting just needs a fresh consent screen."""
    if user_id:
        users.clear_google_token(user_id)


def is_connected(user_id: str) -> bool:
    return get_credentials(user_id) is not None


def _web_flow(redirect_uri: str):
    return Flow.from_client_config(_client_config(), SCOPES, redirect_uri=redirect_uri)


def get_authorization_url(redirect_uri: str):
    """Returns (auth_url, state, code_verifier) — see exchange_code for why
    the caller must stash both state and code_verifier (e.g. Flask session)
    and pass them back."""
    flow = _web_flow(redirect_uri)
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="select_account consent",
    )
    return auth_url, state, flow.code_verifier


def exchange_code(redirect_uri: str, code: str, code_verifier: str = None):
    """Trades the ?code= from Google's redirect for tokens, identifies the
    signed-in user (via the userinfo API — more reliable than manually
    decoding the id_token), upserts their row in users.py, and stores the
    token under their id. Returns the user dict ({id, email, name}) on
    success or None on failure — the caller sets session['user_id'] from it.
    """
    if not CONFIGURED or not code:
        return None
    try:
        flow = _web_flow(redirect_uri)
        flow.code_verifier = code_verifier
        flow.fetch_token(code=code)
        creds = flow.credentials

        oauth2 = build("oauth2", "v2", credentials=creds)
        info = oauth2.userinfo().get().execute()
        user_id = info["id"]

        users.upsert_user(user_id, info.get("email", ""), info.get("name", ""))
        _save_creds(user_id, creds)
        return {"id": user_id, "email": info.get("email", ""), "name": info.get("name", "")}
    except Exception as e:
        logger.error(f"Google OAuth code exchange failed: {e}")
        return None


def _calendar_service(user_id: str):
    creds = get_credentials(user_id)
    if not creds:
        return None
    return build("calendar", "v3", credentials=creds)


def _gmail_service(user_id: str):
    creds = get_credentials(user_id)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def list_calendar_events(user_id: str, time_min_iso: str, time_max_iso: str, max_results: int = 15):
    """Returns a list of {summary, start, end, location} dicts, or None if
    this user has no connected Google Calendar."""
    svc = _calendar_service(user_id)
    if not svc:
        return None
    try:
        result = svc.events().list(
            calendarId="primary",
            timeMin=time_min_iso,
            timeMax=time_max_iso,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
    except HttpError as e:
        logger.error(f"Google Calendar list failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Google Calendar list failed (network/transport error): {e}")
        return None

    events = []
    for item in result.get("items", []):
        start = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
        end = item.get("end", {}).get("dateTime") or item.get("end", {}).get("date")
        events.append({
            "summary": item.get("summary", "(no title)"),
            "start": start,
            "end": end,
            "location": item.get("location", ""),
            "source": "Google",
        })
    return events


def create_calendar_event(user_id: str, summary: str, start_iso: str, end_iso: str, description: str = "", location: str = "") -> str:
    svc = _calendar_service(user_id)
    if not svc:
        return "Google Calendar לא מחובר, אדוני — אנא התחבר שוב."
    body = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    try:
        svc.events().insert(calendarId="primary", body=body).execute()
        return f"נוצר '{summary}' ב-Google Calendar שלך, אדוני."
    except HttpError as e:
        logger.error(f"Google Calendar create failed: {e}")
        return "לא הצלחתי ליצור את האירוע ב-Google Calendar, אדוני — הבקשה נדחתה. אנא נסה שוב בקרוב."
    except Exception as e:
        logger.error(f"Google Calendar create failed (network/transport error): {e}")
        return "לא הצלחתי להתחבר ל-Google Calendar כדי ליצור את האירוע, אדוני — בדוק את חיבור האינטרנט ונסה שוב."


def list_recent_emails(user_id: str, max_results: int = 8, query: str = "is:unread"):
    """Returns a list of {subject, sender, snippet} dicts, or None if this
    user has no connected Gmail."""
    svc = _gmail_service(user_id)
    if not svc:
        return None
    try:
        result = svc.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        message_ids = [m["id"] for m in result.get("messages", [])]

        emails = []
        for mid in message_ids:
            msg = svc.users().messages().get(
                userId="me", id=mid, format="metadata",
                metadataHeaders=["From", "Subject"],
            ).execute()
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            emails.append({
                "subject": headers.get("Subject", "(no subject)"),
                "sender": headers.get("From", "(unknown)"),
                "snippet": msg.get("snippet", ""),
                "source": "Gmail",
            })
        return emails
    except HttpError as e:
        logger.error(f"Gmail list failed: {e}")
        return None
    except Exception as e:
        logger.error(f"Gmail list failed (network/transport error): {e}")
        return None


def send_email(user_id: str, to: str, subject: str, body: str) -> str:
    svc = _gmail_service(user_id)
    if not svc:
        return "Gmail לא מחובר, אדוני — אנא התחבר שוב."
    try:
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"האימייל נשלח אל {to}, אדוני."
    except HttpError as e:
        logger.error(f"Gmail send failed: {e}")
        return "לא הצלחתי לשלוח את האימייל, אדוני — Gmail דחה את הבקשה. אנא נסה שוב בקרוב."
    except Exception as e:
        logger.error(f"Gmail send failed (network/transport error): {e}")
        return "לא הצלחתי להתחבר ל-Gmail כדי לשלוח את האימייל, אדוני — בדוק את חיבור האינטרנט ונסה שוב."
