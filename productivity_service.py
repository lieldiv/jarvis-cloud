"""
productivity_service.py — the "smart schedule + email" layer that ties
google_service.py and microsoft_service.py together into one interface for
app.py's tools and daily_briefing.py's scheduler.

MULTI-USER BUILD: every function now takes a `user_id` and passes it through
to google_service.py's per-user token lookups. microsoft_service.py is NOT
part of this build's login flow (Google-only sign-in, per the plan) and
isn't currently configured with any credentials at all, so its CONFIGURED
flag is False and the microsoft_service.* calls below are dead branches for
now — left in place, unchanged, so Microsoft support is a later addition
rather than a rewrite.

Autonomy level (chosen deliberately, not a default): JARVIS reads calendars
and mailboxes freely, but never creates an event or sends an email without
a spoken confirmation first. Reads go straight through; writes are parked
via guardrails.request_confirmation, the same pattern file_tools.py uses
for delete/kill — one audited confirm-to-act path for every "irreversible"
action in the codebase, not a special case just for this feature.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo

import google_service
import microsoft_service
from guardrails import request_confirmation, get_pending_meta, update_pending

logger = logging.getLogger("jarvis.productivity")

# Same zone as app.py's LOCAL_TZ (same env var, same default) — kept as a
# separate constant rather than imported from app.py to avoid a circular
# import (app.py imports this module). Used so "today"/spoken times reflect
# the user's actual local day, not the server's (Render's containers run on
# UTC, so a naive datetime.now() near midnight could report the wrong date).
LOCAL_TZ = ZoneInfo(os.environ.get("JARVIS_TIMEZONE", "Asia/Jerusalem"))

_HEBREW_WEEKDAYS = ["יום שני", "יום שלישי", "יום רביעי", "יום חמישי", "יום שישי", "שבת", "יום ראשון"]
_HEBREW_WEEKDAYS_SHORT = ["יום ב׳", "יום ג׳", "יום ד׳", "יום ה׳", "יום ו׳", "שבת", "יום א׳"]
_HEBREW_MONTHS = [
    "בינואר", "בפברואר", "במרץ", "באפריל", "במאי", "ביוני",
    "ביולי", "באוגוסט", "בספטמבר", "באוקטובר", "בנובמבר", "בדצמבר",
]


def _hebrew_date_label(dt: datetime) -> str:
    return f"{_HEBREW_WEEKDAYS[dt.weekday()]}, {dt.day} {_HEBREW_MONTHS[dt.month - 1]}"


def any_calendar_configured() -> bool:
    return google_service.CONFIGURED or microsoft_service.CONFIGURED


def any_mail_configured() -> bool:
    return google_service.CONFIGURED or microsoft_service.CONFIGURED


def _time_window(days_ahead: float):
    now = datetime.now(timezone.utc)
    end = now + timedelta(days=days_ahead)
    google_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    google_max = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    # Microsoft's calendarView wants naive timestamps (no 'Z'/offset); we
    # pin their interpretation to UTC via the Prefer header instead.
    ms_min = now.strftime("%Y-%m-%dT%H:%M:%S")
    ms_max = end.strftime("%Y-%m-%dT%H:%M:%S")
    return google_min, google_max, ms_min, ms_max


def _collect_events(user_id: str, days_ahead: float, max_results: int = 15):
    if not any_calendar_configured():
        return None

    google_min, google_max, ms_min, ms_max = _time_window(days_ahead)
    events = []

    if google_service.CONFIGURED:
        g_events = google_service.list_calendar_events(user_id, google_min, google_max, max_results)
        if g_events:
            events.extend(g_events)

    if microsoft_service.CONFIGURED:
        m_events = microsoft_service.list_calendar_events(ms_min, ms_max, max_results)
        if m_events:
            events.extend(m_events)

    events.sort(key=lambda e: e.get("start") or "")
    return events[:max_results]


def _format_time(iso_str: str) -> str:
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        return f"{_HEBREW_WEEKDAYS_SHORT[dt.weekday()]} {dt.strftime('%H:%M')}"
    except ValueError:
        return iso_str


def get_calendar_events_text(user_id: str, days_ahead: float = 1, max_results: int = 15) -> str:
    events = _collect_events(user_id, days_ahead, max_results)
    if events is None:
        return "אין יומן מחובר, אדוני — אנא התחבר קודם."
    if not events:
        return "אין כלום ביומן בטווח הזה, אדוני."

    lines = []
    for e in events:
        where = f" @ {e['location']}" if e.get("location") else ""
        lines.append(f"- {_format_time(e['start'])}: {e['summary']}{where} [{e['source']}]")
    return "\n".join(lines)


def get_upcoming_events_structured(user_id: str, max_results: int = 3):
    """Structured (not spoken-string) event list for the HUD's "upcoming
    events" widget — same data as get_calendar_events_text but as
    JSON-friendly dicts. Returns None if no calendar provider is configured
    at all (vs. an empty list, which means configured-but-nothing-on-it or
    not yet authenticated)."""
    events = _collect_events(user_id, days_ahead=7, max_results=max_results)
    if events is None:
        return None
    return [
        {"summary": e.get("summary", "(no title)"), "time_label": _format_time(e.get("start")), "source": e.get("source", "")}
        for e in events
    ]


def _collect_emails(user_id: str, unread_only: bool, max_results: int):
    if not any_mail_configured():
        return None

    emails = []
    if google_service.CONFIGURED:
        query = "is:unread" if unread_only else ""
        g_emails = google_service.list_recent_emails(user_id, max_results, query)
        if g_emails:
            emails.extend(g_emails)

    if microsoft_service.CONFIGURED:
        m_emails = microsoft_service.list_recent_emails(max_results, unread_only)
        if m_emails:
            emails.extend(m_emails)

    return emails[:max_results]


def get_emails_text(user_id: str, unread_only: bool = True, max_results: int = 8) -> str:
    emails = _collect_emails(user_id, unread_only, max_results)
    if emails is None:
        return "אין תיבת דואר מחוברת, אדוני — אנא התחבר קודם."
    if not emails:
        return "תיבת הדואר ריקה, אדוני." if unread_only else "אין הודעות אחרונות, אדוני."

    lines = [f"- {e['sender']}: {e['subject']} — {e['snippet'][:80]} [{e['source']}]" for e in emails]
    return "\n".join(lines)


def get_inbox_structured(user_id: str, max_results: int = 8):
    """Structured (not spoken-string) unread-email list for the HUD's inbox
    modal — same data as get_emails_text but as JSON-friendly dicts, with the
    sender's actual address parsed out of the raw "Name <addr>" header (needed
    so "reply" has somewhere to actually send to). Returns None if no mailbox
    is configured at all."""
    emails = _collect_emails(user_id, unread_only=True, max_results=max_results)
    if emails is None:
        return None
    structured = []
    for e in emails:
        name, addr = parseaddr(e.get("sender", ""))
        structured.append({
            "sender_name": name or addr or e.get("sender", "(unknown)"),
            "sender_email": addr or e.get("sender", ""),
            "subject": e.get("subject", "(no subject)"),
            "snippet": e.get("snippet", ""),
            "source": e.get("source", ""),
        })
    return structured


def get_inbox_summary_spoken(emails: list) -> str:
    """Pure string formatting, no LLM call — same zero-LLM philosophy as
    get_daily_agenda_text/daily_briefing.py, so this can't come back garbled
    with markdown (there's no free-form model output to strip) and can't
    break just because a Groq key is flaky."""
    if not emails:
        return "תיבת הדואר שלך ריקה, אדוני."
    count = len(emails)
    lines = [f"יש לך {count} אימיילים שלא נקראו, אדוני." if count != 1 else "יש לך אימייל אחד שלא נקרא, אדוני."]
    for e in emails[:5]:
        lines.append(f"מאת {e['sender_name']}: {e['subject']}.")
    return " ".join(lines)


def get_daily_agenda_text(user_id: str) -> str:
    """Pure string formatting, no LLM call — used both as a tool result
    (so voice queries like 'what's my day look like' work any time) and by
    daily_briefing.py's scheduler (so the proactive morning brief doesn't
    spend a Groq API call it doesn't need)."""
    today_label = _hebrew_date_label(datetime.now(LOCAL_TZ))

    if not any_calendar_configured() and not any_mail_configured():
        return (
            f"בוקר טוב, אדוני. היום {today_label}. "
            "אין עדיין יומן או תיבת דואר מחוברים — ראה את קובץ ה-README כדי לחבר אחד."
        )

    events = _collect_events(user_id, days_ahead=1, max_results=15) or []
    emails = _collect_emails(user_id, unread_only=True, max_results=10) or []

    parts = [f"בוקר טוב, אדוני. היום {today_label}."]

    if events:
        parts.append(f"יש לך {len(events)} פריטים ביומן:" if len(events) != 1 else "יש לך פריט אחד ביומן:")
        for e in events:
            where = f" ב{e['location']}" if e.get("location") else ""
            parts.append(f"  {_format_time(e['start'])} — {e['summary']}{where}")
    elif any_calendar_configured():
        parts.append("אין כלום ביומן היום.")

    if emails:
        parts.append(f"יש {len(emails)} אימיילים שלא נקראו, כולל:" if len(emails) != 1 else "יש אימייל אחד שלא נקרא:")
        for e in emails[:5]:
            parts.append(f"  מאת {e['sender']}: {e['subject']}")
    elif any_mail_configured():
        parts.append("תיבת הדואר ריקה.")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Writes — always confirm-gated, never fire immediately
# ---------------------------------------------------------------------------
def _pick_calendar_provider(provider: str) -> str:
    provider = (provider or "auto").lower()
    if provider in ("google", "microsoft"):
        return provider
    if google_service.CONFIGURED:
        return "google"
    if microsoft_service.CONFIGURED:
        return "microsoft"
    return "none"


def request_create_calendar_event(user_id: str, summary: str, start_iso: str, end_iso: str,
                                   description: str = "", location: str = "",
                                   provider: str = "auto") -> dict:
    target = _pick_calendar_provider(provider)
    if target == "none":
        return {"status": "refused", "message": "אין יומן מחובר, אדוני — אנא התחבר קודם."}

    if target == "google":
        impl = google_service.create_calendar_event
        cb_args = (user_id, summary, start_iso, end_iso, description, location)
    else:
        impl = microsoft_service.create_calendar_event
        cb_args = (summary, start_iso, end_iso, description, location)

    description_text = f"יצירת אירוע ביומן '{summary}' ({start_iso} - {end_iso}) ב-{target.title()}"
    token = request_confirmation(
        description_text, impl, *cb_args,
        meta={"kind": "calendar_event", "provider": target, "user_id": user_id},
    )
    # `kind` + `details` let the HUD render a proper card instead of a text
    # blob — see resolveConfirmation()/renderConfirmationModal() in
    # index.html. Approving re-runs `impl` with whatever args are currently
    # stored for `token`: normally that's exactly what was proposed here,
    # but edit_pending_calendar_event() below can swap them first if the
    # user corrects something in the HUD before approving.
    return {
        "status": "confirmation_required", "token": token, "message": description_text,
        "kind": "calendar_event",
        "details": {
            "summary": summary, "start_iso": start_iso, "end_iso": end_iso,
            "description": description, "location": location, "provider": target.title(),
        },
    }


def edit_pending_calendar_event(token: str, summary: str, start_iso: str, end_iso: str,
                                 description: str = "", location: str = "") -> dict:
    """Rewrites a still-pending calendar-event proposal in place, so the
    next approve/deny acts on the corrected fields instead of the original
    ones. Refuses if the token expired or was never a calendar-event
    proposal — same failure mode as approving/denying a stale token."""
    meta = get_pending_meta(token)
    if not meta or meta.get("kind") != "calendar_event":
        return {"status": "error", "message": "האישור הזה כבר פג תוקף או שאינו קיים, אדוני."}

    target = meta["provider"]
    if target == "google":
        cb_args = (meta["user_id"], summary, start_iso, end_iso, description, location)
    else:
        cb_args = (summary, start_iso, end_iso, description, location)

    description_text = f"יצירת אירוע ביומן '{summary}' ({start_iso} - {end_iso}) ב-{target.title()}"
    if not update_pending(token, cb_args, description_text):
        return {"status": "error", "message": "האישור הזה כבר פג תוקף או שאינו קיים, אדוני."}

    return {
        "status": "ok", "token": token, "message": description_text,
        "kind": "calendar_event",
        "details": {
            "summary": summary, "start_iso": start_iso, "end_iso": end_iso,
            "description": description, "location": location, "provider": target.title(),
        },
    }


def _pick_mail_provider(provider: str) -> str:
    provider = (provider or "auto").lower()
    if provider in ("google", "microsoft"):
        return provider
    if google_service.CONFIGURED:
        return "google"
    if microsoft_service.CONFIGURED:
        return "microsoft"
    return "none"


def request_send_email(user_id: str, to: str, subject: str, body: str, provider: str = "auto") -> dict:
    target = _pick_mail_provider(provider)
    if target == "none":
        return {"status": "refused", "message": "אין תיבת דואר מחוברת, אדוני — אנא התחבר קודם."}

    if target == "google":
        impl = google_service.send_email
        cb_args = (user_id, to, subject, body)
    else:
        impl = microsoft_service.send_email
        cb_args = (to, subject, body)

    description_text = f"שליחת אימייל אל {to}: '{subject}' דרך {target.title()}"
    token = request_confirmation(
        description_text, impl, *cb_args,
        meta={"kind": "email", "provider": target, "user_id": user_id},
    )
    return {
        "status": "confirmation_required", "token": token, "message": description_text,
        "kind": "email",
        "details": {"to": to, "subject": subject, "body": body, "provider": target.title()},
    }


def edit_pending_email(token: str, to: str, subject: str, body: str) -> dict:
    """Same idea as edit_pending_calendar_event() but for a pending send_email
    proposal — swaps the stored args so approval sends the corrected message."""
    meta = get_pending_meta(token)
    if not meta or meta.get("kind") != "email":
        return {"status": "error", "message": "האישור הזה כבר פג תוקף או שאינו קיים, אדוני."}

    target = meta["provider"]
    if target == "google":
        cb_args = (meta["user_id"], to, subject, body)
    else:
        cb_args = (to, subject, body)

    description_text = f"שליחת אימייל אל {to}: '{subject}' דרך {target.title()}"
    if not update_pending(token, cb_args, description_text):
        return {"status": "error", "message": "האישור הזה כבר פג תוקף או שאינו קיים, אדוני."}

    return {
        "status": "ok", "token": token, "message": description_text,
        "kind": "email",
        "details": {"to": to, "subject": subject, "body": body, "provider": target.title()},
    }
