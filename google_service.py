"""
google_service.py — Google Calendar + Gmail, via the official Google APIs
(no scraping, no simulated clicks).

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
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
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


def get_authorization_url(redirect_uri: str, login_hint: str = None):
    """Returns (auth_url, state, code_verifier) — see exchange_code for why
    the caller must stash both state and code_verifier (e.g. Flask session)
    and pass them back. login_hint pre-fills a specific email on Google's
    account chooser (the HUD's "switch account" list) — it's a UX nicety
    only, not a security boundary, since the user still has to complete a
    real sign-in either way."""
    flow = _web_flow(redirect_uri)
    kwargs = {
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "select_account consent",
    }
    if login_hint:
        kwargs["login_hint"] = login_hint
    auth_url, state = flow.authorization_url(**kwargs)
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
            "id": item.get("id", ""),
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
        return "Google Calendar isn't connected, sir — please sign in again."
    body = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start_iso},
        "end": {"dateTime": end_iso},
    }
    try:
        svc.events().insert(calendarId="primary", body=body).execute()
        return f"Created '{summary}' on your Google Calendar, sir."
    except HttpError as e:
        logger.error(f"Google Calendar create failed: {e}")
        return "I couldn't create that event on Google Calendar, sir — the request was refused. Please try again shortly."
    except Exception as e:
        logger.error(f"Google Calendar create failed (network/transport error): {e}")
        return "I couldn't reach Google Calendar to create that event, sir — check the internet connection and try again."


def update_calendar_event(user_id: str, event_id: str, start_iso: str = "", end_iso: str = "") -> str:
    """Patches (not replaces) an existing event — only start/end are ever
    sent, so summary/location/description already on the real event are
    left untouched. Used for 'push my 10am back to 10:30' / 'that meeting
    is running long, extend it to 10:50' style requests, which create_
    calendar_event can't handle since the event already exists."""
    svc = _calendar_service(user_id)
    if not svc:
        return "Google Calendar isn't connected, sir — please sign in again."
    body = {}
    if start_iso:
        body["start"] = {"dateTime": start_iso}
    if end_iso:
        body["end"] = {"dateTime": end_iso}
    if not body:
        return "Nothing to update, sir — no new time was given."
    try:
        svc.events().patch(calendarId="primary", eventId=event_id, body=body).execute()
        return "Updated that event on your Google Calendar, sir."
    except HttpError as e:
        logger.error(f"Google Calendar update failed: {e}")
        return "I couldn't update that event, sir — Google Calendar refused the request. Please try again shortly."
    except Exception as e:
        logger.error(f"Google Calendar update failed (network/transport error): {e}")
        return "I couldn't reach Google Calendar to update that event, sir — check the internet connection and try again."


def delete_calendar_event(user_id: str, event_id: str) -> str:
    svc = _calendar_service(user_id)
    if not svc:
        return "Google Calendar isn't connected, sir — please sign in again."
    try:
        svc.events().delete(calendarId="primary", eventId=event_id).execute()
        return "Cancelled that event on your Google Calendar, sir."
    except HttpError as e:
        logger.error(f"Google Calendar delete failed: {e}")
        return "I couldn't cancel that event, sir — Google Calendar refused the request. Please try again shortly."
    except Exception as e:
        logger.error(f"Google Calendar delete failed (network/transport error): {e}")
        return "I couldn't reach Google Calendar to cancel that event, sir — check the internet connection and try again."


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


def send_email(user_id: str, to: str, subject: str, body: str, attachments: list = None) -> str:
    """attachments, if given, is a list of {"filename": str, "content_b64": str}
    dicts — already-base64 file content, as it arrives from the HUD's file
    input (see app.py's /api/compose/send). Plain MIMEText when there are
    none, same as before; only switches to multipart when there's actually
    something to attach."""
    svc = _gmail_service(user_id)
    if not svc:
        return "Gmail isn't connected, sir — please sign in again."
    try:
        if attachments:
            message = MIMEMultipart()
            message.attach(MIMEText(body))
            for att in attachments:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(base64.b64decode(att["content_b64"]))
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{att["filename"]}"')
                message.attach(part)
        else:
            message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("utf-8")
        svc.users().messages().send(userId="me", body={"raw": raw}).execute()
        return f"Sent the email to {to}, sir."
    except HttpError as e:
        logger.error(f"Gmail send failed: {e}")
        return "I couldn't send that email, sir — Gmail refused the request. Please try again shortly."
    except Exception as e:
        logger.error(f"Gmail send failed (network/transport error): {e}")
        return "I couldn't reach Gmail to send that email, sir — check the internet connection and try again."
